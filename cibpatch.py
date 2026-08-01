#!/usr/bin/env python3
"""Prepare a freshly created macOS guest without ever showing Setup Assistant.

Setup Assistant cannot be skipped from outside a running guest, and driving it with
synthetic keystrokes is brittle: one changed pane and the whole sequence types into
the wrong field. So this does the other thing — it writes the state Setup Assistant
would have produced directly onto the guest's disk, before the guest ever boots.

That is deterministic: no timing, no OCR, and nothing to re-learn when Apple moves a
button. It is also unsupported, so every write here is one Apple could change; the
markers are documented inline so a future reader can check them against a real
install rather than guess.

What gets written, and why each is needed:

  /var/db/.AppleSetupDone            loginwindow stats this path; if it exists there
                                     is no first-boot Setup Assistant
  dslocal user record                .AppleSetupDone alone would leave a login
                                     window with no accounts to log in to
  admin + staff membership           an account that cannot administer the guest
                                     cannot install anything
  SetupAssistant DidSee* keys        suppresses the *per-user* assistant, which runs
                                     at first login even when the system one is gone
  Library/User Template/.skipbuddy   the same, for accounts created later
  HIToolbox input sources            the host's keyboard layout; a guest left on
                                     U.S. puts the punctuation somewhere else
  loginwindow autoLoginUser          boot straight to the desktop
  /etc/kcpassword                    the obfuscated password autologin reads
  launchd disabled.plist             turns on Remote Login, which is how cib then
                                     takes the guest over

Standard library only, like the rest of this project: hashlib does PBKDF2 and
plistlib does the plists.
"""

from __future__ import annotations

import hashlib
import os
import plistlib
import secrets
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from xml.parsers.expat import ExpatError

# macOS stores an salted PBKDF2-SHA512 verifier. These are the parameters a real
# account gets; they are not a security choice of ours, they are what the OS expects.
SALT_BYTES = 32
ENTROPY_BYTES = 128
ITERATIONS = 50_000

# XOR key for /etc/kcpassword. Apple's autologin obfuscation, not encryption.
KCPASSWORD_KEY = bytes([0x7D, 0x89, 0x52, 0x23, 0xD2, 0xBC, 0xDD, 0xEA, 0xA3, 0xB9, 0x1F])

UID = 501
GID = 20


class PatchError(Exception):
    """Something the caller should see as a message, not a traceback."""


@dataclass(frozen=True)
class Account:
    name: str
    password: str
    full_name: str = "browser-in-a-box"
    uid: int = UID
    gid: int = GID


@dataclass(frozen=True)
class Keys:
    """What the guest needs so cib can reach it by key alone: the public key it
    should trust, and the host key it should present."""

    authorized: str = ""
    host_private: str = ""
    host_public: str = ""


@dataclass(frozen=True)
class Keyboard:
    """The layout the guest should type in. Defaults to what macOS installs with,
    so a caller that cannot read the host's layout still gets a working guest."""

    layout_id: int = 0
    name: str = "U.S."


def shadow_hash_data(password: str) -> bytes:
    """The binary plist macOS keeps a password verifier in.

    Wrong iteration counts or a wrong layout do not fail loudly — the account simply
    refuses every password — so this mirrors what dscl produces exactly.
    """
    salt = secrets.token_bytes(SALT_BYTES)
    entropy = hashlib.pbkdf2_hmac("sha512", password.encode(), salt, ITERATIONS, ENTROPY_BYTES)
    return plistlib.dumps(
        {
            "SALTED-SHA512-PBKDF2": {
                "entropy": entropy,
                "salt": salt,
                "iterations": ITERATIONS,
            }
        },
        fmt=plistlib.FMT_BINARY,
    )


def user_record(account: Account, guid: str | None = None) -> dict[str, list]:
    """A dslocal user plist. Every value is a list — that is how DirectoryService
    stores single values, and a plain string is silently ignored."""
    return {
        "name": [account.name],
        "realname": [account.full_name],
        "uid": [str(account.uid)],
        "gid": [str(account.gid)],
        "home": [f"/Users/{account.name}"],
        "shell": ["/bin/zsh"],
        "generateduid": [guid or str(uuid.uuid4()).upper()],
        "authentication_authority": [";ShadowHash;HASHLIST:<SALTED-SHA512-PBKDF2>"],
        "passwd": ["********"],
        "ShadowHashData": [shadow_hash_data(account.password)],
        "_writers_passwd": [account.name],
    }


def kcpassword(password: str) -> bytes:
    """Autologin reads /etc/kcpassword, which is the password XOR-ed with a fixed
    key and padded to a multiple of the key length."""
    data = bytearray(password.encode())
    # The padding is not optional: without it macOS reads past the password.
    data.append(0)
    while len(data) % len(KCPASSWORD_KEY) != 0:
        data.append(0)
    return bytes(b ^ KCPASSWORD_KEY[i % len(KCPASSWORD_KEY)] for i, b in enumerate(data))


def guest_path(
    root: Path, relative: str, make_parents: bool = False, directory: bool = False
) -> Path:
    """A path inside the guest volume, with every component proved not to be a link.

    This runs as root on the *host*, and the guest volume is just a directory on the
    host filesystem: a symlink stored inside the guest resolves against the host. So
    `ln -s /private/etc "$HOME/Library"` in the guest would redirect a root-owned
    write here onto the host's /etc.

    Round 7 put a guard on the home directory, but it ran last — four earlier steps
    had already written through whatever link they were given. This is the guard for
    all of them, so the class is closed rather than one instance of it.

    Checked once rather than by opening every component with O_NOFOLLOW: the guest is
    powered off and its disk is attached to this host alone, so no component can be
    swapped while this runs.
    """
    current = root
    parts = relative.strip("/").split("/")
    leaf = root.joinpath(*parts)
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise PatchError(
                f"{relative!r} passes through a symlink inside the guest ({current}); "
                "refusing to write, because it resolves against this host's filesystem"
            )
        # A symlink is not the only thing that misbehaves when opened as root: a FIFO
        # planted in the guest blocks open() until something opens the other end,
        # which nothing ever will, so the patch would hang for ever. Sockets and
        # device nodes are refused for the same reason.
        if current.exists() and not (current.is_dir() or current.is_file()):
            raise PatchError(
                f"{relative!r} passes through {current}, which is neither a directory "
                "nor a regular file; refusing to write through it"
            )
        # A regular file where a directory has to be — the guest making ~/.ssh a
        # file, say — otherwise came out as a NotADirectoryError traceback from a
        # step running as root.
        if current.is_file() and current != leaf:
            raise PatchError(
                f"{relative!r} passes through {current}, which is a file where a "
                "directory has to be; refusing to write through it"
            )
    # The leaf's own kind, checked whether or not parents are being created: a
    # directory where a file belongs is a traceback from a step running as root,
    # and a file where a directory belongs is the same in reverse.
    if current.exists():
        if directory and not current.is_dir():
            raise PatchError(
                f"{relative!r} is a file in the guest where a directory has to be; "
                "refusing to write through it"
            )
        if not directory and current.is_dir():
            raise PatchError(
                f"{relative!r} is a directory in the guest where a file has to be; "
                "refusing to write through it"
            )
    if make_parents:
        # Safe now: no component above this one is a link, so nothing can be
        # created somewhere else.
        current.parent.mkdir(parents=True, exist_ok=True)
    return current


def write_plist(root: Path, relative: str, data: dict) -> None:
    path = guest_path(root, relative, make_parents=True)
    with path.open("wb") as handle:
        plistlib.dump(data, handle, fmt=plistlib.FMT_BINARY)


def read_plist(root: Path, relative: str) -> dict:
    path = guest_path(root, relative)
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            record = plistlib.load(handle)
    except (plistlib.InvalidFileException, OSError, ValueError, ExpatError):
        return {}
    # A plist root can be any type. Treating an array as a dict raised a traceback
    # out of a step running as root, rather than a message.
    return record if isinstance(record, dict) else {}


def write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Write a secret with its permissions set at creation.

    Creating the file and chmod-ing it afterwards leaves it world-readable for the
    moment in between, which is long enough on a filesystem anything else can see.
    """
    path.unlink(missing_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode), "wb") as handle:
        handle.write(data)


def authorise_key(root: Path, account: Account, public_key: str) -> None:
    """Let cib log in as the account without a password.

    sshd runs 'cib vm setup' non-interactively, and the script it runs carries the
    account's password for sudo. Sending that password to authenticate as well
    would mean sending it before the peer is identified at all.
    """
    # directory=True: this leaf IS a directory, and after the first patch it exists.
    # Without it every later 'cib vm prepare' — the documented retry — refused to
    # run, blaming the guest for something the guest did correctly.
    ssh_dir = guest_path(root, f"Users/{account.name}/.ssh", make_parents=True, directory=True)
    ssh_dir.mkdir(parents=True, exist_ok=True)
    ssh_dir.chmod(0o700)
    write_private(
        guest_path(root, f"Users/{account.name}/.ssh/authorized_keys"),
        public_key.strip().encode() + b"\n",
    )


def plant_host_key(root: Path, private_key: str, public_key: str) -> None:
    """Give the guest the host key cib already expects.

    A host key the guest generates on first boot cannot be verified on the first
    connection, which is the one that carries the password. Planting it means there
    is never an unverified connection.
    """
    write_private(
        guest_path(root, "private/etc/ssh/ssh_host_ed25519_key", make_parents=True),
        private_key.encode(),
    )
    write_private(
        guest_path(root, "private/etc/ssh/ssh_host_ed25519_key.pub"),
        public_key.strip().encode() + b"\n",
        mode=0o644,
    )


def add_to_group(root: Path, group: str, account: Account, guid: str) -> None:
    """Append the account to a dslocal group, keeping whatever is already there.

    Both lists have to be written: "users" holds short names, "groupmembers" holds
    the *user's* GUID. Writing only one leaves the membership half-recorded, and
    macOS believes whichever it consults first.
    """
    relative = f"private/var/db/dslocal/nodes/Default/groups/{group}.plist"
    record = read_plist(root, relative)
    if not record:
        raise PatchError(f"the guest has no {group} group at {root / relative}")
    for key, member in (("users", account.name), ("groupmembers", guid)):
        members = list(record.get(key, []))
        if member not in members:
            members.append(member)
        record[key] = members
    write_plist(root, relative, record)


def suppress_setup_assistant(root: Path, account: Account) -> None:
    """The system assistant and the per-user one are separate; skipping only the
    first leaves the second waiting at the desktop."""
    seen = {
        "DidSeeCloudSetup": True,
        "DidSeePrivacy": True,
        "DidSeeSiriSetup": True,
        "DidSeeTouchIDSetup": True,
        "DidSeeTrueToneSetup": True,
        "DidSeeAppearanceSetup": True,
        "GestureMovieSeen": "none",
        "LastSeenBuddyBuildVersion": "99Z999",
        "LastSeenCloudProductVersion": "99.9",
    }
    write_plist(root, "Library/Preferences/com.apple.SetupAssistant.plist", seen)
    write_plist(
        root, f"Users/{account.name}/Library/Preferences/com.apple.SetupAssistant.plist", seen
    )
    # Accounts created later inherit the template, and would otherwise see it again.
    guest_path(root, "Library/User Template/.skipbuddy", make_parents=True).touch()


def set_keyboard_layout(root: Path, account: Account, keyboard: Keyboard) -> None:
    """Give the guest the host's keyboard layout.

    Written per account, because HIToolbox is a per-user preference. Both keys are
    needed: "Enabled" is what the guest may switch between and "Selected" is what
    it types in, and selecting a layout that is not enabled leaves it on U.S.
    """
    source = {
        "InputSourceKind": "Keyboard Layout",
        "KeyboardLayout ID": keyboard.layout_id,
        "KeyboardLayout Name": keyboard.name,
    }
    write_plist(
        root,
        f"Users/{account.name}/Library/Preferences/com.apple.HIToolbox.plist",
        {"AppleEnabledInputSources": [source], "AppleSelectedInputSources": [source]},
    )


def enable_autologin(root: Path, account: Account) -> None:
    relative = "Library/Preferences/com.apple.loginwindow.plist"
    record = read_plist(root, relative)
    record["autoLoginUser"] = account.name
    record["autoLoginUserUID"] = account.uid
    # "Log in automatically, then lock the screen", which is autologin doing all the
    # work and none of the good. Whatever the base image carried used to survive, so
    # the guest booted to a password prompt for a generated 24-character password
    # that a VM has no Touch ID to shortcut. It is a separate mechanism from the
    # screensaver lock: turning that one off, in every way there is, changes nothing
    # here, because loginwindow applies this at session creation.
    record["autoLoginUserScreenLocked"] = False
    # An AccountInfo entry re-launches Setup Assistant at the next graphical login
    # even with .AppleSetupDone present, so it has to go.
    record.pop("AccountInfo", None)
    write_plist(root, relative, record)
    kc = guest_path(root, "private/etc/kcpassword", make_parents=True)
    write_private(kc, kcpassword(account.password))


def enable_remote_login(root: Path) -> None:
    """Turn on sshd the way launchd records it, so cib can take over by SSH."""
    relative = "private/var/db/com.apple.xpc.launchd/disabled.plist"
    record = read_plist(root, relative)
    record["com.openssh.sshd"] = False
    write_plist(root, relative, record)


def create_account(root: Path, account: Account) -> None:
    users = guest_path(root, "private/var/db/dslocal/nodes/Default/users", directory=True)
    if not users.is_dir():
        raise PatchError(
            f"{users} is missing — is this the guest's Data volume, and has the guest "
            "been booted once so its first-boot state exists?"
        )
    # The uid is fixed at 501, so a second account under a different name would be
    # the same user to the filesystem: logging in as the new one would give full
    # access to the old one's home, Chrome profile and login keychain. Changing
    # CIB_VM_USER and re-running 'cib vm prepare' is a plausible way to reach this.
    # An account of this name that the guest already has: root, daemon, _spotlight
    # and a dozen others all match the name rules. Overwriting root's record with a
    # uid-501 one leaves the guest with no working sudo at all.
    mine = users / f"{account.name}.plist"
    if mine.exists() and not mine.is_symlink():
        existing_uid = read_plist(
            root, f"private/var/db/dslocal/nodes/Default/users/{account.name}.plist"
        ).get("uid")
        if existing_uid and existing_uid != [str(account.uid)]:
            raise PatchError(
                f"the guest already has an account called {account.name!r} on uid "
                f"{existing_uid[0]} — this would overwrite it. Pick another CIB_VM_USER."
            )
    for existing in sorted(users.glob("*.plist")):
        if existing.stem == account.name or existing.is_symlink():
            continue
        record = read_plist(root, f"private/var/db/dslocal/nodes/Default/users/{existing.name}")
        if record.get("uid") == [str(account.uid)]:
            raise PatchError(
                f"the guest already has an account {existing.stem!r} on uid {account.uid}, "
                f"so {account.name!r} would share its home and its keychain. Either set "
                f"CIB_VM_USER={existing.stem} to keep using it, or 'cib vm delete' and "
                "build again."
            )
    # Reused when the account is already there: group membership records the GUID,
    # so a fresh one on every 'cib vm prepare' would leave admin and staff pointing
    # at a user that no longer exists under that id.
    existing = read_plist(
        root, f"private/var/db/dslocal/nodes/Default/users/{account.name}.plist"
    ).get("generateduid")
    guid = existing[0] if existing else str(uuid.uuid4()).upper()
    write_plist(
        root,
        f"private/var/db/dslocal/nodes/Default/users/{account.name}.plist",
        user_record(account, guid),
    )
    for group in ("admin", "staff"):
        add_to_group(root, group, account, guid)
    home = guest_path(root, f"Users/{account.name}", directory=True)
    home.mkdir(parents=True, exist_ok=True)
    guest_path(root, f"Users/{account.name}/.CFUserTextEncoding").write_text("0:0")


def own_home(root: Path, account: Account) -> None:
    """Give the account its home, without ever following a link.

    The guest is where untrusted browsing happens, and this runs as root on the
    host. A symlink stored in the guest resolves against the *host* filesystem, so
    following one here would hand host paths to the guest: `ln -s / ~` inside the
    guest would otherwise chown the host's root filesystem on the next prepare.
    """
    home = guest_path(root, f"Users/{account.name}", directory=True)
    os.chown(home, account.uid, account.gid, follow_symlinks=False)
    for parent, dirs, files in os.walk(home, followlinks=False):
        for name in dirs + files:
            os.chown(Path(parent) / name, account.uid, account.gid, follow_symlinks=False)


def mark_setup_done(root: Path) -> None:
    marker = guest_path(root, "private/var/db/.AppleSetupDone", make_parents=True)
    marker.touch()
    os.chown(marker, 0, 0)
    marker.chmod(0o644)


def patch(
    root: Path,
    account: Account,
    keyboard: Keyboard | None = None,
    keys: Keys | None = None,
) -> None:
    """Apply everything to a mounted guest Data volume."""
    # This runs as root and builds paths from the account name, so it validates it
    # rather than trusting whoever invoked it.
    if "/" in account.name or ".." in account.name or account.name.startswith("."):
        raise PatchError(f"refusing to use {account.name!r} as an account name")
    if os.geteuid() != 0:
        raise PatchError(
            "preparing the guest writes into its dslocal database and has to set root "
            "ownership, so this needs root — re-run with sudo"
        )
    root = Path(root)
    if not guest_path(root, "private/var/db", directory=True).is_dir():
        raise PatchError(f"{root} does not look like a macOS Data volume")
    create_account(root, account)
    mark_setup_done(root)
    suppress_setup_assistant(root, account)
    set_keyboard_layout(root, account, keyboard or Keyboard())
    keys = keys or Keys()
    if keys.authorized:
        authorise_key(root, account, keys.authorized)
    if keys.host_private and keys.host_public:
        plant_host_key(root, keys.host_private, keys.host_public)
    enable_autologin(root, account)
    enable_remote_login(root)
    # Last, not inside create_account: suppress_setup_assistant creates
    # ~/Library/Preferences, and a home tree chowned before that leaves those
    # directories owned by root. cfprefsd then silently fails to write anything,
    # so no preference the guest sets — including the one this all depends on —
    # would ever persist.
    own_home(root, account)


# --- attaching the guest's disk ------------------------------------------------


def attach(disk: Path) -> str:
    """Attach a raw tart disk image and return the device node."""
    result = subprocess.run(  # noqa: S603
        [
            "/usr/bin/hdiutil",
            "attach",
            "-imagekey",
            "diskimage-class=CRawDiskImage",
            # Without this macOS mounts the volume noowners, where chown returns
            # success and writes nothing — every file the patcher creates would stay
            # root-owned and the guest could not write its own home.
            "-owners",
            "on",
            "-nomount",
            str(disk),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PatchError(f"could not attach {disk}: {result.stderr.strip()}")
    for line in result.stdout.splitlines():
        if line.startswith("/dev/disk"):
            return line.split()[0]
    raise PatchError(f"hdiutil attached {disk} but reported no device")


def detach(device: str) -> None:
    subprocess.run(  # noqa: S603
        ["/usr/bin/hdiutil", "detach", device, "-force"],
        capture_output=True,
        check=False,
    )


def data_volume(device: str) -> str:
    """The guest's writable volume.

    Attaching a disk image gives a device whose partitions hold an APFS *container*,
    and macOS synthesises that container onto a different disk number — so listing
    the attached device alone shows no volumes at all. The container is therefore
    found by its physical store pointing back at our device.

    Matched on the APFS role rather than the name, because the System volume is
    sealed and read-only: writing to the wrong one silently achieves nothing.

    A macOS disk carries more than one container: the first one found on the device
    holds iSCPreboot, xART, Hardware and Recovery, and the guest's own volumes are
    in the next. So every matching container is searched, not just the first.
    """
    result = subprocess.run(
        ["/usr/sbin/diskutil", "apfs", "list", "-plist"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise PatchError("could not list the APFS containers on this host")
    ours = Path(device).name  # e.g. "disk10"
    found: list[str] = []
    for container in plistlib.loads(result.stdout).get("Containers", []):
        stores = container.get("PhysicalStores", [])
        if not any(
            st.get("DeviceIdentifier", "") in (ours,)
            or st.get("DeviceIdentifier", "").startswith(ours + "s")
            for st in stores
        ):
            continue
        for volume in container.get("Volumes", []):
            roles = volume.get("Roles") or []
            if "Data" in roles or volume.get("Name") == "Data":
                return "/dev/" + volume["DeviceIdentifier"]
        found.extend(v.get("Name", "?") for v in container.get("Volumes", []))
    if found:
        raise PatchError(f"no Data volume on {device}; its containers hold " + ", ".join(found))
    raise PatchError(f"nothing on this host has {device} as its physical store")


def ownership_is_honoured(mountpoint: Path) -> bool:
    """Whether the mount records ownership. On a noowners mount chown is a silent
    no-op, so a home directory would stay root-owned however carefully it is set."""
    result = subprocess.run(["/sbin/mount"], capture_output=True, text=True, check=False)
    for line in result.stdout.splitlines():
        # "/dev/disk9s1 on /Volumes/Data (apfs, local, noowners)". A substring test
        # for " on /Volumes/Data " also matched "/Volumes/Data Backup" lines, so a
        # second volume on the host could answer for this one — and every chown
        # afterwards would be a silent no-op.
        device, _, rest = line.partition(" on ")
        path, _, options = rest.partition(" (")
        if not device or path != str(mountpoint):
            continue
        return "noowners" not in options.rstrip(")").split(", ")
    return False


def mount(volume: str) -> Path:
    result = subprocess.run(  # noqa: S603
        ["/usr/sbin/diskutil", "mount", volume],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise PatchError(f"could not mount {volume}: {(result.stderr or result.stdout).strip()}")
    info = subprocess.run(  # noqa: S603
        ["/usr/sbin/diskutil", "info", "-plist", volume],
        capture_output=True,
        check=False,
    )
    if info.returncode != 0:
        raise PatchError(
            f"{volume} mounted but diskutil would not describe it: "
            f"{(info.stderr or b'').decode(errors='replace').strip()}"
        )
    try:
        described = plistlib.loads(info.stdout)
    except (plistlib.InvalidFileException, ValueError) as exc:
        raise PatchError(f"could not read diskutil's description of {volume}: {exc}") from None
    point = described.get("MountPoint") if isinstance(described, dict) else None
    if not point:
        raise PatchError(f"{volume} mounted but reported no mount point")
    mountpoint = Path(point)
    if not ownership_is_honoured(mountpoint):
        # Enabling it here rather than failing: hdiutil -owners on covers the normal
        # path, and this catches a volume that was already attached differently.
        subprocess.run(  # noqa: S603
            ["/usr/sbin/diskutil", "enableOwnership", volume],
            capture_output=True,
            check=False,
        )
        if not ownership_is_honoured(mountpoint):
            raise PatchError(
                f"{mountpoint} is mounted without ownership, so the guest's home would "
                "stay root-owned and it could not write its own preferences"
            )
    return mountpoint


def unmount(volume: str) -> None:
    subprocess.run(  # noqa: S603
        ["/usr/sbin/diskutil", "unmount", volume],
        capture_output=True,
        check=False,
    )


def prepare(
    disk: Path,
    account: Account,
    keyboard: Keyboard | None = None,
    keys: Keys | None = None,
) -> None:
    """Attach the guest's disk, patch its Data volume, and put it all back.

    Unwound in reverse even when the patch fails, so a half-finished run never
    leaves a disk image attached to the host.
    """
    device = attach(disk)
    volume = None
    try:
        volume = data_volume(device)
        patch(mount(volume), account, keyboard, keys)
    finally:
        if volume:
            unmount(volume)
        detach(device)


def main(argv: list[str]) -> int:
    """Run the patch as root, for just the step that needs it.

    cib invokes this with sudo rather than asking for the whole build to run as
    root: a multi-gigabyte download and a VM boot have no business being root.
    The password arrives on stdin, so it is not in the process list.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="cibpatch", description=__doc__)
    parser.add_argument("--disk", required=True, type=Path)
    parser.add_argument("--user", required=True)
    # Defaulted rather than required, so the patcher stays usable on its own; cib
    # always passes the host's layout.
    parser.add_argument("--keyboard-id", type=int, default=Keyboard.layout_id)
    parser.add_argument("--keyboard-name", default=Keyboard.name)
    # Paths rather than key material: an argument list is readable by every local
    # user for as long as the process runs.
    parser.add_argument("--authorized-key", type=Path)
    parser.add_argument("--host-key", type=Path)
    args = parser.parse_args(argv)
    password = sys.stdin.readline().rstrip("\n")
    if not password:
        raise SystemExit("error: no password on stdin")
    try:
        keys = Keys(
            authorized=args.authorized_key.read_text() if args.authorized_key else "",
            host_private=args.host_key.read_text() if args.host_key else "",
            host_public=(args.host_key.with_suffix(".pub").read_text() if args.host_key else ""),
        )
        prepare(
            args.disk,
            Account(name=args.user, password=password),
            Keyboard(layout_id=args.keyboard_id, name=args.keyboard_name),
            keys,
        )
    except PatchError as exc:
        raise SystemExit(f"error: {exc}") from None
    print("prepared")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
