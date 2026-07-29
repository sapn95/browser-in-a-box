#!/usr/bin/env python3
"""chrome-in-a-box — a second Google Chrome that your machine's policy does not manage.

Every command says which of the two variants it acts on, because they are not
interchangeable:

  cib box ...   A Linux container running Chrome behind KasmVNC, which you use from
                a tab in your own browser at https://localhost:6901. Needs podman or
                docker, starts in seconds, and runs anywhere. Google account sync and
                the Google Password Manager work. The host keychain does not reach
                into it, so Touch ID and iCloud Keychain passkeys are unavailable.

  cib vm ...    A macOS guest VM on Apple silicon, driven by tart, which you use in
                its own window. Needs ~40 GB and minutes to build. It is a real macOS
                install signed into your Apple Account, so iCloud Keychain and its
                passkeys work — but it has no Secure Enclave, so passkeys ask for the
                account password instead of Touch ID.

Neither variant can use a hardware security key: no USB is passed through.

Single file, standard library only: no venv, no pip install.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn
from xml.parsers.expat import ExpatError

__version__ = "1.4.0"

# renovate: datasource=docker depName=kasmweb/chrome
DEFAULT_IMAGE = "docker.io/kasmweb/chrome:1.19.0"

# KasmVNC ships fixed modes only up to this size; anything larger silently falls
# back to 1024x768, and this image ignores VNC_RESOLUTION entirely, so the mode is
# applied with xrandr after boot.
MAX_WIDTH, MAX_HEIGHT = 1920, 1200
# KasmVNC refuses to start with a shorter password, even though the login prompt
# is disabled with DisableBasicAuth.
MIN_PASSWORD_LEN = 6
# DynamicQualityMax above 9 makes Xvnc exit with a fatal error.
VNC_OPTIONS = "-DisableBasicAuth=1 -DynamicQualityMin=8 -DynamicQualityMax=9 -DLP_ClipDelay=0"

# renovate: datasource=github-releases depName=cirruslabs/tart-guest-agent
GUEST_AGENT_VERSION = "0.11.0"

CHROME_BIN = "/opt/google/chrome/google-chrome"
PROFILE_DIR = "/home/kasm-user/.config/google-chrome"

# Clears a stale profile lock (which makes Chrome exit into a black desktop),
# applies the resolution, and starts Chrome if the image's own launch did not.
DESKTOP_SCRIPT = f"""
export DISPLAY=:1
if [ -n "$RES" ]; then
  xrandr -s "$RES" >/dev/null ||
    echo "could not set mode $RES (KasmVNC ships a fixed mode list)" >&2
fi
if ! pgrep chrome >/dev/null 2>&1; then
  rm -f {PROFILE_DIR}/Singleton*
  nohup {CHROME_BIN} --no-sandbox --start-maximized \
    --user-data-dir={PROFILE_DIR} >/tmp/chrome.log 2>&1 &
fi
"""


class Failure(Exception):
    """A problem worth reporting to the user without a traceback."""


def env_flag(name: str) -> bool:
    return os.environ.get(name, "0") == "1"


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: str, minimum: int = 1) -> int:
    """An integer setting. A bad value is the user's typo, not a crash."""
    raw = os.environ.get(name, default)
    try:
        value = int(raw)
    except ValueError:
        raise Failure(f"{name} must be a whole number, got {raw!r}") from None
    if value < minimum:
        raise Failure(f"{name} must be at least {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Config:
    # Read at instantiation, not at import, so the environment is always current.
    image: str = field(default_factory=lambda: _env("CIB_IMAGE", DEFAULT_IMAGE))
    name: str = field(default_factory=lambda: _env("CIB_NAME", "chrome-in-a-box"))
    volume: str = field(default_factory=lambda: _env("CIB_VOLUME", "chrome-in-a-box-profile"))
    port: int = field(default_factory=lambda: _env_int("CIB_PORT", "6901"))
    # Empty means "follow the browser window": KasmVNC resizes the desktop to the
    # client, which is what ?resize=remote asks for. Pinning a mode as well would
    # fight it, so a fixed size is opt-in via CIB_RESOLUTION.
    resolution: str = field(default_factory=lambda: _env("CIB_RESOLUTION", ""))
    password: str = field(default_factory=lambda: _env("CIB_PASSWORD", "chromeinabox"))
    # At least 1: the deadline is checked before the first probe, so 0 gives the UI
    # no chance at all and every `box up` reports a timeout.
    wait_secs: int = field(default_factory=lambda: _env_int("CIB_WAIT_SECS", "120"))
    # A number, not a string: it is handed to the engine, which rejects anything
    # else with its own error rather than ours.
    log_tail: int = field(default_factory=lambda: _env_int("CIB_LOG_TAIL", "200", 0))

    @property
    def url(self) -> str:
        return f"https://localhost:{self.port}/?resize=remote"

    def check(self) -> None:
        """Reject the settings that are known to kill the container, with an
        explanation, rather than failing obscurely minutes later."""
        if len(self.password) < MIN_PASSWORD_LEN:
            raise Failure(
                f"CIB_PASSWORD must be at least {MIN_PASSWORD_LEN} characters; "
                "KasmVNC refuses to start with a shorter one"
            )
        if not self.resolution:
            return
        try:
            width, height = (int(part) for part in self.resolution.lower().split("x"))
        except ValueError:
            raise Failure(
                f"CIB_RESOLUTION must look like 1920x1200, got {self.resolution!r}"
            ) from None
        if width < 1 or height < 1:
            raise Failure(f"CIB_RESOLUTION must be positive, got {self.resolution}")
        if width > MAX_WIDTH or height > MAX_HEIGHT:
            raise Failure(
                f"CIB_RESOLUTION {self.resolution} exceeds the modes KasmVNC ships "
                f"(max {MAX_WIDTH}x{MAX_HEIGHT}); larger values silently fall back to 1024x768"
            )


def find_engine() -> str:
    """Return the container engine to use."""
    preferred = os.environ.get("CIB_ENGINE")
    if preferred:
        path = shutil.which(preferred)
        if not path:
            raise Failure(f"CIB_ENGINE={preferred} is not on PATH")
        return path
    for candidate in ("podman", "docker"):
        path = shutil.which(candidate)
        if path:
            return path
    raise Failure("need podman or docker on PATH")


def run(
    engine: str,
    *args: str,
    check: bool = True,
    capture: bool = False,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    # Argument list is built here and never passed through a shell.
    return subprocess.run(  # noqa: S603
        [engine, *args],
        check=check,
        capture_output=capture,
        text=True,
        env=env,
    )


def container_running(engine: str, cfg: Config) -> bool:
    result = run(engine, "inspect", "-f", "{{.State.Running}}", cfg.name, check=False, capture=True)
    return result.returncode == 0 and result.stdout.strip() == "true"


def ui_status(cfg: Config) -> int | None:
    """HTTP status of the web UI, or None if it is not reachable yet.

    The UI answers over HTTPS with a certificate the image generates at boot, so
    verification is deliberately off; this only ever talks to localhost.
    """
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    try:
        # Fixed https://localhost URL, never user input.
        with urllib.request.urlopen(
            f"https://localhost:{cfg.port}/", timeout=5, context=context
        ) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (urllib.error.URLError, OSError, ssl.SSLError, TimeoutError):
        return None


def ui_is_up(cfg: Config) -> bool:
    return ui_status(cfg) == 200


def wait_for_ui(engine: str, cfg: Config) -> None:
    deadline = time.monotonic() + cfg.wait_secs
    while time.monotonic() < deadline:
        status = ui_status(cfg)
        if status == 200:
            return
        if status == 401:
            raise Failure(
                "the web UI is asking for a login — DisableBasicAuth is not taking "
                "effect, check VNCOPTIONS"
            )
        # A container that died at boot would otherwise be reported as "slow".
        if not container_running(engine, cfg):
            logs = run(engine, "logs", "--tail", "50", cfg.name, check=False, capture=True)
            sys.stderr.write(logs.stdout + logs.stderr)
            raise Failure("the container exited during boot (log above)")
        time.sleep(2)
    raise Failure(f"the web UI did not come up within {cfg.wait_secs}s; check 'cib box logs'")


def ensure_desktop(engine: str, cfg: Config) -> bool:
    """Apply the resolution and make sure Chrome is running. Returns False and
    warns on trouble, rather than failing the whole command."""
    result = run(
        engine,
        "exec",
        "-e",
        # xrandr -s reads anything but lowercase <int>x<int> as a mode index, and
        # check() accepts "1280X800" and "1280 x 800".
        f"RES={''.join(cfg.resolution.lower().split())}",
        cfg.name,
        "bash",
        "-c",
        DESKTOP_SCRIPT,
        check=False,
        capture=True,
    )
    noise = (result.stdout + result.stderr).strip()
    if result.returncode == 0 and not noise:
        return True
    print(
        f"warning: desktop setup incomplete (rc={result.returncode})\n{noise}",
        file=sys.stderr,
    )
    return False


def cmd_up(engine: str, cfg: Config) -> None:
    cfg.check()
    if container_running(engine, cfg) and not env_flag("CIB_FORCE") and ui_is_up(cfg):
        ensure_desktop(engine, cfg)  # still re-applies the mode and revives Chrome
        print(f"Already running. Open {cfg.url}")
        return

    run(engine, "rm", "-f", cfg.name, check=False, capture=True)
    print("Starting Google Chrome (amd64 image; emulated on Apple Silicon) ...")
    run(
        engine,
        "run",
        "-d",
        "--name",
        cfg.name,
        "--platform",
        "linux/amd64",
        # Load-bearing: the image's startup script waits forever for a veth, and
        # rootless podman's default netns has none. A no-op on docker and rootful
        # podman, where bridge is already the default.
        "--network",
        "bridge",
        "--shm-size=2g",
        "--security-opt",
        "seccomp=unconfined",
        "-p",
        f"127.0.0.1:{cfg.port}:6901",
        "-e",
        f"VNC_PW={cfg.password}",
        "-e",
        f"VNCOPTIONS={VNC_OPTIONS}",
        "-v",
        f"{cfg.volume}:/home/kasm-user",
        cfg.image,
        capture=True,
    )
    print("Waiting for the desktop ...")
    wait_for_ui(engine, cfg)
    for attempt in range(3):
        if ensure_desktop(engine, cfg):
            break
        if attempt < 2:
            time.sleep(3)
    print()
    print(f"Ready. Open {cfg.url}")
    print("No login needed — accept the self-signed certificate the browser warns about.")


def cmd_down(engine: str, cfg: Config) -> None:
    # podman's `rm -f` exits 0 for a container that never existed, so its exit code
    # cannot tell "removed it" from "there was nothing there".
    present = run(
        engine, "container", "inspect", "-f", "{{.Id}}", cfg.name, check=False, capture=True
    )
    if present.returncode != 0:
        print("Not running.")
        return
    run(engine, "rm", "-f", cfg.name, check=False, capture=True)
    print(f"Stopped. The browser profile is kept in volume {cfg.volume!r}.")


def cmd_reset(engine: str, cfg: Config) -> None:
    prompt = f"Delete the browser profile (volume {cfg.volume})? All logins are lost. [y/N] "
    try:
        answer = input(prompt)
    except EOFError:
        answer = ""
    if not answer.lower().startswith("y"):
        print("Cancelled.")
        return
    run(engine, "rm", "-f", cfg.name, check=False, capture=True)
    result = run(engine, "volume", "rm", cfg.volume, check=False, capture=True)
    if result.returncode == 0:
        print("Profile deleted.")
    else:
        print(f"Nothing to delete (volume {cfg.volume!r} is not present or still in use).")


def cmd_open(engine: str, cfg: Config) -> None:
    opener = shutil.which("open") or shutil.which("xdg-open")
    # opener comes from shutil.which, the URL is built from our own config.
    if opener and subprocess.run([opener, cfg.url], check=False).returncode == 0:  # noqa: S603
        return
    print(f"Open {cfg.url}")


def cmd_status(engine: str, cfg: Config) -> None:
    run(
        engine,
        "ps",
        "-a",
        "--filter",
        f"name={cfg.name}",
        "--format",
        "table {{.Names}}\t{{.Status}}\t{{.Ports}}",
    )


def cmd_logs(engine: str, cfg: Config, follow: bool = False) -> None:
    # Following by default would hang every non-interactive caller, including CI.
    extra = ["-f"] if follow else ["--tail", str(cfg.log_tail)]
    result = run(engine, "logs", *extra, cfg.name, check=False)
    # Every other box command reports a failure as one. This used to exit 0 whatever
    # the engine did, so `cib box logs > out.txt || handle` never fired and out.txt
    # was silently empty.
    if result.returncode != 0:
        raise Failure(f"could not read the logs of {cfg.name!r} — is it there? ('cib box status')")


def cmd_shell(engine: str, cfg: Config) -> None:
    run(engine, "exec", "-it", cfg.name, "bash", check=False)


def cmd_engine(engine: str, cfg: Config) -> None:
    print(engine)


# --- the macOS VM variant -----------------------------------------------------
#
# A macOS guest is not enrolled in the host's MDM, so its Chrome is policy-free,
# and since macOS 15 Apple supports signing a VM into an Apple Account — which
# brings iCloud Keychain, and therefore passkeys, with it. It has no Secure
# Enclave and no Touch ID, so passkey use falls back to the account password.


@dataclass(frozen=True)
class VmConfig:
    name: str = field(default_factory=lambda: _env("CIB_VM_NAME", "chrome-vm"))
    # Floors are what macOS itself needs, not what the type allows: a guest given
    # one core or 1 GB fails somewhere in the middle of a half-hour install.
    cpus: int = field(default_factory=lambda: _env_int("CIB_VM_CPUS", "4", 2))
    memory: int = field(default_factory=lambda: _env_int("CIB_VM_MEMORY", "8192", 4096))
    disk: int = field(default_factory=lambda: _env_int("CIB_VM_DISK", "100", 20))
    display: str = field(default_factory=lambda: _env("CIB_VM_DISPLAY", "1920x1200"))
    # "bridged" gives the guest an address from the real network, so it inherits a
    # working DNS resolver. tart's default "shared" mode hands out the vmnet gateway
    # as resolver, and on some hosts that gateway does not answer DNS at all — the
    # guest then has an address but cannot resolve anything, which reads as "not
    # connected to the Internet" in Setup Assistant.
    net: str = field(default_factory=lambda: _env("CIB_VM_NET", "bridged"))
    interface: str = field(default_factory=lambda: _env("CIB_VM_INTERFACE", "en0"))
    user: str = field(default_factory=lambda: _env("CIB_VM_USER", "admin"))
    # Shared with the guest, so downloads land on the host rather than inside a disk
    # image. A folder under ~/Downloads rather than ~/Downloads itself: the guest
    # gets what it needs and no more.
    share: str = field(default_factory=lambda: _env("CIB_VM_SHARE", "~/Downloads/chrome-vm"))

    def check(self) -> None:
        """Reject what would fail later, or quietly do the wrong thing."""
        # An empty share expands to the current directory, which would be shared
        # into the guest wholesale.
        if not self.share.strip():
            raise Failure("CIB_VM_SHARE is empty; unset it for the default, or give a path")
        # The box variant validates the same shape; the VM used to drop a typo
        # silently and open a window at whatever tart chose.
        if not re.fullmatch(r"\d+x\d+", self.display):
            raise Failure(f"CIB_VM_DISPLAY must look like 1920x1200, got {self.display!r}")

    # "latest" is what Apple is shipping today, which is what a new guest usually
    # wants — but it moves, so a rebuild is not reproducible unless it can be told
    # which installer to use (a URL or a path to an .ipsw).
    ipsw: str = field(default_factory=lambda: _env("CIB_VM_IPSW", "latest"))


PACKER_TEMPLATE = Path(__file__).resolve().parent / "packer" / "chrome-vm.pkr.hcl"
# Where the generated guest password is kept, so it survives between commands and
# can be pasted rather than typed.
CREDENTIALS = Path.home() / ".config" / "chrome-in-a-box" / "vm-credentials"
# The key cib logs in with, the host key it plants in the guest so it can recognise
# it again, and the known_hosts holding that host key.
VM_KEY = CREDENTIALS.parent / "vm-key"
VM_HOST_KEY = CREDENTIALS.parent / "vm-host-key"
KNOWN_HOSTS = CREDENTIALS.parent / "vm-known-hosts"


PATCHER = Path(__file__).resolve().parent / "cibpatch.py"


def find_patcher() -> Path:
    """The offline path spawns cibpatch.py rather than importing it, so nothing
    but this check knows whether it is there."""
    if not PATCHER.exists():
        raise Failure(
            f"the patcher is missing at {PATCHER} — the offline path needs it beside "
            "cib. Either run cib.py from the repository, or fall back to driving Setup "
            "Assistant: 'cib vm delete' first, then re-run with CIB_VM_PACKER=1 "
            "(without the delete, 'vm create' only reports that the VM exists)."
        )
    return PATCHER


def find_packer() -> str:
    path = shutil.which("packer")
    if not path:
        raise Failure(
            "packer is not on PATH — install it with: brew install hashicorp/tap/packer\n"
            "(the guest is built unattended, which is what packer drives)"
        )
    return path


# What macOS installs with, and the fallback when the host's own layout cannot be
# read. 0 is the U.S. layout; HIToolbox identifies layouts by this number.
DEFAULT_KEYBOARD = (0, "U.S.")

# What the packer template carried before it was told to follow the host.
DEFAULT_TIME_ZONE = ("Europe/Zurich", "Zurich")


def host_keyboard_layout() -> tuple[int, str]:
    """The layout this host types in, to give the guest the same one.

    The generated password is pasted, but everything typed in the guest afterwards
    is typed by hand, and a guest on a different layout moves the punctuation.

    Read through `defaults export` rather than straight from the plist: cfprefsd
    caches preferences and flushes them on its own schedule, so the file on disk
    can be stale or missing while the setting is live.
    """
    # Under sudo the euid is root, whose HIToolbox domain is empty; asking as the
    # invoking user is the difference between the host's layout and a silent U.S.
    owner = os.environ.get("SUDO_USER")
    prefix = ["/usr/bin/sudo", "-n", "-u", owner] if os.geteuid() == 0 and owner else []
    result = run(
        *prefix,
        "/usr/bin/defaults",
        "export",
        "com.apple.HIToolbox",
        "-",
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return DEFAULT_KEYBOARD
    try:
        prefs = plistlib.loads((result.stdout or "").encode())
    # Well-formed-looking but broken XML raises ExpatError, which is neither of the
    # other two: a guest layout is not worth crashing a 40-minute build over.
    except (plistlib.InvalidFileException, ValueError, ExpatError):
        return DEFAULT_KEYBOARD
    # Selected first: enabled can hold several, and only one of them is in use.
    for key in ("AppleSelectedInputSources", "AppleEnabledInputSources"):
        for source in prefs.get(key) or []:
            # Input *methods* (Press-And-Hold, handwriting) sit in the same lists
            # and carry no layout, so they are skipped rather than misread.
            if not isinstance(source, dict):
                continue
            layout_id, name = source.get("KeyboardLayout ID"), source.get("KeyboardLayout Name")
            if isinstance(layout_id, int) and isinstance(name, str) and name:
                return layout_id, name
    return DEFAULT_KEYBOARD


SUDO_MESSAGE = (
    "this step needs sudo and there is no cached credential to use.\n"
    "Run 'sudo -v', then re-run — cib never prompts for a password itself, so\n"
    "a credential cached beforehand is the only way in, whatever it is run from."
)


def sudo_is_cached() -> bool:
    """Whether sudo would run without prompting.

    sudo prompts on its own tty, so it can ask for nothing when cib runs detached;
    -n turns that into an exit code instead of a hang.
    """
    probe = subprocess.run(["/usr/bin/sudo", "-n", "true"], check=False, capture_output=True)
    return probe.returncode == 0


class SudoKeepalive:
    """Holds the sudo credential open across a build longer than sudo's timeout.

    The patch step is the last thing `vm create` does and the only thing needing
    root, but sudo forgets a credential after about five minutes and the build
    before it takes thirty to sixty. So a credential cached at the start had always
    expired by the time it was used, and every unattended build ended by refusing
    to do its final step.
    """

    def __init__(self, interval: int = 60) -> None:
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> SudoKeepalive:
        self._thread = threading.Thread(target=self._refresh, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _refresh(self) -> None:
        while not self._stop.wait(self.interval):
            # -n, so a lapsed credential is reported by the step that needs it
            # rather than by a background thread nobody is watching.
            subprocess.run(["/usr/bin/sudo", "-n", "-v"], check=False, capture_output=True)


def host_time_zone() -> tuple[str, str]:
    """The host's Olson time zone and its city, for the guest to match.

    Read from the /etc/localtime link rather than `systemsetup -gettimezone`, which
    needs root. Returns the default the packer template already carried when the
    link says nothing usable.
    """
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return DEFAULT_TIME_ZONE
    _, _, zone = target.partition("zoneinfo/")
    if not zone:
        return DEFAULT_TIME_ZONE
    # "Europe/Zurich" and "America/Argentina/Buenos_Aires" both end in the city, and
    # Setup Assistant's field searches for a city rather than an Olson id. A
    # single-component zone like "UTC" is its own city, not a reason to fall back.
    return zone, zone.rsplit("/", 1)[-1].replace("_", " ")


def _keygen(path: Path, comment: str) -> None:
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        raise Failure("ssh-keygen is not on PATH, and the guest is reached over ssh")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for stale in (path, path.with_suffix(".pub")):
        stale.unlink(missing_ok=True)
    run(keygen, "-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(path), check=False)
    if not (path.exists() and path.with_suffix(".pub").exists()):
        raise Failure(f"ssh-keygen did not produce a key pair at {path}")


def ensure_vm_keys() -> None:
    """Create the two key pairs the guest is reached with, once.

    Login is by key rather than by password: sshd runs `vm setup` non-interactively,
    so a password login would have to be typed at every command — and the script it
    runs carries the guest's own password for sudo, which should not travel to a
    peer that has not been identified yet.

    The guest's *host* key is generated here and planted in the guest rather than
    left for it to make on first boot, because a key that is only learned on first
    connection cannot be checked on that connection. Planting it means the very
    first connection is verified.
    """
    if not (VM_KEY.exists() and VM_KEY.with_suffix(".pub").exists()):
        _keygen(VM_KEY, "cib")
    if not (VM_HOST_KEY.exists() and VM_HOST_KEY.with_suffix(".pub").exists()):
        _keygen(VM_HOST_KEY, "cib-guest")
    # A wildcard host pattern on purpose: the guest's address changes with every
    # lease, and this file is used for nothing but connections to that one guest.
    KNOWN_HOSTS.write_text("* " + VM_HOST_KEY.with_suffix(".pub").read_text().strip() + "\n")
    KNOWN_HOSTS.chmod(0o600)


def guest_password(create: bool = False) -> str:
    """The guest account password. Generated once, then remembered — you paste it,
    you never type it."""
    if CREDENTIALS.exists():
        saved = CREDENTIALS.read_text().strip()
        if saved:
            return saved
        # An interrupted write must not become an empty password.
        CREDENTIALS.unlink()
    if not create:
        raise Failure(f"no saved guest password at {CREDENTIALS} — build the VM first")
    # Typed into the guest as keystrokes during the build, so it must not contain a
    # character whose key moves between the US and Swiss German layouts: -, _, y, z
    # and their capitals all do.
    alphabet = "abcdefghijklmnopqrstuvwxABCDEFGHIJKLMNOPQRSTUVWX0123456789"
    password = "".join(secrets.choice(alphabet) for _ in range(24))
    CREDENTIALS.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Created 0600 rather than chmod-ed afterwards, so it is never briefly readable.
    with os.fdopen(os.open(CREDENTIALS, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write(password + "\n")
    return password


def find_tart() -> str:
    """Return the tart binary, after checking the VM variant can run at all."""
    if platform.system() != "Darwin":
        raise Failure("the vm variant needs macOS; use the container variant instead")
    if platform.machine() != "arm64":
        raise Failure("the vm variant needs Apple silicon; use the container variant instead")
    path = shutil.which("tart")
    if not path:
        raise Failure("tart is not on PATH — install it with: brew install cirruslabs/cli/tart")
    return path


def vm_running(tart: str, vm: VmConfig) -> bool:
    """tart exits non-zero on `run` for a VM that is already up. Without this the
    generic failure path would blame bridged networking for it."""
    result = run(tart, "list", "--format", "json", check=False, capture=True)
    if result.returncode != 0:
        return False
    try:
        entries = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    return any(e.get("Name") == vm.name and e.get("Running") for e in entries)


def vm_exists(tart: str, vm: VmConfig) -> bool:
    result = run(tart, "list", "--quiet", check=False, capture=True)
    return vm.name in result.stdout.split()


def cmd_vm_create(tart: str, vm: VmConfig) -> None:
    # Checked here rather than in the patch step at the end: a typo in CIB_VM_USER
    # used to cost the whole build before anyone mentioned it.
    validate_vm_user(vm.user)
    if vm_exists(tart, vm):
        # Not necessarily a finished VM: if preparation failed, this exists but still
        # has no account, and 'vm up' would land on Setup Assistant rather than a
        # desktop. Both ways out are named, because from here they look identical.
        print(
            f"{vm.name!r} already exists. 'cib vm up' to start it, or 'cib vm prepare' "
            "if its preparation did not finish ('cib vm delete' removes it)."
        )
        return
    if env_flag("CIB_VM_PACKER"):
        return _create_with_packer(tart, vm)
    return _create_offline(tart, vm)


def cmd_vm_prepare(tart: str, vm: VmConfig) -> None:
    """Run just the preparation step against an existing VM.

    Building the guest takes half an hour; if only the patch failed there is no
    reason to do it all again.
    """
    if not vm_exists(tart, vm):
        raise Failure(f"{vm.name!r} does not exist — run 'cib vm create' first")
    if vm_running(tart, vm):
        raise Failure(
            f"{vm.name!r} is running — 'cib vm down' first. Its disk cannot be patched "
            "while the guest has it open."
        )
    _prepare_guest(vm, guest_password())


def _create_offline(tart: str, vm: VmConfig) -> None:
    """Build the guest and prepare it by patching its disk, so Setup Assistant is
    never shown. Deterministic, unlike typing into it."""
    password = guest_password(create=True)
    firstboot = _env_int("CIB_VM_FIRSTBOOT_SECS", "180", 0)  # before anything is built
    # Checked before the multi-gigabyte download rather than after it: the patch
    # step is the only part that needs root, but finding that out at the end costs
    # the entire build.
    # Both checked before the multi-gigabyte download rather than after it: the
    # patch step is the only part that needs either, but finding out at the end
    # costs the entire build and there is no way to finish it afterwards.
    find_patcher()
    if not sudo_is_cached():
        raise Failure(f"{SUDO_MESSAGE}\nNothing has been downloaded yet.")
    # The credential is held open across the build: sudo forgets it after about
    # five minutes and the build takes thirty to sixty, so one cached at the start
    # was always gone by the time the patch step asked for it.
    with SudoKeepalive():
        print(f"Creating {vm.name!r} from a fresh macOS image ...")
        # Sized here, not afterwards: tart create installs macOS onto its default 50 GB
        # disk, and `tart set --disk-size` only grows the image file — the partitions and
        # the APFS container stay where the installer put them.
        run(tart, "create", f"--from-ipsw={vm.ipsw}", f"--disk-size={vm.disk}", vm.name)
        run(
            tart,
            "set",
            vm.name,
            "--cpu",
            str(vm.cpus),
            "--memory",
            str(vm.memory),
            "--display",
            vm.display,
            check=False,
        )
        # The guest has to boot once for its first-boot state to exist; there is nothing
        # to patch before that.
        print("Booting once so the guest lays down its first-boot state ...")
        boot = subprocess.Popen(  # noqa: S603
            [tart, "run", "--no-graphics", vm.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(firstboot)
        # A boot that never happened would otherwise be patched and called "Built".
        if boot.poll() is not None:
            raise Failure(
                f"the guest's first boot exited immediately (tart exit {boot.returncode})"
                + (f": {(boot.stderr.read() or '').strip()}" if boot.stderr else "")
            )
        run(tart, "stop", vm.name, check=False, capture=True)
        try:
            boot.wait(timeout=120)
        except subprocess.TimeoutExpired:
            # Otherwise the orphan keeps disk.img open, which is what we need next.
            boot.kill()
            boot.wait()
            raise Failure(
                f"{vm.name!r} did not shut down in time and was killed, so its disk was "
                "never patched. Check 'cib vm status' shows it stopped, then 'cib vm "
                "prepare' finishes it without building it again."
            ) from None

        _prepare_guest(vm, password)
    print()
    print(f"Built. The account is {vm.user!r}; 'cib vm password' prints its password.")
    print("Next:")
    print("  1. cib vm up          — boots straight to the desktop, no Setup Assistant.")
    print("                          It stays in the foreground, so run the rest from")
    print("                          a second terminal; Ctrl-C here stops the guest.")
    print("  2. cib vm setup       — installs Chrome, the clipboard agent and downloads")
    print("  3. sign in to your Apple Account, then turn on iCloud Keychain")


def _prepare_guest(vm: VmConfig, password: str) -> None:
    """Write what Setup Assistant would have produced onto the guest's disk.

    Split out so a failure here can be retried with 'cib vm prepare' instead of
    rebuilding a VM that took half an hour.
    """
    tart_home = Path(os.environ.get("TART_HOME") or Path.home() / ".tart")
    validate_vm_user(vm.user)
    disk = tart_home / "vms" / vm.name / "disk.img"
    if not disk.exists():
        raise Failure(f"the guest's disk is not where it was expected: {disk}")
    patcher = find_patcher()
    # sys.executable is the compiled binary itself under Nuitka, not an interpreter,
    # so it cannot be used to run a script. Find a real python instead.
    python = (
        sys.executable
        if Path(sys.executable).name.startswith("python")
        # /usr/bin/python3 first: this is handed to sudo, so a PATH entry that came
        # from anywhere else would be running as root.
        else ("/usr/bin/python3" if Path("/usr/bin/python3").exists() else shutil.which("python3"))
    )
    # Only this step needs root — writing the guest's user database and setting
    # ownership inside it. The download and the boot do not, so sudo is asked for
    # here rather than for the whole command. sudo prompts on its own tty, so it
    # cannot ask for anything when cib runs detached; check before trying.
    ensure_vm_keys()
    layout_id, layout_name = host_keyboard_layout()
    print(
        f"Preparing the guest without Setup Assistant, keyboard {layout_name} "
        "(this step needs sudo) ..."
    )
    if not sudo_is_cached():
        raise Failure(
            f"{SUDO_MESSAGE}\n"
            f"The VM {vm.name!r} is kept, so 'cib vm prepare' redoes only this step."
        )
    result = subprocess.run(  # noqa: S603
        [
            "/usr/bin/sudo",
            # -n as well as the probe: without it a credential that lapsed in
            # between would make sudo read the password off this pipe as its own,
            # and the patcher would then find nothing on stdin.
            "-n",
            python,
            str(patcher),
            "--disk",
            str(disk),
            "--user",
            vm.user,
            "--keyboard-id",
            str(layout_id),
            "--keyboard-name",
            layout_name,
            "--authorized-key",
            str(VM_KEY.with_suffix(".pub")),
            "--host-key",
            str(VM_HOST_KEY),
        ],
        input=password + "\n",
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise Failure(
            "preparing the guest failed (see above).\n"
            "'cib vm prepare' retries just this step. To fall back to driving Setup "
            "Assistant instead, 'cib vm delete' first and re-run with CIB_VM_PACKER=1."
        )


def _create_with_packer(tart: str, vm: VmConfig) -> None:
    packer = find_packer()
    password = guest_password(create=True)
    if not PACKER_TEMPLATE.exists():
        raise Failure(
            f"the build template is missing at {PACKER_TEMPLATE} — 'vm create' needs a "
            "checkout of the repository; the installed command cannot build a VM"
        )
    print(f"Building {vm.name!r} from a fresh macOS image, unattended.")
    run(packer, "init", str(PACKER_TEMPLATE))
    print("This takes a while: it installs macOS, drives Setup Assistant and adds Chrome.")
    layout_id, layout_name = host_keyboard_layout()
    zone, city = host_time_zone()
    # Built from a fresh installer on purpose: Apple only grants a VM an Apple
    # Account identity when it was created from a macOS 15+ one.
    # The password goes in the environment, not argv: a CalledProcessError prints the
    # command, and argv is readable by every local user for the whole build.
    run(
        packer,
        "build",
        "-var",
        f"vm_name={vm.name}",
        "-var",
        f"username={vm.user}",
        "-var",
        f"cpu_count={vm.cpus}",
        "-var",
        # Round up: 8000 MB is 8 GB of intent, and truncating would give the guest 7.
        f"memory_gb={-(-vm.memory // 1024)}",
        "-var",
        f"disk_size_gb={vm.disk}",
        # Same layout as the offline path: the fallback used to hardcode Swiss
        # German, so anyone else taking it got a guest typing someone else's
        # punctuation.
        "-var",
        f"keyboard_layout_id={layout_id}",
        "-var",
        f"keyboard_layout_name={layout_name}",
        "-var",
        f"timezone={zone}",
        "-var",
        f"timezone_city={city}",
        "-var",
        f"from_ipsw={vm.ipsw}",
        str(PACKER_TEMPLATE),
        env={**os.environ, "PKR_VAR_password": password},
    )
    run(tart, "set", vm.name, "--display", vm.display, check=False)
    print()
    print(f"Built. The account is {vm.user!r}; 'cib vm password' prints its password.")
    print("Next:")
    print("  1. cib vm up")
    print("  2. sign in to your Apple Account            (interactive: 2FA)")
    print("  3. System Settings > Apple Account > iCloud > turn on Passwords & Keychain")
    print("  4. cib vm setup    — points the guest's Downloads at the shared host folder")


# Where a tart directory share appears inside a macOS guest.
GUEST_SHARE = "/Volumes/My Shared Files/downloads"


def vm_run_args(vm: VmConfig) -> list[str]:
    share = Path(vm.share).expanduser()
    if ":" in str(share):
        raise Failure(f"CIB_VM_SHARE cannot contain a colon, tart uses it as a separator: {share}")
    try:
        share.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Failure(f"cannot use {share} as the shared downloads folder: {exc}") from None
    args = ["run", f"--dir=downloads:{share}"]
    if vm.net == "bridged":
        return [*args, f"--net-bridged={vm.interface}", vm.name]
    if vm.net == "host":
        return [*args, "--net-host", vm.name]
    if vm.net != "shared":
        raise Failure(f"CIB_VM_NET must be bridged, shared or host, got {vm.net!r}")
    return [*args, vm.name]


def cmd_vm_up(tart: str, vm: VmConfig) -> None:
    if not vm_exists(tart, vm):
        raise Failure(f"{vm.name!r} does not exist yet — run 'cib vm create' first")
    if vm_running(tart, vm):
        print(f"{vm.name!r} is already running.")
        return
    print(f"Starting {vm.name!r} (a window will open) ...")
    result = run(tart, *vm_run_args(vm), check=False)
    if result.returncode != 0 and vm.net != "bridged":
        raise Failure(f"the VM failed to start (exit code {result.returncode})")
    if result.returncode != 0 and vm.net == "bridged":
        raise Failure(
            f"bridged networking on {vm.interface!r} failed; list the usable interfaces with "
            f"'tart run --net-bridged=list {vm.name}', set CIB_VM_INTERFACE, or fall back to "
            "CIB_VM_NET=shared (whose DNS may not work on every host)"
        )


CHROME_APP = "/Applications/Google Chrome.app"
CHROME_EXE = f"{CHROME_APP}/Contents/MacOS/Google Chrome"


AGENT_BIN = "/usr/local/bin/tart-guest-agent"
AGENT_LABEL = "org.cirruslabs.tart-guest-agent"
AGENT_PLIST_PATH = f"/Library/LaunchAgents/{AGENT_LABEL}.plist"
# What cirruslabs' own image templates ship, pointed at where cib installs the
# binary. --run-agent, not --run-daemon: only the session agent sees the pasteboard.
AGENT_PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" \
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>{AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
      <string>{AGENT_BIN}</string>
      <string>--run-agent</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
  </dict>
</plist>"""


def guest_install_script(password: str) -> str:
    """Chrome, the clipboard agent and the shared Downloads folder, as a script the
    guest runs.

    The password is embedded in the script rather than passed as an argument,
    because the script is fed to ssh on stdin: nothing here reaches either host's
    process list. sudo is then handed it on a pipe — `sudo -S` reading from the
    script's own stdin would swallow the rest of the script.

    It is needed at all because sshd runs this non-interactively, so there is no
    tty for sudo to prompt on and no cached credential to fall back to. `sudo -n`
    used to be used here and could never succeed, which left the clipboard agent
    uninstalled on every guest built the default way.
    """
    return f"""set -eu
CIB_SUDO_PW={shlex.quote(password)}
# Scratch space under the account's own home rather than /tmp, which every user in
# the guest can write to: a staged Chrome.app sitting there could be swapped
# between the copy and the move into /Applications.
CIB_WORK="$HOME/.cache/cib"
rm -rf "$CIB_WORK"
mkdir -p "$CIB_WORK"
trap 'rm -rf "$CIB_WORK"' EXIT
sudo_pw() {{ printf '%s\\n' "$CIB_SUDO_PW" | sudo -S -p '' "$@"; }}
# Downloads land on the host: replace the guest's own Downloads folder with the
# shared one, so every app follows, not just Chrome.
if [ -d "{GUEST_SHARE}" ]; then
  # Three states, not two: the offline path creates the home itself, so Downloads
  # may not exist at all. -e is false for a dangling link, so a stale one is replaced.
  if [ -e "$HOME/Downloads" ] && [ ! -L "$HOME/Downloads" ]; then
    # A second run must not nest the backup inside the first one, and must not
    # overwrite whatever the first one saved.
    backup="$HOME/Downloads.local"
    n=1
    while [ -e "$backup" ] || [ -L "$backup" ]; do
      backup="$HOME/Downloads.local.$n"
      n=$((n + 1))
    done
    rmdir "$HOME/Downloads" 2>/dev/null || {{
      mv "$HOME/Downloads" "$backup"
      echo "kept the guest's own Downloads at $backup" >&2
    }}
  fi
  ln -sfn "{GUEST_SHARE}" "$HOME/Downloads"
else
  echo "the shared downloads folder is not mounted; start the VM with 'cib vm up'" >&2
  exit 1
fi
# Tested on the binary, not the bundle: an interrupted `cp -R` leaves a directory
# that exists but cannot run, and a directory test would call that "installed"
# for ever.
if [ -x {shlex.quote(CHROME_EXE)} ]; then
  echo 'Chrome is already installed'
else
  rm -rf {shlex.quote(CHROME_APP)}
  curl -fsSL -o "$CIB_WORK/chrome.dmg" \
    'https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg'
  hdiutil attach -nobrowse -quiet "$CIB_WORK/chrome.dmg" -mountpoint "$CIB_WORK/mount"
  # Copied aside and moved into place as the last step, so an interruption cannot
  # leave half a Chrome at the path everything else looks at.
  mkdir -p "$CIB_WORK/staging"
  cp -R "$CIB_WORK/mount/Google Chrome.app" "$CIB_WORK/staging/"
  hdiutil detach -quiet "$CIB_WORK/mount"
  mv "$CIB_WORK/staging/Google Chrome.app" {shlex.quote(CHROME_APP)}
fi
{shlex.quote(CHROME_EXE)} --version
if [ ! -x {AGENT_BIN} ]; then
  # Host/guest copy-paste needs an agent inside the guest. Without it the generated
  # password would have to be typed by hand at every passkey prompt, which is the
  # one thing generating it was meant to avoid.
  curl -fsSL -o "$CIB_WORK/agent.tar.gz" \
    "https://github.com/cirruslabs/tart-guest-agent/releases/download/v{GUEST_AGENT_VERSION}/tart-guest-agent-darwin-all.tar.gz"
  tar -xzf "$CIB_WORK/agent.tar.gz" -C "$CIB_WORK"
  sudo_pw install -m 0755 "$CIB_WORK/tart-guest-agent" {AGENT_BIN}
fi
# The agent has no self-install flag — it is started by launchd, from a plist. It
# runs as a LaunchAgent rather than a daemon on purpose: the pasteboard belongs to
# the logged-in session, and a root daemon cannot reach it.
printf '%s\n' {shlex.quote(AGENT_PLIST)} > "$CIB_WORK/agent.plist"
sudo_pw install -m 0644 -o root -g wheel "$CIB_WORK/agent.plist" {AGENT_PLIST_PATH}
# Already loaded from an earlier run, or not yet: neither is an error.
launchctl bootout "gui/$(id -u)/{AGENT_LABEL}" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$(id -u)" {AGENT_PLIST_PATH} >/dev/null 2>&1 || true
# Failing here rather than reporting success: without the agent there is no
# copy-paste, and the generated password would have to be typed by hand.
test -x {AGENT_BIN}
if ! launchctl print "gui/$(id -u)/{AGENT_LABEL}" >/dev/null 2>&1; then
  echo "the clipboard agent is installed but not running yet;" \
       "'cib vm down' then 'cib vm up' starts it at login" >&2
fi
"""


def ssh_options() -> list[str]:
    """How cib reaches the guest: by key, against a host key it planted itself.

    This used to be StrictHostKeyChecking=no with UserKnownHostsFile=/dev/null,
    which accepts any peer answering on the guest's address — and the script sent
    over that connection carries the guest's password.
    """
    return [
        "-i",
        str(VM_KEY),
        # Without this ssh offers every key the agent holds before ours, and a guest
        # that has forgotten our key would prompt for a password instead of failing.
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "PasswordAuthentication=no",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o",
        "LogLevel=ERROR",
        "-o",
        "ConnectTimeout=10",
    ]


# How long `tart ip` is given to see the guest on the network. A parameter until
# nothing ever passed one.
IP_WAIT_SECS = "60"


def vm_ip(tart: str, vm: VmConfig) -> str:
    """Resolve the guest's address. Bridged guests get theirs from the real
    network, so the DHCP lease file the default resolver reads is empty."""
    resolver = "arp" if vm.net == "bridged" else "dhcp"
    result = run(
        tart,
        "ip",
        "--resolver",
        resolver,
        "--wait",
        IP_WAIT_SECS,
        vm.name,
        check=False,
        capture=True,
    )
    ip = result.stdout.strip()
    if result.returncode != 0 or not ip:
        # Not "past Setup Assistant": the offline path never shows one, so naming it
        # here sent people looking for a screen that does not exist.
        detail = (result.stderr or "").strip()
        raise Failure(
            f"could not work out the address of {vm.name!r} after {IP_WAIT_SECS}s — is "
            f"it running? ('cib vm status', then 'cib vm up')" + (f"\n{detail}" if detail else "")
        )
    return ip


def validate_vm_user(name: str) -> str:
    """The account name reaches both ssh and a root-privileged patcher, so it is
    checked once, here. A name starting with "-" would be read by ssh as an option,
    and one containing ".." would escape the guest volume the patcher writes into."""
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name) or ".." in name:
        raise Failure(f"CIB_VM_USER is not a usable account name: {name!r}")
    return name


def ssh_command(vm: VmConfig, ip: str, script: str | None = None) -> list[str]:
    validate_vm_user(vm.user)
    ssh = shutil.which("ssh")
    if not ssh:
        raise Failure("ssh is not on PATH")
    # A script is read on stdin, not passed as an argument: `ssh host "<script>"`
    # puts the whole thing in this host's process list, and the guest password has
    # to travel inside it.
    return [ssh, *ssh_options(), f"{vm.user}@{ip}", *(["/bin/sh", "-s"] if script else [])]


def guest_ssh(vm: VmConfig, ip: str, script: str | None = None) -> int:
    # No script means an interactive shell, which needs this terminal's stdin.
    return subprocess.run(  # noqa: S603
        ssh_command(vm, ip, script), input=script, text=True, check=False
    ).returncode


def cmd_vm_password(tart: str, vm: VmConfig) -> None:
    print(guest_password())


def cmd_vm_ip(tart: str, vm: VmConfig) -> None:
    print(vm_ip(tart, vm))


def cmd_vm_ssh(tart: str, vm: VmConfig) -> None:
    ip = vm_ip(tart, vm)
    # ssh passes the remote shell's exit status through; 255 is ssh's own "could not
    # connect". Anything else just means the shell ended non-zero, which is normal.
    if guest_ssh(vm, ip) == 255:
        raise Failure(
            f"could not open a shell on {vm.user}@{ip}. The offline build turns Remote "
            "Login on and installs cib's key, so this usually means the guest was built "
            "another way, or CIB_VM_USER no longer matches the account it was built "
            f"with. 'cib vm prepare' re-installs the key ({VM_KEY.with_suffix('.pub')})."
        )


def cmd_vm_setup(tart: str, vm: VmConfig) -> None:
    """Finish the guest from here: everything after Setup Assistant."""
    ip = vm_ip(tart, vm)
    print(f"Installing Chrome on {vm.user}@{ip} ...")
    if guest_ssh(vm, ip, guest_install_script(guest_password())) != 0:
        raise Failure(
            f"installing Chrome on the guest at {ip} failed (see above). The offline "
            "build turns Remote Login on and installs cib's key; if the connection "
            "itself was refused, 'cib vm prepare' re-installs both."
        )
    print("Done. In the guest, sign Chrome into your Google account.")


def cmd_vm_down(tart: str, vm: VmConfig) -> None:
    result = run(tart, "stop", vm.name, check=False, capture=True)
    print("Stopped." if result.returncode == 0 else "Not running.")


def cmd_vm_status(tart: str, vm: VmConfig) -> None:
    run(tart, "list", check=False)


def cmd_vm_delete(tart: str, vm: VmConfig) -> None:
    try:
        answer = input(f"Delete the VM {vm.name} and everything in it? [y/N] ")
    except EOFError:
        answer = ""
    if not answer.lower().startswith("y"):
        print("Cancelled.")
        return
    result = run(tart, "delete", vm.name, check=False, capture=True)
    # The password and the keys belong to the VM that is gone. Left behind, the next
    # build silently reuses them — and 'cib vm password' keeps printing a password
    # for a guest that no longer exists.
    for leftover in (
        CREDENTIALS,
        VM_KEY,
        VM_KEY.with_suffix(".pub"),
        VM_HOST_KEY,
        VM_HOST_KEY.with_suffix(".pub"),
        KNOWN_HOSTS,
    ):
        leftover.unlink(missing_ok=True)
    print("Deleted." if result.returncode == 0 else f"Nothing to delete ({vm.name!r} not found).")


VM_ACTIONS = {
    "create": cmd_vm_create,
    "up": cmd_vm_up,
    "prepare": cmd_vm_prepare,
    "setup": cmd_vm_setup,
    "ssh": cmd_vm_ssh,
    "ip": cmd_vm_ip,
    "password": cmd_vm_password,
    "down": cmd_vm_down,
    "status": cmd_vm_status,
    "delete": cmd_vm_delete,
}


BOX_ACTIONS = {
    "up": cmd_up,
    "down": cmd_down,
    "open": cmd_open,
    "status": cmd_status,
    "logs": cmd_logs,
    "shell": cmd_shell,
    "engine": cmd_engine,
    "reset": cmd_reset,
}

BOX_HELP = """\
  up       start it and wait until Chrome is running (reuses a healthy container)
  down     stop and remove it; the browser profile is kept
  open     open the web UI in your browser
  status   show whether it is running
  logs     show the last log lines (-f follows instead)
  shell    open a shell inside it
  engine   print the container engine that will be used
  reset    delete the browser profile, losing every login (asks first)"""

VM_HELP = """\
  create   build the VM from a fresh macOS image (large download, one time)
  prepare  redo just the offline preparation on an already-built VM
  up       start it; a window opens
  setup    install Chrome and the clipboard agent in the guest, over SSH
  ssh      open a shell in the guest
  ip       print the guest's address
  password print the generated guest account password (copy it, do not retype it)
  down     stop it
  status   list VMs and their state
  delete   delete the VM and everything in it (asks first)

`create` never shows Setup Assistant. Instead of typing into it, it writes the
state Setup Assistant would have produced onto the guest's disk before its first
real boot: the account, autologin, Remote Login, and this host's keyboard layout.
That one step needs sudo on the host; nothing else does. The account password is
generated, so you never have to type it.

`cib vm up` stays in the foreground: run the steps after it from a second terminal,
because Ctrl-C there stops the guest.

Chrome is not part of `create` — 'cib vm setup' installs it and the clipboard
agent, over SSH. That connection is by key, not by password: the build generates
one and installs it, along with the guest's own host key, so cib can verify the
guest on the very first connection. Nothing asks you to type anything.

Downloads in the guest land in ~/Downloads/chrome-vm on the host (CIB_VM_SHARE).

Two things stay manual, because Apple makes them interactive on purpose: signing
in to the Apple Account, and turning on iCloud Keychain."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cib",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment overrides\n"
            "  box: CIB_PORT, CIB_RESOLUTION, CIB_WAIT_SECS, CIB_ENGINE, CIB_IMAGE,\n"
            "       CIB_NAME, CIB_VOLUME, CIB_PASSWORD, CIB_LOG_TAIL,\n"
            "       CIB_FORCE=1 to recreate a running container instead of reusing it\n"
            "  vm:  CIB_VM_NAME, CIB_VM_CPUS, CIB_VM_MEMORY, CIB_VM_DISK, CIB_VM_DISPLAY,\n"
            "       CIB_VM_NET, CIB_VM_INTERFACE, CIB_VM_USER, CIB_VM_SHARE,\n"
            "       CIB_VM_FIRSTBOOT_SECS, CIB_VM_IPSW to pin the macOS installer,\n"
            "       CIB_VM_PACKER=1 to drive Setup Assistant instead of patching the disk"
        ),
    )
    parser.add_argument("--version", action="version", version=f"cib {__version__}")
    sub = parser.add_subparsers(dest="variant", metavar="{box,vm}")

    box = sub.add_parser(
        "box",
        help="the Linux container, used from a browser tab (podman or docker)",
        description="The Linux container variant, used from a tab in your own browser.",
        epilog=BOX_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    box.add_argument("action", choices=sorted(BOX_ACTIONS), metavar="action")
    box.add_argument(
        "-f", "--follow", action="store_true", help="with logs: follow instead of printing"
    )

    vm = sub.add_parser(
        "vm",
        help="the macOS VM, used in its own window (Apple silicon; has iCloud Keychain)",
        description="The macOS guest VM variant, which has iCloud Keychain and its passkeys.",
        epilog=VM_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    vm.add_argument("action", choices=sorted(VM_ACTIONS), metavar="action")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not args.variant:
        parser.print_help(sys.stderr)
        return 2

    try:
        if args.variant == "vm":
            vm = VmConfig()
            vm.check()
            VM_ACTIONS[args.action](find_tart(), vm)
            return 0
        cfg = Config()
        engine = find_engine()
        if getattr(args, "follow", False) and args.action != "logs":
            raise Failure(f"-f/--follow only applies to logs, not {args.action}")
        if args.action == "logs":
            cmd_logs(engine, cfg, follow=args.follow)
        else:
            BOX_ACTIONS[args.action](engine, cfg)
    except Failure as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or "").strip()
        print(
            f"error: {' '.join(exc.cmd)} failed with exit code {exc.returncode}"
            + (f"\n{detail}" if detail else ""),
            file=sys.stderr,
        )
        return exc.returncode if exc.returncode > 0 else 1
    except KeyboardInterrupt:
        return 130
    return 0


def _entrypoint() -> NoReturn:
    sys.exit(main())


if __name__ == "__main__":
    _entrypoint()
