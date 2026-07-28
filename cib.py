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
import re
import secrets
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

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
    wait_secs: int = field(default_factory=lambda: _env_int("CIB_WAIT_SECS", "120", 0))
    log_tail: str = field(default_factory=lambda: _env("CIB_LOG_TAIL", "200"))

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
    print("No login needed — accept the self-signed certificate once.")


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
    extra = ["-f"] if follow else ["--tail", cfg.log_tail]
    run(engine, "logs", *extra, cfg.name, check=False)


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
    cpus: int = field(default_factory=lambda: _env_int("CIB_VM_CPUS", "4"))
    memory: int = field(default_factory=lambda: _env_int("CIB_VM_MEMORY", "8192", 1024))
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


PACKER_TEMPLATE = Path(__file__).resolve().parent / "packer" / "chrome-vm.pkr.hcl"
# Where the generated guest password is kept, so it survives between commands and
# can be pasted rather than typed.
CREDENTIALS = Path.home() / ".config" / "chrome-in-a-box" / "vm-credentials"


def find_packer() -> str:
    path = shutil.which("packer")
    if not path:
        raise Failure(
            "packer is not on PATH — install it with: brew install hashicorp/tap/packer\n"
            "(the guest is built unattended, which is what packer drives)"
        )
    return path


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
    if vm_exists(tart, vm):
        print(f"{vm.name!r} already exists. 'cib vm up' to start it.")
        return
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


# Installs Chrome in the guest. Runs there, not here, so it is a shell script.
GUEST_INSTALL_CHROME = f"""set -eu
# Downloads land on the host: replace the guest's own Downloads folder with the
# shared one, so every app follows, not just Chrome.
if [ -d "{GUEST_SHARE}" ]; then
  if [ ! -L "$HOME/Downloads" ]; then
    rmdir "$HOME/Downloads" 2>/dev/null || mv "$HOME/Downloads" "$HOME/Downloads.local"
  fi
  ln -sfn "{GUEST_SHARE}" "$HOME/Downloads"
else
  echo "the shared downloads folder is not mounted; start the VM with 'cib vm up'" >&2
  exit 1
fi
if [ -d '/Applications/Google Chrome.app' ]; then
  echo 'Chrome is already installed'
else
  curl -fsSL -o /tmp/chrome.dmg \
    'https://dl.google.com/chrome/mac/universal/stable/GGRO/googlechrome.dmg'
  hdiutil attach -nobrowse -quiet /tmp/chrome.dmg -mountpoint /tmp/chrome-mount
  cp -R '/tmp/chrome-mount/Google Chrome.app' /Applications/
  hdiutil detach -quiet /tmp/chrome-mount
  rm -f /tmp/chrome.dmg
fi
'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' --version
"""

SSH_OPTIONS = [
    # The guest is recreated freely and reached by a changing address, so a
    # remembered host key would only ever be in the way.
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "ConnectTimeout=10",
]


def vm_ip(tart: str, vm: VmConfig, wait: str = "60") -> str:
    """Resolve the guest's address. Bridged guests get theirs from the real
    network, so the DHCP lease file the default resolver reads is empty."""
    resolver = "arp" if vm.net == "bridged" else "dhcp"
    result = run(
        tart, "ip", "--resolver", resolver, "--wait", wait, vm.name, check=False, capture=True
    )
    ip = result.stdout.strip()
    if result.returncode != 0 or not ip:
        raise Failure(
            f"could not work out the address of {vm.name!r} — is it running and past "
            "Setup Assistant? ('cib vm up')"
        )
    return ip


def ssh_command(vm: VmConfig, ip: str, script: str | None = None) -> list[str]:
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", vm.user):
        # A user starting with "-" would be read by ssh as an option.
        raise Failure(f"CIB_VM_USER is not a usable account name: {vm.user!r}")
    ssh = shutil.which("ssh")
    if not ssh:
        raise Failure("ssh is not on PATH")
    return [ssh, *SSH_OPTIONS, f"{vm.user}@{ip}", *([script] if script else [])]


def guest_ssh(vm: VmConfig, ip: str, script: str | None = None) -> int:
    return subprocess.run(ssh_command(vm, ip, script), check=False).returncode  # noqa: S603


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
            f"could not open a shell on {vm.user}@{ip} — turn on System Settings > "
            "General > Sharing > Remote Login in the guest, and set CIB_VM_USER if "
            "the account is not called 'admin'"
        )


def cmd_vm_setup(tart: str, vm: VmConfig) -> None:
    """Finish the guest from here: everything after Setup Assistant."""
    ip = vm_ip(tart, vm)
    print(f"Installing Chrome on {vm.user}@{ip} ...")
    if guest_ssh(vm, ip, GUEST_INSTALL_CHROME) != 0:
        raise Failure(
            f"the guest at {ip} refused the connection or the install failed — turn on "
            "System Settings > General > Sharing > Remote Login in the guest, and set "
            "CIB_VM_USER if the account is not called 'admin'"
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
    print("Deleted." if result.returncode == 0 else f"Nothing to delete ({vm.name!r} not found).")


VM_ACTIONS = {
    "create": cmd_vm_create,
    "up": cmd_vm_up,
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
  up       start it; a window opens
  setup    install Chrome in the guest over SSH (needs Remote Login on in it)
  ssh      open a shell in the guest
  ip       print the guest's address
  password print the generated guest account password (copy it, do not retype it)

Downloads in the guest land in ~/Downloads/chrome-vm on the host (CIB_VM_SHARE).
  down     stop it
  status   list VMs and their state
  delete   delete the VM and everything in it (asks first)

`create` is unattended: it installs macOS, drives Setup Assistant, sets the Swiss
keyboard layout and installs Chrome, using a generated account password you never
have to type. Two things stay manual afterwards, because Apple makes them
interactive on purpose: signing in to the Apple Account, and turning on iCloud
Keychain."""


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
            "       CIB_VM_NET, CIB_VM_INTERFACE, CIB_VM_USER"
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
            VM_ACTIONS[args.action](find_tart(), VmConfig())
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
