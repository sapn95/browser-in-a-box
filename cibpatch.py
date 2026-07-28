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
    full_name: str = "chrome-in-a-box"
    uid: int = UID
    gid: int = GID


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


def write_plist(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        plistlib.dump(data, handle, fmt=plistlib.FMT_BINARY)


def read_plist(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return plistlib.load(handle)
    except (plistlib.InvalidFileException, OSError):
        return {}


def add_to_group(root: Path, group: str, account: Account, guid: str) -> None:
    """Append the account to a dslocal group, keeping whatever is already there.

    Both lists have to be written: "users" holds short names, "groupmembers" holds
    the *user's* GUID. Writing only one leaves the membership half-recorded, and
    macOS believes whichever it consults first.
    """
    path = root / f"private/var/db/dslocal/nodes/Default/groups/{group}.plist"
    record = read_plist(path)
    if not record:
        raise PatchError(f"the guest has no {group} group at {path}")
    for key, member in (("users", account.name), ("groupmembers", guid)):
        members = list(record.get(key, []))
        if member not in members:
            members.append(member)
        record[key] = members
    write_plist(path, record)


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
    write_plist(root / "Library/Preferences/com.apple.SetupAssistant.plist", seen)
    write_plist(
        root / f"Users/{account.name}/Library/Preferences/com.apple.SetupAssistant.plist", seen
    )
    # Accounts created later inherit the template, and would otherwise see it again.
    template = root / "Library/User Template/.skipbuddy"
    template.parent.mkdir(parents=True, exist_ok=True)
    template.touch()


def enable_autologin(root: Path, account: Account) -> None:
    path = root / "Library/Preferences/com.apple.loginwindow.plist"
    record = read_plist(path)
    record["autoLoginUser"] = account.name
    record["autoLoginUserUID"] = account.uid
    # A FirstLogins entry re-launches Setup Assistant at the next graphical login
    # even with .AppleSetupDone present, so it has to go.
    record.pop("AccountInfo", None)
    write_plist(path, record)
    kc = root / "private/etc/kcpassword"
    kc.parent.mkdir(parents=True, exist_ok=True)
    kc.write_bytes(kcpassword(account.password))
    kc.chmod(0o600)


def enable_remote_login(root: Path) -> None:
    """Turn on sshd the way launchd records it, so cib can take over by SSH."""
    path = root / "private/var/db/com.apple.xpc.launchd/disabled.plist"
    record = read_plist(path)
    record["com.openssh.sshd"] = False
    write_plist(path, record)


def create_account(root: Path, account: Account) -> None:
    users = root / "private/var/db/dslocal/nodes/Default/users"
    if not users.is_dir():
        raise PatchError(
            f"{users} is missing — is this the guest's Data volume, and has the guest "
            "been booted once so its first-boot state exists?"
        )
    guid = str(uuid.uuid4()).upper()
    write_plist(users / f"{account.name}.plist", user_record(account, guid))
    for group in ("admin", "staff"):
        add_to_group(root, group, account, guid)
    home = root / f"Users/{account.name}"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".CFUserTextEncoding").write_text("0:0")


def own_home(root: Path, account: Account) -> None:
    """Give the account its home, without ever following a link.

    The guest is where untrusted browsing happens, and this runs as root on the
    host. A symlink stored in the guest resolves against the *host* filesystem, so
    following one here would hand host paths to the guest: `ln -s / ~` inside the
    guest would otherwise chown the host's root filesystem on the next prepare.
    """
    home = root / f"Users/{account.name}"
    if home.is_symlink():
        raise PatchError(f"{home} is a symlink; refusing to own a link's target")
    os.chown(home, account.uid, account.gid, follow_symlinks=False)
    for parent, dirs, files in os.walk(home, followlinks=False):
        for name in dirs + files:
            os.chown(Path(parent) / name, account.uid, account.gid, follow_symlinks=False)


def mark_setup_done(root: Path) -> None:
    marker = root / "private/var/db/.AppleSetupDone"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()
    os.chown(marker, 0, 0)
    marker.chmod(0o644)


def patch(root: Path, account: Account) -> None:
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
    if not (root / "private/var/db").is_dir():
        raise PatchError(f"{root} does not look like a macOS Data volume")
    create_account(root, account)
    mark_setup_done(root)
    suppress_setup_assistant(root, account)
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
    """
    result = subprocess.run(
        ["/usr/sbin/diskutil", "apfs", "list", "-plist"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise PatchError("could not list the APFS containers on this host")
    ours = Path(device).name  # e.g. "disk10"
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
        raise PatchError(
            f"the container on {device} has no Data volume; it has "
            + ", ".join(v.get("Name", "?") for v in container.get("Volumes", []))
        )
    raise PatchError(f"nothing on this host has {device} as its physical store")


def ownership_is_honoured(mountpoint: Path) -> bool:
    """Whether the mount records ownership. On a noowners mount chown is a silent
    no-op, so a home directory would stay root-owned however carefully it is set."""
    result = subprocess.run(["/sbin/mount"], capture_output=True, text=True, check=False)
    for line in result.stdout.splitlines():
        if f" on {mountpoint} " in line:
            return "noowners" not in line
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
    point = plistlib.loads(info.stdout).get("MountPoint")
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


def prepare(disk: Path, account: Account) -> None:
    """Attach the guest's disk, patch its Data volume, and put it all back.

    Unwound in reverse even when the patch fails, so a half-finished run never
    leaves a disk image attached to the host.
    """
    device = attach(disk)
    volume = None
    try:
        volume = data_volume(device)
        patch(mount(volume), account)
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
    args = parser.parse_args(argv)
    password = sys.stdin.readline().rstrip("\n")
    if not password:
        raise SystemExit("error: no password on stdin")
    try:
        prepare(args.disk, Account(name=args.user, password=password))
    except PatchError as exc:
        raise SystemExit(f"error: {exc}") from None
    print("prepared")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
