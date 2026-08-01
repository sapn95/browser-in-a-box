#!/usr/bin/env python3
"""browser-in-a-box — a second browser that your machine's policy does not manage.

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
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn
from urllib.parse import quote
from xml.parsers.expat import ExpatError

import cibbrowsers
import cibicon

__version__ = "2.0.0"


def chosen_browser() -> cibbrowsers.Browser:
    """Which browser goes in the box. The images live in cibbrowsers, one row each."""
    key = _env("CIB_BROWSER", cibbrowsers.DEFAULT_BROWSER).strip().lower()
    if key not in cibbrowsers.BROWSERS:
        raise Failure(
            f"CIB_BROWSER must be one of {', '.join(sorted(cibbrowsers.BROWSERS))}, got {key!r}"
        )
    return cibbrowsers.BROWSERS[key]


# KasmVNC ships fixed modes only up to this size; anything larger silently falls
# back to 1024x768, and this image ignores VNC_RESOLUTION entirely, so the mode is
# applied with xrandr after boot.
# The modes this image's Xvnc actually offers. "In range" is not the same as
# available: 1600x900 is smaller than the largest and still not there, and xrandr
# then leaves the desktop at 1024x768 while cib reported success.
KASM_MODES = (
    (1024, 768),
    (1280, 720),
    (1280, 800),
    (1280, 1024),
    (1366, 768),
    (1440, 900),
    (1600, 1200),
    (1680, 1050),
    (1920, 1080),
    (1920, 1200),
)
MAX_WIDTH, MAX_HEIGHT = max(w for w, _ in KASM_MODES), max(h for _, h in KASM_MODES)
# KasmVNC refuses to start with a shorter password, even though the login prompt
# is disabled with DisableBasicAuth.
MIN_PASSWORD_LEN = 6
# DynamicQualityMax above 9 makes Xvnc exit with a fatal error.
VNC_OPTIONS = "-DisableBasicAuth=1 -DynamicQualityMin=8 -DynamicQualityMax=9 -DLP_ClipDelay=0"

# renovate: datasource=github-releases depName=cirruslabs/tart-guest-agent
GUEST_AGENT_VERSION = "0.11.0"

# The container user, whose home holds the profile the volume keeps.
KASM_HOME = "/home/kasm-user"
# How long to give the browser to appear in the process table before calling the
# launch failed. A cold start on a fresh volume is a second or two; ten is slack
# for a loaded host, and it is only ever waited out when something is wrong.
LAUNCH_WAIT_SECS = 10


def desktop_script(browser: cibbrowsers.Browser) -> str:
    """What `box up` runs inside the container: apply the resolution, clear a stale
    profile lock (which makes the browser exit into a black desktop), and start the
    browser if the image's own launch did not.

    Per browser, all of it. This was Chrome's binary, Chrome's profile and Chrome's
    process name on all three images, so `CIB_BROWSER=firefox cib box up` reached a
    desktop with nothing on it — and said nothing, because `nohup ... &` is a
    success the moment the shell forks, whatever happens next.
    """
    profile = f"{KASM_HOME}/{browser.container_profile}"
    if browser.settings == "firefox":
        # Firefox takes neither --user-data-dir nor --start-maximized: the profile
        # comes from profiles.ini, and Kasm's own startup maximises the window with
        # wmctrl afterwards. Its stale lock is a pair of files inside the profile.
        launch = browser.container_bin
        locks = f"{profile}/*/.parentlock {profile}/*/lock"
    else:
        launch = f"{browser.container_bin} --no-sandbox --start-maximized --user-data-dir={profile}"
        locks = f"{profile}/Singleton*"
    return f"""
export DISPLAY=:1
CIB_LOG=/tmp/{browser.key}.log
if [ -n "$RES" ]; then
  xrandr -s "$RES" >/dev/null ||
    echo "could not set mode $RES (KasmVNC ships a fixed mode list)" >&2
fi
if ! pgrep -x {browser.container_process} >/dev/null 2>&1; then
  rm -f {locks}
  nohup {launch} >"$CIB_LOG" 2>&1 &
  # Wait for it rather than trust the fork: a binary that is not in this image at
  # all exits immediately, and without this that was indistinguishable from a
  # browser that started fine.
  for _ in $(seq {LAUNCH_WAIT_SECS * 2}); do
    pgrep -x {browser.container_process} >/dev/null 2>&1 && break
    sleep 0.5
  done
  if ! pgrep -x {browser.container_process} >/dev/null 2>&1; then
    echo "{browser.label} did not start; last lines of $CIB_LOG:" >&2
    tail -n 20 "$CIB_LOG" >&2
  fi
fi
"""


class Failure(Exception):
    """A problem worth reporting to the user without a traceback."""


def env_flag(name: str) -> bool:
    """A yes/no setting, from the environment or the settings file.

    Through _env rather than os.environ, for the reason _env_int already carries a
    note about: read directly, CIB_FORCE and CIB_VM_PACKER were parsed out of the
    settings file, stored, and then never looked at — the file appeared to work and
    those two keys quietly did nothing.

    "true" counts as well as "1", because yaml reads `packer: yes` as a boolean and
    load_config writes booleans out as "true".
    """
    # Defined above _env and CONFIG, which is fine: the lookup happens when this is
    # called, and nothing calls it at import time.
    return _env(name, "0").strip().lower() in ("1", "true")


# The project was called chrome-in-a-box before it grew Firefox and Chromium.
FORMER_NAME = "chrome-in-a-box"
PROJECT = "browser-in-a-box"


def config_root() -> Path:
    """Where everything cib keeps lives, moving it off the old name if it is there.

    Renamed rather than left behind: that directory holds the guest's password and
    both key pairs, and its disk was patched with that key. A rename that quietly
    started a fresh directory would lock cib out of a VM that is still running,
    with no password fallback to get back in.
    """
    root = Path.home() / ".config" / PROJECT
    former = Path.home() / ".config" / FORMER_NAME
    if former.is_dir() and not root.exists():
        former.replace(root)
    return root


def config_path() -> Path:
    """Where the settings file lives. CIB_CONFIG is read straight from the
    environment, because everything else is read through the file it names."""
    named = os.environ.get("CIB_CONFIG", "")
    if named:
        return Path(named).expanduser()
    return config_root() / "cib.yaml"


# Sections rather than one flat list, because the two variants share a prefix but
# not much else: [box] keys become CIB_<KEY>, [vm] keys become CIB_VM_<KEY>.
CONFIG_SECTIONS = {"box": "CIB_", "vm": "CIB_VM_"}


def load_config() -> dict[str, str]:
    """The settings file, flattened into the same names the environment uses.

    Absent is normal and silent. Present but unreadable is not: a typo in a file
    someone wrote on purpose should say so rather than be ignored, which would
    look exactly like the setting not working.
    """
    path = config_path()
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        # cib runs from a checkout with nothing installed, which is one of the four
        # documented ways in. Silently ignoring the file in that case would be the
        # worst outcome: the settings would simply not apply, with no clue why.
        raise Failure(
            f"{path} needs PyYAML to read, and it is not installed. Install cib with "
            "'uv tool install' or 'brew install', or use CIB_* environment variables "
            "instead — they need nothing."
        ) from None
    try:
        loaded = yaml.safe_load(path.read_text()) or {}
    except (yaml.YAMLError, OSError) as exc:
        raise Failure(f"cannot read {path}: {exc}") from None
    if not isinstance(loaded, dict):
        raise Failure(f"{path} must hold a mapping of sections, not {type(loaded).__name__}")
    settings: dict[str, str] = {}
    for section, prefix in CONFIG_SECTIONS.items():
        values = loaded.get(section) or {}
        if not isinstance(values, dict):
            raise Failure(f"{path}: the {section!r} section must be a mapping")
        for key, value in values.items():
            if isinstance(value, bool):
                # yaml turns "yes" into True, and everything downstream reads
                # strings. Lower case, because that is what the env vars use.
                value = str(value).lower()
            settings[f"{prefix}{str(key).upper()}"] = str(value)
    unknown = set(loaded) - set(CONFIG_SECTIONS)
    if unknown:
        raise Failure(
            f"{path}: unknown section(s) {', '.join(sorted(unknown))} — "
            f"expected {' and '.join(CONFIG_SECTIONS)}"
        )
    return settings


CONFIG = load_config()


def _env(name: str, default: str) -> str:
    # Environment first: it is the more immediate of the two, so exporting a
    # variable for one command overrides the file without editing it.
    return os.environ.get(name) or CONFIG.get(name, default)


def _env_int(name: str, default: str, minimum: int = 1) -> int:
    """An integer setting. A bad value is the user's typo, not a crash."""
    # Through _env, not os.environ: reading the environment directly here meant the
    # settings file worked for every string setting and silently for no numeric one.
    raw = _env(name, default)
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
    browser: str = field(default_factory=lambda: chosen_browser().key)
    # CIB_IMAGE still wins, for a fork or a pinned digest — but the default now
    # follows the browser rather than being Chrome's whatever CIB_BROWSER says.
    image: str = field(default_factory=lambda: _env("CIB_IMAGE", chosen_browser().image))

    def __post_init__(self) -> None:
        if not self.volume:
            object.__setattr__(self, "volume", f"{PROJECT}-profile")
        if self.browser == cibbrowsers.ALL:
            raise Failure(
                "CIB_BROWSER=all is a VM mode: a container image serves exactly one "
                "browser, and there is no image with three. Use 'cib vm' for that, or "
                "name one browser here."
            )

    name: str = field(default_factory=lambda: _env("CIB_NAME", PROJECT))
    # The old name is kept when a volume under it already exists, because that
    # volume is the browser profile: passwords, sessions, extensions. Defaulting to
    # the new name would start an empty one and look like the profile was lost.
    volume: str = field(default_factory=lambda: _env("CIB_VOLUME", ""))
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
        if (width, height) not in KASM_MODES:
            available = ", ".join(f"{w}x{h}" for w, h in KASM_MODES)
            raise Failure(
                f"CIB_RESOLUTION {self.resolution} is not one of the modes KasmVNC "
                f"ships, so xrandr would refuse it and the desktop would stay at "
                f"1024x768. Available: {available}. Leave it unset to follow the "
                "browser window instead, which is what most people want."
            )


def find_engine() -> str:
    """Return the container engine to use."""
    # Through _env, so a settings file can name the engine like every other key.
    preferred = _env("CIB_ENGINE", "")
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
    # urlopen honours HTTPS_PROXY. This only ever talks to localhost, so a proxy in
    # the environment could only ever break it — a healthy container was reported
    # dead, and `box up` then tore it down and built it again.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context)
    )
    try:
        # Fixed https://localhost URL, never user input.
        with opener.open(f"https://localhost:{cfg.port}/", timeout=5) as response:
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
    """Apply the resolution and make sure the browser is running. Returns False and
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
        desktop_script(cibbrowsers.BROWSERS[cfg.browser]),
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


def ensure_image(engine: str, cfg: Config) -> None:
    """Pull the image with the engine's own progress on screen.

    `run -d` is captured so the container id does not land on the terminal — which
    also swallowed the entire first pull. cib printed one line and then nothing at
    all for several gigabytes, which reads as a hang.
    """
    # The architecture is part of what cib needs: an arm64 copy of the same tag
    # satisfies `image inspect` and then `run --platform linux/amd64` pulls the
    # amd64 one anyway — silently, because that pull is captured.
    local = run(
        engine, "image", "inspect", "-f", "{{.Architecture}}", cfg.image, check=False, capture=True
    )
    if local.returncode == 0 and local.stdout.strip() == "amd64":
        return
    print(f"Pulling {cfg.image} — several GB, once ...")
    result = run(engine, "pull", "--platform", "linux/amd64", cfg.image, check=False)
    if result.returncode != 0:
        raise Failure(f"could not pull {cfg.image}")


def cmd_up(engine: str, cfg: Config) -> None:
    cfg.check()
    if container_running(engine, cfg) and not env_flag("CIB_FORCE") and ui_is_up(cfg):
        ensure_desktop(engine, cfg)  # still re-applies the mode and revives the browser
        print(f"Already running. Open {cfg.url}")
        return

    # Pulled before the container is removed, not after: a pull that cannot succeed
    # would otherwise have destroyed a working container and then reported only the
    # pull. A guard has to run before the thing it guards.
    ensure_image(engine, cfg)
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
    # Checked here rather than by reading the engine's exit code: podman and docker
    # disagree about what "no such container" is worth, and `cib box shell && echo
    # attached` printed the refusal and then "attached".
    if not container_running(engine, cfg):
        raise Failure(f"{cfg.name!r} is not running — 'cib box up' first")
    # -t only when there is a terminal to attach: podman blocks for ever allocating
    # a pty for a stdin that is a pipe, so `cib box shell` from a script hung.
    flags = "-it" if sys.stdin.isatty() else "-i"
    result = run(engine, "exec", flags, cfg.name, "bash", check=False)
    # 125 and 126 are the engine's own "could not start this at all"; anything else
    # is the shell's exit status, which is the user's business, not a cib failure.
    if result.returncode in (125, 126):
        raise Failure(
            f"could not start a shell in {cfg.name!r} (engine exit {result.returncode}) — "
            "the container may have stopped, or the image may have no bash"
        )


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
    # Deliberately smaller than the screen you are reading this on. tart makes the
    # VM's resolution the window's *minimum* size, and SwiftUI marks a window whose
    # minimum does not fit the display fullScreenNone — which greys out View > Enter
    # Full Screen with nothing to say why. 1920x1200 did that on a 1728x1117-point
    # laptop screen, i.e. on most of them. Nothing is lost by starting small: the
    # window resizes freely upwards, and tart refits the guest's resolution to match.
    display: str = field(default_factory=lambda: _env("CIB_VM_DISPLAY", "1280x800"))
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
        # The name is a path component: SECRETS is ~/.config/browser-in-a-box/<name>,
        # and 'cib vm delete' removes that directory whole. Empty would make it every
        # VM's secrets, and ".." would make it ~/.config.
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", self.name):
            raise Failure(
                f"CIB_VM_NAME must start with a letter or digit and hold only letters, "
                f"digits, dot, dash or underscore, got {self.name!r}"
            )
        # An empty share expands to the current directory, which would be shared
        # into the guest wholesale.
        if not self.share.strip():
            raise Failure("CIB_VM_SHARE is empty; unset it for the default, or give a path")
        # Checked here rather than only in vm_run_args: 'create' does not reach that
        # until the guest is built, so a typo used to cost the whole build first.
        if self.viewer not in ("window", "vnc"):
            raise Failure(f"CIB_VM_VIEWER must be window or vnc, got {self.viewer!r}")
        if self.net not in ("bridged", "shared", "host"):
            raise Failure(f"CIB_VM_NET must be bridged, shared or host, got {self.net!r}")
        if ":" in str(Path(self.share).expanduser()):
            raise Failure(
                f"CIB_VM_SHARE cannot contain a colon, tart uses it as a separator: {self.share}"
            )
        # Normalised, not merely rejected: the box variant accepts "1280 X 800" and
        # lowercases it, and tart takes 1920X1200 without complaint while ignoring
        # it — so a capital X used to produce a guest silently stuck at 1024x768.
        if not re.fullmatch(r"\d+x\d+", self.normalised_display):
            raise Failure(f"CIB_VM_DISPLAY must look like 1920x1200, got {self.display!r}")

    @property
    def normalised_display(self) -> str:
        return self.display.lower().replace(" ", "")

    # "latest" is what Apple is shipping today, which is what a new guest usually
    # wants — but it moves, so a rebuild is not reproducible unless it can be told
    # which installer to use (a URL or a path to an .ipsw).
    ipsw: str = field(default_factory=lambda: _env("CIB_VM_IPSW", "latest"))
    # "window" is tart's own, which cannot go full screen or scale. "vnc" hands the
    # display to macOS Screen Sharing, which does both.
    browser: str = field(default_factory=lambda: chosen_browser().key)
    viewer: str = field(default_factory=lambda: _env("CIB_VM_VIEWER", "window"))
    # Sends Cmd+Space, Cmd+Tab and the rest to the guest while its window has focus,
    # instead of to whatever on the host has registered them. Off by default: it is
    # all-or-nothing, so a host launcher on Cmd+Space becomes unreachable until you
    # click away. tart rejects it alongside --vnc, which has no window to focus.
    capture_keys: str = field(default_factory=lambda: _env("CIB_VM_CAPTURE_KEYS", "false"))


PACKER_TEMPLATE = Path(__file__).resolve().parent / "packer" / "browser-vm.pkr.hcl"


# Where the generated guest password is kept, so it survives between commands and
# can be pasted rather than typed.
# Per VM name. They used to sit flat in one directory, so a second CIB_VM_NAME
# reused the first one's password and key — and deleting either took the other's
# away with it. Read at import because a CLI cannot change its own environment.
DEFAULT_VM_NAME = "chrome-vm"


def secrets_dir() -> Path:
    """Where this VM's password and keys live."""
    return config_root() / _env("CIB_VM_NAME", DEFAULT_VM_NAME)


SECRETS = secrets_dir()
CREDENTIALS = SECRETS / "vm-credentials"
# The key cib logs in with, the host key it plants in the guest so it can recognise
# it again, and the known_hosts holding that host key.
VM_KEY = SECRETS / "vm-key"
VM_HOST_KEY = SECRETS / "vm-host-key"
KNOWN_HOSTS = SECRETS / "vm-known-hosts"
# What cib knows about this guest that is not a secret. One labelled file rather
# than a directory of bare values: vm-last-ip held "192.168.1.56" and nothing else,
# so the only way to know what it was for was to read the source.
STATE = SECRETS / "state.yaml"
# tart's own output from a detached start, kept because that start has no terminal
# to print on and a failure would otherwise leave nothing to read.
BOOT_LOG = SECRETS / "tart-boot.log"


PATCHER = Path(__file__).resolve().parent / "cibpatch.py"


def find_guest_python() -> str:
    """A python that can actually run the patcher.

    /usr/bin/python3 on macOS is a shim: without the Command Line Tools it exists,
    is executable, and exits non-zero the moment it is run. The compiled build has
    no interpreter of its own — it spawns one — so this is checked rather than
    assumed, and named rather than surfacing as a patch that failed for no reason.
    """
    for candidate in ("/usr/bin/python3", shutil.which("python3")):
        if not candidate:
            continue
        probe = subprocess.run(  # noqa: S603
            [candidate, "-c", ""], check=False, capture_output=True
        )
        if probe.returncode == 0:
            return candidate
    raise Failure(
        "no working python3 was found, and the patcher is a script that needs one.\n"
        "On a Mac without the Command Line Tools, /usr/bin/python3 is only a stub: "
        "run 'xcode-select --install', or 'brew install python@3.14'."
    )


def find_patcher() -> Path:
    """The offline path spawns cibpatch.py rather than importing it, so nothing
    but this check knows whether it is there."""
    if not PATCHER.exists():
        raise Failure(
            f"the patcher is missing at {PATCHER} — the offline path needs it beside "
            "cib. Either run cib.py from the repository, or fall back to driving Setup "
            "Assistant: re-run with CIB_VM_PACKER=1, and 'cib vm delete' first if a "
            "half-built VM is already there ('vm create' only reports that it exists)."
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
    "Run 'sudo -v' and then cib FROM THE SAME TERMINAL: sudo remembers a\n"
    "credential per tty, so one cached in another window does not count, and a\n"
    "process with no tty at all can never have one. cib never prompts for a\n"
    "password itself, so a credential cached beforehand is the only way in."
)


def sudo_is_cached() -> bool:
    """Whether sudo would run without prompting.

    sudo prompts on its own tty, so it can ask for nothing when cib runs detached;
    -n turns that into an exit code instead of a hang.

    It also *remembers* per tty: a credential cached in another window is invisible
    here, and a process launched without a tty at all can never obtain one. So this
    is not only "has the user run sudo -v", it is "did they run it here".
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


SECRET_NAMES = (
    "vm-credentials",
    "vm-key",
    "vm-key.pub",
    "vm-host-key",
    "vm-host-key.pub",
    "vm-known-hosts",
)


def migrate_flat_secrets() -> None:
    """Move what an older cib left one directory up.

    It kept them flat under the config directory, shared by every VM name, so
    nothing on disk says which guest they belong to. They go to the *default* name
    rather than to whichever name happens to run first: moving them into the first
    name would take them away from the guest that is actually using them, whose
    disk was patched with that key pair — and with no password fallback left, cib
    could never reach it again.

    Moved rather than regenerated, for the same reason.
    """
    flat = config_root()
    for name in SECRET_NAMES:
        old, current = flat / name, flat / DEFAULT_VM_NAME / name
        if old.is_file() and not current.exists():
            current.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            old.replace(current)
    # The remembered address used to be a file of its own holding a bare value, and
    # for one release there was a vm-vnc-url beside it that nothing reads any more.
    # Carried over rather than dropped: losing it costs a guest that arp has
    # forgotten, which is the one case the whole thing exists for.
    previous = SECRETS / "vm-last-ip"
    if previous.is_file():
        if not STATE.exists():
            write_state(last_ip=previous.read_text().strip())
        previous.unlink()
    for stale in ("vm-vnc-url", "vm-boot-log"):
        (SECRETS / stale).unlink(missing_ok=True)


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
    migrate_flat_secrets()
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
    migrate_flat_secrets()
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
    chosen = _env("CIB_VM_PASSWORD", "")
    if chosen:
        # Yours to choose, and yours to weigh: a bridged guest sits on the same
        # network as everyone else on it. SSH here is key-only, so a guessable
        # password does not open that — but Screen Sharing, if you ever turn it on
        # inside the guest, takes exactly this password from anywhere on the LAN.
        if set(chosen) - set(alphabet):
            raise Failure(
                "CIB_VM_PASSWORD may only hold letters and digits, and not y or z: "
                "the build types it as keystrokes, and those keys move between the "
                "US and Swiss German layouts"
            )
        password = chosen
    else:
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
        # Homebrew's directories are put on PATH by a shell profile, and plenty of
        # ways of running cib have no shell: a Dock click runs with PATH set to
        # /usr/bin:/bin:/usr/sbin:/sbin and nothing else.
        for known in ("/opt/homebrew/bin/tart", "/usr/local/bin/tart"):
            if os.access(known, os.X_OK):
                return known
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
    # create=True: the patch rewrites the account record wholesale, so whatever
    # password it writes *is* the account's password afterwards. Refusing here
    # because the file went missing would leave a built guest with no way forward.
    _prepare_guest(vm, guest_password(create=True))


# `tart create` returns before the Virtualization framework has let go of the VM's
# auxiliary storage, so a boot started straight afterwards fails with "Failed to
# lock auxiliary storage" (EAGAIN). Nothing on the host holds it a moment later: it
# is a handover, not a conflict.
BOOT_ATTEMPTS = 12
BOOT_SETTLE_SECS = 5
LOCKED_MARKER = "lock auxiliary storage"


def boot_once(tart: str, vm: VmConfig) -> subprocess.Popen[str]:
    """Start the fresh guest, waiting out the installer's lock if it is still held.

    Returns the still-running child. A boot that exits for any other reason is
    reported as it is: a guest that never booted has no first-boot state, and
    patching it would produce something that says "Built" and cannot log in.
    """
    for attempt in range(1, BOOT_ATTEMPTS + 1):
        boot = subprocess.Popen(  # noqa: S603
            [tart, "run", "--no-graphics", vm.name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(BOOT_SETTLE_SECS)
        if boot.poll() is None:
            return boot
        detail = (boot.stderr.read() or "").strip() if boot.stderr else ""
        if LOCKED_MARKER not in detail:
            raise Failure(
                f"the guest's first boot exited immediately (tart exit {boot.returncode})"
                + (f": {detail}" if detail else "")
            )
        if attempt == 1:
            print("  the installer has not let go of the VM yet, waiting ...")
    raise Failure(
        f"{vm.name!r} was still locked after "
        f"{BOOT_ATTEMPTS * BOOT_SETTLE_SECS}s. Check 'cib vm status', and that no "
        "other tart process has it open."
    )


def _create_offline(tart: str, vm: VmConfig) -> None:
    """Build the guest and prepare it by patching its disk, so Setup Assistant is
    never shown. Deterministic, unlike typing into it."""
    password = guest_password(create=True)
    firstboot = _env_int("CIB_VM_FIRSTBOOT_SECS", "180", 0)  # before anything is built
    # All three checked before the multi-gigabyte download rather than after it: the
    # patch step is the only part that needs any of them, and finding that out at
    # the end costs the entire build.
    find_patcher()
    find_guest_python()
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
            vm.normalised_display,
            check=False,
        )
        # The guest has to boot once for its first-boot state to exist; there is nothing
        # to patch before that.
        print("Booting once so the guest lays down its first-boot state ...")
        # A boot that never happened would otherwise be patched and called "Built",
        # so boot_once raises rather than returning a dead child.
        boot = boot_once(tart, vm)
        time.sleep(max(0, firstboot - BOOT_SETTLE_SECS))
        if boot.poll() is not None:
            raise Failure(
                f"the guest's first boot stopped before it was ready (tart exit "
                f"{boot.returncode})"
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
    print("Starting it — a window opens, and this carries on without it ...")
    boot = start_detached(tart, vm)
    ip = wait_for_guest(tart, vm, boot)
    # 'create' resolves the address itself rather than through vm_ip, so without this
    # the fallback is empty for exactly the user who ran one command and walked away.
    remember_ip(ip)
    print(f"Installing Chrome, the clipboard agent and downloads on {vm.user}@{ip} ...")
    if install_browsers(vm, ip, password) != 0:
        raise Failure(
            f"the guest at {ip} is up but the install failed (see above). It is still "
            "running: 'cib vm setup' retries just this part."
        )
    print()
    print(f"Ready. The account is {vm.user!r}; 'cib vm password' prints its password.")
    show_viewer(vm, ip)
    print()
    # Signing in switches iCloud Keychain on by itself and joins the sync circle, so
    # there is no second toggle to hunt for. Nor could there be a third command here:
    # joining is a sponsorship handshake with a device already in the circle, not a
    # setting a script could write.
    print("One thing Apple only allows by hand: sign in to your Apple Account in the")
    print("guest. That is also what brings your passkeys in.")
    print()
    print("'cib vm down' stops it, 'cib vm up' brings it back.")


SCREEN_SHARING_OFF = """\
CIB_VM_VIEWER=vnc needs Screen Sharing turned on inside the guest, and nothing on
this side can turn it on: macOS 26 only accepts it from the guest's own System
Settings. Do it once, in tart's window (CIB_VM_VIEWER=window):

    System Settings > General > Sharing > Screen Sharing

Then 'cib vm open' connects, and the window can go full screen."""


def show_viewer(vm: VmConfig, ip: str) -> None:
    """Say how to look at the guest, which differs per CIB_VM_VIEWER."""
    if vm.viewer != "vnc":
        print("Its window is already open. 'cib vm icon' puts it in ~/Applications.")
    elif guest_answers(ip, SCREEN_SHARING_PORT):
        print("Open its screen with 'cib vm open' — the password goes in for you.")
    else:
        print(SCREEN_SHARING_OFF)


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
        else find_guest_python()
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
            f"the build template is missing at {PACKER_TEMPLATE}. It ships with every "
            "install, so this is a broken one — reinstall cib, or run cib.py from a "
            "checkout. The default path needs no template at all: unset CIB_VM_PACKER."
        )
    print(f"Building {vm.name!r} from a fresh macOS image, unattended.")
    run(packer, "init", str(PACKER_TEMPLATE))
    print("This takes a while: it installs macOS and drives Setup Assistant.")
    # The packer path never generated these, so the 'cib vm setup' it tells you to
    # run next could never connect to what it had just built.
    ensure_vm_keys()
    layout_id, layout_name = host_keyboard_layout()
    zone, city = host_time_zone()
    key_env = {
        "PKR_VAR_authorized_key": VM_KEY.with_suffix(".pub").read_text().strip(),
        "PKR_VAR_host_private_key": VM_HOST_KEY.read_text(),
        "PKR_VAR_host_public_key": VM_HOST_KEY.with_suffix(".pub").read_text().strip(),
    }
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
        # Key material by environment, not argv, for the same reason as the
        # password: argv is readable by every local user for the whole build.
        env={**os.environ, "PKR_VAR_password": password, **key_env},
    )
    run(tart, "set", vm.name, "--display", vm.normalised_display, check=False)
    print()
    print(f"Built. The account is {vm.user!r}; 'cib vm password' prints its password.")
    print("Next:")
    print("  1. cib vm up")
    # No Keychain step: signing in switches it on and joins the sync circle itself.
    print("  2. sign in to your Apple Account            (interactive: 2FA)")
    print("  3. cib vm setup    — installs Chrome, the clipboard agent and downloads")


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
    if vm.viewer == "vnc":
        # tart's own window has no full screen and no scaling; Screen Sharing has
        # both. It needs Remote Login in the guest, which the offline patch turns on.
        args.append("--vnc")
    elif vm.viewer != "window":
        raise Failure(f"CIB_VM_VIEWER must be window or vnc, got {vm.viewer!r}")
    if vm.capture_keys.lower() in ("1", "true", "yes"):
        if vm.viewer != "window":
            raise Failure(
                "CIB_VM_CAPTURE_KEYS needs tart's own window; tart rejects it with "
                "--vnc, which has no window of its own to hold the focus"
            )
        args.append("--capture-system-keys")
    if vm.net == "bridged":
        return [*args, f"--net-bridged={vm.interface}", vm.name]
    if vm.net == "host":
        return [*args, "--net-host", vm.name]
    if vm.net != "shared":
        raise Failure(f"CIB_VM_NET must be bridged, shared or host, got {vm.net!r}")
    return [*args, vm.name]


def start_detached(tart: str, vm: VmConfig) -> subprocess.Popen[str]:
    """Start the guest without holding this terminal.

    `tart run` is a foreground process for as long as the VM lives, which is why
    `cib vm up` blocks. Left as a child that outlives cib, the window stays and the
    command can carry on — 'cib vm down' stops it, the same as before.

    Its stdout goes to a file rather than to nothing, so a start that fails leaves
    something to read afterwards — a detached run has no terminal to print on.
    """
    BOOT_LOG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Unlinked first so O_CREAT applies the mode even when the file is already there.
    BOOT_LOG.unlink(missing_ok=True)
    log = os.fdopen(os.open(BOOT_LOG, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")
    try:
        return subprocess.Popen(  # noqa: S603
            [tart, *vm_run_args(vm)],
            stdout=log,
            # Into the same file, not a pipe. A pipe nobody drains kills the guest
            # twice over: tart blocks once 64 KiB of diagnostics have filled it, and
            # when cib exits the read end closes and the next write is a SIGPIPE. The
            # detach this function is named for does not survive either.
            stderr=subprocess.STDOUT,
            text=True,
            # The detach this function is named for. Without a session of its own the
            # guest is still in the caller's process group, so it takes the SIGHUP
            # sent when that group goes away: closing the terminal, or a wrapper
            # exiting, killed the VM seconds after it started. "Not held by this
            # terminal" has to mean not reachable by that terminal's signals.
            start_new_session=True,
        )
    finally:
        # Popen duplicates the descriptor, so the child keeps writing after this.
        log.close()


def screen_url(vm: VmConfig, ip: str) -> str:
    """The address Screen Sharing opens, with the credentials already in it.

    tart's --vnc is not a VNC server of its own: it opens macOS Screen Sharing at
    the guest and nothing more, so the password here is the guest account's, not
    something tart generated. Putting it in the URL is what stops Screen Sharing
    asking for a password nobody can be expected to type from memory.
    """
    return f"vnc://{vm.user}:{quote(guest_password(), safe='')}@{ip}"


def boot_log_tail(lines: int = 5) -> str:
    """The end of what tart said, for a failure with no terminal to have said it on."""
    if not BOOT_LOG.exists():
        return ""
    return " / ".join(BOOT_LOG.read_text(errors="replace").split("\n")[-lines:]).strip(" /")


def wait_for_guest(tart: str, vm: VmConfig, boot: subprocess.Popen[str]) -> str:
    """Wait until the guest answers on the network, or say why it never did."""
    deadline = time.monotonic() + GUEST_WAIT_SECS
    while time.monotonic() < deadline:
        if boot.poll() is not None:
            detail = boot_log_tail()
            raise Failure(
                f"{vm.name!r} stopped while it was starting (tart exit {boot.returncode})"
                + (f": {detail}" if detail else "")
            )
        result = run(
            tart,
            "ip",
            "--resolver",
            "arp" if vm.net == "bridged" else "dhcp",
            "--wait",
            "10",
            vm.name,
            check=False,
            capture=True,
        )
        ip = result.stdout.strip()
        if ip:
            return ip
        time.sleep(2)
    raise Failure(
        f"{vm.name!r} did not answer on the network within {GUEST_WAIT_SECS}s. It is "
        "still running: 'cib vm setup' retries just this part."
    )


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


# Chrome's own preference file, written before its first launch. prompt_for_download
# stays off so a download does not open a panel pointing at the guest's own disk.
FIRST_RUN_PREFS = json.dumps(
    {
        "download": {"default_directory": GUEST_SHARE, "prompt_for_download": False},
        # Everything below sends less to Google. User preferences, not managed
        # policy: a policy is the one thing this VM exists to be free of, and it
        # would put a "managed by your organization" banner in the menu.
        #
        # None of it touches passkeys. Those come from iCloud Keychain by way of
        # macOS, not from Google's password manager, so turning Google's own
        # services down does not take them away.
        "search": {"suggest_enabled": False},
        "alternate_error_pages": {"enabled": False},
        "safebrowsing": {"enabled": False, "enhanced": False},
        "spellcheck": {"use_spelling_service": False},
        # 2 is "do not preconnect or prefetch", which otherwise resolves and opens
        # connections to whatever a page merely hints at.
        "net": {"network_prediction_options": 2},
        "browser": {"has_seen_welcome_page": True},
        "credentials_enable_service": False,
        "profile": {"password_manager_leak_detection": False},
    }
)
# Metrics consent is not a profile preference — it lives in Local State, beside the
# profiles rather than inside one, so writing it into Preferences does nothing.
LOCAL_STATE_PREFS = json.dumps({"user_experience_metrics": {"reporting_enabled": False}})

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


def _fetch_browser(browser: cibbrowsers.Browser) -> str:
    """The shell that gets the app into /Applications, whatever it ships as.

    Staged aside and moved into place as the last step, so an interruption cannot
    leave half a browser at the path everything else looks at.
    """
    work = '"$CIB_WORK'
    if browser.archive == "dmg":
        lines = [
            f'  curl -fsSL -o {work}/browser.dmg" {shlex.quote(browser.url)}',
            f'  hdiutil attach -nobrowse -quiet {work}/browser.dmg" -mountpoint {work}/mount"',
            f'  mkdir -p {work}/staging"',
            f'  cp -R {work}/mount/{browser.inside}" {work}/staging/"',
            f'  hdiutil detach -quiet {work}/mount"',
        ]
        return "\n".join(lines)
    # Chromium publishes no release for macOS, only per-commit snapshots, so the
    # newest build number has to be read before there is a URL to fetch at all.
    url = browser.url.replace("{revision}", "$CIB_REVISION")
    return "\n".join(
        [
            f'  CIB_REVISION="$(curl -fsSL {shlex.quote(browser.revision_url)})"',
            f'  [ -n "$CIB_REVISION" ] || {{ echo "no {browser.label} build found" >&2; exit 1; }}',
            f'  curl -fsSL -o {work}/browser.zip" "{url}"',
            f'  mkdir -p {work}/staging"',
            f'  ditto -x -k {work}/browser.zip" {work}/unpacked"',
            f'  mv {work}/unpacked/{browser.inside}" {work}/staging/"',
        ]
    )


def _first_run_settings(browser: cibbrowsers.Browser) -> str:
    """Downloads and privacy, written before the browser's first launch.

    Before rather than after, so it starts with the settings instead of being
    reconfigured once it has already phoned home.
    """
    profile = f'"$HOME/{browser.profile}"'
    if browser.settings == "firefox":
        # profiles.ini as well as the directory: Firefox will not use a profile it
        # has not been told about, so the files alone would be ignored.
        support = '"$HOME/Library/Application Support/Firefox/profiles.ini"'
        return "\n".join(
            [
                f"    mkdir -p {profile}",
                f"    printf '%s' {shlex.quote(cibbrowsers.firefox_preferences(GUEST_SHARE))}"
                f" > {profile}/user.js",
                f"    printf '%s' {shlex.quote(cibbrowsers.FIREFOX_PROFILES_INI)} > {support}",
            ]
        )
    return "\n".join(
        [
            f"    mkdir -p {profile}",
            f"    printf '%s' {shlex.quote(cibbrowsers.chromium_preferences(GUEST_SHARE))}"
            f" > {profile}/Preferences",
            f"    printf '%s' {shlex.quote(cibbrowsers.CHROMIUM_LOCAL_STATE)}"
            f' > {profile}/../"Local State"',
        ]
    )


def install_browsers(vm: VmConfig, ip: str, password: str) -> int:
    """Run the install once per browser the choice covers.

    A loop rather than one script that installs three: the script is idempotent —
    it says "already installed" and moves on — so running it again is cheap, and
    keeping it single-browser is what stops it growing a second dimension of
    conditionals nobody can read.
    """
    zone = host_time_zone()[0]
    for position, browser in enumerate(cibbrowsers.expand(vm.browser)):
        print(f"Installing {browser.label} on {vm.user}@{ip} ...")
        failed = guest_ssh(
            vm, ip, guest_install_script(password, zone, browser, first=position == 0)
        )
        if failed:
            return failed
    return 0


def guest_install_script(
    password: str,
    time_zone: str = "",
    browser: cibbrowsers.Browser | None = None,
    first: bool = True,
) -> str:
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
    # Set in the guest rather than by patching its disk: on a real Data volume both
    # /etc/localtime and the zoneinfo directory are symlinks into paths that resolve
    # against *this* host, so the patcher cannot follow them safely — and refusing
    # them aborted the whole patch. systemsetup is the guest's own tool for this.
    time_zone_step = (
        f"sudo_pw systemsetup -settimezone {shlex.quote(time_zone)} >/dev/null 2>&1 || \\\n"
        f'  echo "could not set the time zone to {time_zone}" >&2'
        if time_zone
        else ":"
    )
    browser = browser or chosen_browser()
    # `all` is a choice, not a browser: its row carries no app name, so every path
    # below that names the bundle would come out as "/Applications" itself — and one
    # of them is `rm -rf`. install_browsers expands the choice and calls this once
    # per browser; nothing else may call it with the sentinel.
    if not browser.app_name:
        raise Failure(
            f"guest_install_script needs one browser, not {browser.key!r}; "
            "expand the choice with cibbrowsers.expand() first"
        )
    # Written out here rather than branched on inside the shell: the script is
    # already the hardest thing in this file to read, and a second dimension of
    # conditionals in it would not survive the next change.
    fetch = _fetch_browser(browser)
    settings = _first_run_settings(browser)
    # Only the first pass asks. Under CIB_BROWSER=all this script runs once per
    # browser, and asking every one of them in turn left whichever happened to be
    # installed last as the default — Chromium, by dict order, which is not the
    # browser anyone chose.
    if not first:
        default_browser_step = ": # the first browser of this run already asked"
    elif browser.settings == "firefox":
        # Firefox has its own switch and ignores the Chromium one entirely.
        default_browser_step = (
            f"{shlex.quote(browser.binary)} --setDefaultBrowser >/dev/null 2>&1 || true"
        )
    else:
        default_browser_step = (
            f"open -a {shlex.quote(browser.app)} --args --make-default-browser"
            " >/dev/null 2>&1 || true"
        )
    # -array replaces the whole row, -array-add appends to it. The first pass clears
    # Apple's suite out, the rest add themselves — otherwise the last browser
    # installed was the only one in the Dock, and re-running would stack duplicates.
    dock_verb = "-array" if first else "-array-add"

    return f"""set -eu
CIB_SUDO_PW={shlex.quote(password)}
# Scratch space under the account's own home rather than /tmp, which every user in
# the guest can write to: a staged Chrome.app sitting there could be swapped
# between the copy and the move into /Applications.
CIB_WORK="$HOME/.cache/cib"
# Detached before the directory is touched, in both places: rm -rf over a mounted
# DMG recurses into a read-only volume, fails, and leaves the image attached — and
# the next run then aborts here instead of installing anything.
cleanup() {{
  hdiutil detach -quiet -force "$CIB_WORK/mount" >/dev/null 2>&1 || true
  rm -rf "$CIB_WORK"
}}
cleanup
mkdir -p "$CIB_WORK"
trap cleanup EXIT
sudo_pw() {{ printf '%s\\n' "$CIB_SUDO_PW" | sudo -S -p '' "$@"; }}
# Downloads land on the host. Not by replacing ~/Downloads: macOS protects that
# folder against being renamed, and a process arriving over ssh has no TCC grant
# for it, so `mv` there fails with EPERM however the permissions look. Chrome is
# pointed at the share instead, and a link inside ~/Downloads makes it reachable
# from anything else.
if [ -d "{GUEST_SHARE}" ]; then
  ln -sfn "{GUEST_SHARE}" "$HOME/Downloads/on-the-host" 2>/dev/null ||
    echo "could not link the shared folder into ~/Downloads" >&2
  if [ -e "$HOME/{browser.profile}" ]; then
    echo "{browser.label} already has a profile; leaving its settings alone" >&2
  else
{settings}
  fi
else
  echo "the shared downloads folder is not mounted; start the VM with 'cib vm up'" >&2
  exit 1
fi
# Tested on the binary, not the bundle: an interrupted `cp -R` leaves a directory
# that exists but cannot run, and a directory test would call that "installed"
# for ever.
if [ -x {shlex.quote(browser.binary)} ]; then
  echo '{browser.label} is already installed'
else
  rm -rf {shlex.quote(browser.app)}
{fetch}
  mv "$CIB_WORK/staging/{browser.app_name}" {shlex.quote(browser.app)}
fi
{shlex.quote(browser.binary)} --version
if [ ! -x {AGENT_BIN} ]; then
  # Host/guest copy-paste needs an agent inside the guest. Without it the generated
  # password would have to be typed by hand at every passkey prompt, which is the
  # one thing generating it was meant to avoid.
  curl -fsSL -o "$CIB_WORK/agent.tar.gz" \
    "https://github.com/cirruslabs/tart-guest-agent/releases/download/v{GUEST_AGENT_VERSION}/tart-guest-agent-darwin-all.tar.gz"
  tar -xzf "$CIB_WORK/agent.tar.gz" -C "$CIB_WORK"
  # BSD install does not create the target directory, and a fresh guest may have
  # /usr/local without a bin in it.
  sudo_pw install -d -m 0755 "$(dirname {AGENT_BIN})"
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
{time_zone_step}
# A guest that locks its screen asks for the generated 24-character password, and
# a VM has no Touch ID to shortcut it. The screensaver never starts, the display
# never sleeps, and neither does the machine.
# All three are ByHost preferences, so all three need -currentHost. Two of them
# used to go without it, which writes a domain nothing reads: the guest kept
# asking for the password however many times the setting was turned off.
defaults -currentHost write com.apple.screensaver idleTime -int 0
defaults -currentHost write com.apple.screensaver askForPassword -int 0
defaults -currentHost write com.apple.screensaver askForPasswordDelay -int 0
sudo_pw pmset -a displaysleep 0 sleep 0 >/dev/null ||
  echo "could not turn off display sleep" >&2
# macOS 14 and later keep the lock behind sysadminctl as well; older ones do not
# have the flag at all, so its absence is not a failure.
sudo_pw sysadminctl -screenLock off -password {shlex.quote(password)} >/dev/null 2>&1 || true

# Updates apply themselves. An unpatched guest is the browser you do your banking
# in, and the alternative is the update badge nagging in a VM you opened to do one
# thing. Chrome brings its own updater (Keystone) with the install, so only macOS
# needs saying.
sudo_pw softwareupdate --schedule on >/dev/null 2>&1 || true
for CIB_KEY in AutomaticCheckEnabled AutomaticDownload AutomaticallyInstallMacOSUpdates \\
               CriticalUpdateInstall ConfigDataInstall; do
  sudo_pw defaults write /Library/Preferences/com.apple.SoftwareUpdate "$CIB_KEY" -bool true
done
sudo_pw defaults write /Library/Preferences/com.apple.commerce AutoUpdate -bool true

# Spotlight in the guest answers to Ctrl+Opt+Cmd+Space rather than Cmd+Space. A
# host launcher on Cmd+Space — Raycast, Alfred, Spotlight itself — registers it as a
# global hotkey, and a global hotkey wins over the focused VM window, so the guest
# never sees the keystroke. Nothing can tell the two Command keys apart either: the
# shortcut is stored as a device-independent modifier mask with no left/right bit.
# 32 is the space character, 49 its key code, and 1835008 is control+option+command.
defaults write com.apple.symbolichotkeys AppleSymbolicHotKeys -dict-add 64 \\
  '{{enabled = 1; value = {{parameters = (32, 49, 1835008); type = standard;}};}}'
# Applies it to the running session, so this does not need a logout to take effect.
/System/Library/PrivateFrameworks/SystemAdministration.framework/Resources/activateSettings -u \\
  >/dev/null 2>&1 || true

# Chrome opens the links, since a guest whose default browser is Safari defeats the
# point. macOS takes this without a prompt only because Chrome asks for it itself.
{default_browser_step}

# The browsers this VM is for, and nothing else. The default row is Apple's whole
# suite, none of which it is for.
defaults write com.apple.dock persistent-apps {dock_verb} \\
  '<dict><key>tile-data</key><dict><key>file-data</key><dict>'\\
'<key>_CFURLString</key><string>{browser.app}</string>'\\
'<key>_CFURLStringType</key><integer>0</integer></dict></dict>'\\
'<key>tile-type</key><string>file-tile</string></dict>'
defaults write com.apple.dock persistent-others -array
defaults write com.apple.dock show-recents -bool false
# Out of the way by default: the guest's window is already smaller than the screen
# it sits on, so a permanent Dock costs a strip of the little room there is.
defaults write com.apple.dock autohide -bool true
defaults write com.apple.dock autohide-delay -float 0
# Right edge, so it does not sit on top of the host's own Dock along the bottom of
# the same screen. macOS centres the Dock on whichever edge it is given.
defaults write com.apple.dock orientation -string right
killall Dock >/dev/null 2>&1 || true

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
        # PasswordAuthentication alone does not stop a prompt: sshd offers the same
        # password through keyboard-interactive, which is a separate method.
        "-o",
        "KbdInteractiveAuthentication=no",
        "-o",
        "NumberOfPasswordPrompts=0",
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

# How long `vm create` gives the guest to boot and answer before it gives up and
# tells the user to finish with `vm setup`.
GUEST_WAIT_SECS = 300


SCREEN_SHARING_PORT = 5900


def guest_answers(ip: str, port: int = 22) -> bool:
    """Whether anything is listening at this address."""
    with socket.socket() as probe:
        probe.settimeout(2)
        return probe.connect_ex((ip, port)) == 0


def read_state() -> dict[str, str]:
    """What cib remembers about this guest, or nothing if it has never run.

    Parsed here rather than through PyYAML: cib writes this file itself and writes
    only flat `key: value` lines, and state has to work on the bare-checkout path
    where PyYAML may not be installed at all.
    """
    if not STATE.exists():
        return {}
    remembered = {}
    for line in STATE.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if sep:
            remembered[key.strip()] = value.strip()
    return remembered


def write_state(**changes: str) -> None:
    """Update named keys, leaving the rest of the file alone."""
    remembered = read_state() | {key: value for key, value in changes.items() if value}
    STATE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    body = "".join(f"{key}: {value}\n" for key, value in sorted(remembered.items()))
    STATE.write_text("# Written by cib. Safe to delete — everything here is rediscovered.\n" + body)


def remember_ip(ip: str) -> None:
    """Keep the address, so a guest arp has forgotten can still be reached."""
    write_state(last_ip=ip)


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
    if ip:
        remember_ip(ip)
        return ip
    # The host's arp table forgets a guest that has been quiet, and `tart ip --wait`
    # only re-reads that table — it sends nothing that would repopulate it. The
    # guest is usually still there on the address it last answered on.
    remembered = read_state().get("last_ip", "")
    if remembered and guest_answers(remembered):
        return remembered
    # Not "past Setup Assistant": the offline path never shows one, so naming it
    # here sent people looking for a screen that does not exist.
    detail = (result.stderr or "").strip()
    raise Failure(
        f"could not work out the address of {vm.name!r} after {IP_WAIT_SECS}s — is it "
        f"running? ('cib vm status', then 'cib vm up')" + (f"\n{detail}" if detail else "")
    )


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
    # Just the password, nothing around it, so it can be piped straight to pbcopy.
    print(guest_password())


def cmd_vm_login(tart: str, vm: VmConfig) -> None:
    """Both halves of the credential, for the times a login screen does appear.

    Autologin means the guest normally walks past it, but the screen still comes
    back for Screen Sharing, for System Settings and after a lock.
    """
    print(f"user:     {vm.user}")
    print(f"password: {guest_password()}")


def screen_address(tart: str, vm: VmConfig) -> str:
    """The guest's screen address, or say why there is not one."""
    if not vm_running(tart, vm):
        raise Failure(f"{vm.name!r} is not running — 'cib vm up' starts it")
    ip = vm_ip(tart, vm)
    if not guest_answers(ip, SCREEN_SHARING_PORT):
        raise Failure(
            f"{vm.name!r} is up at {ip}, but nothing answers on 5900.\n{SCREEN_SHARING_OFF}"
        )
    return screen_url(vm, ip)


def cmd_vm_viewer(tart: str, vm: VmConfig) -> None:
    # Printed rather than opened, so it can be piped. It carries the password.
    print(screen_address(tart, vm))


def cmd_vm_open(tart: str, vm: VmConfig) -> None:
    """Put the guest's screen in front of the user, whatever the viewer is.

    This is what the clickable app runs, so it has to cope with a guest that is
    not running yet rather than telling the user to go and start it first.
    """
    if not vm_running(tart, vm):
        print(f"Starting {vm.name!r} ...")
        boot = start_detached(tart, vm)
        wait_for_guest(tart, vm, boot)
    if vm.viewer != "vnc":
        raise_window(tart)
        print(f"{vm.name!r} is running; its window is in front.")
        return
    run("/usr/bin/open", screen_address(tart, vm))


def raise_window(tart: str) -> None:
    """Bring tart's window forward, since the guest is usually already running.

    Without this a click on the launcher does nothing visible whenever the VM is
    up — which is most of the time, and reads as the launcher being broken. `open
    -a` on a bundle that is already running activates it rather than starting a
    second copy, so this is safe to call unconditionally.
    """
    bundle = tart_bundle(tart)
    if bundle is None:
        return
    run("/usr/bin/open", "-a", str(bundle), check=False, capture=True)


def tart_bundle(tart: str) -> Path | None:
    """tart's .app, which is not where resolving the binary lands you.

    Homebrew's `tart` is a bash shim, so resolve() ends at Cellar/tart/<v>/bin/tart
    and no parent of it is a bundle at all. The real one is a sibling of that bin,
    under libexec. Walking up and checking both shapes covers the shim, a direct
    bundle path, and a bare binary with no bundle to activate.
    """
    binary = Path(tart).resolve()
    for parent in binary.parents:
        if parent.suffix == ".app":
            return parent
        nested = parent / "libexec" / "tart.app"
        if nested.is_dir():
            return nested
    return None


APPS_DIR = Path("~/Applications").expanduser()


def launcher_command() -> str:
    """How to invoke this same cib from a Dock click.

    The interpreter is named outright when cib is running as a script. A Dock
    launch gets almost none of a login shell's PATH, so `#!/usr/bin/env python3`
    resolves against /usr/bin — where an unconfigured Mac has a stub that opens
    the "install command line tools" dialog instead of running anything.
    """
    entry = Path(sys.argv[0]).resolve()
    if entry.suffix != ".py":
        # A frozen build is its own interpreter.
        return shlex.quote(str(entry))
    python = Path(sys.executable)
    # A virtualenv is the wrong thing to bake into a launcher meant to outlive the
    # shell that wrote it — the icon would break the day that project is cleaned up.
    # cib imports nothing outside the standard library, so the base interpreter the
    # virtualenv was built on runs it just as well. /usr/bin/python3 is not an
    # option: macOS still ships 3.9 there, and cib needs 3.10.
    if sys.prefix != sys.base_prefix:
        base = Path(sys.base_prefix) / "bin" / "python3"
        if base.exists():
            python = base
    return f"{shlex.quote(str(python))} {shlex.quote(str(entry))}"


def cmd_vm_icon(tart: str, vm: VmConfig) -> None:
    """Write a clickable app that starts the guest and shows its screen.

    The environment is baked in rather than read at click time: a Dock icon gets
    almost none of a login shell's environment, so a CIB_VM_NAME that only exists
    in the user's shell profile would silently open the wrong VM.
    """
    browser = chosen_browser()
    # Named for what it opens: three launchers called the same thing would be a
    # worse Dock than none, and the icon alone is not enough at Dock size.
    label = "Browsers" if browser.key == cibbrowsers.ALL else browser.label
    name = f"{label} in a Box"
    if vm.name != DEFAULT_VM_NAME:
        name = f"{name} ({vm.name})"
    bundle = APPS_DIR / f"{name}.app"
    settings = " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in (("CIB_VM_NAME", vm.name), ("CIB_VM_VIEWER", vm.viewer))
    )
    # AppleScript rather than a shell script as the bundle's executable. A script
    # named in CFBundleExecutable is launched by LaunchServices and then simply does
    # not run — no output, no error, nothing in the log. osacompile produces a real
    # signed bundle around the AppleScript runner, which does.
    #
    # The timeout is generous because a cold start builds nothing but does wait for
    # the guest to answer, and the default would give up first.
    command = f"{settings} {launcher_command()} vm open"
    script = (
        f"with timeout of {GUEST_WAIT_SECS + 60} seconds\n"
        f"  do shell script {applescript_string(command)}\n"
        f"end timeout\n"
    )
    APPS_DIR.mkdir(parents=True, exist_ok=True)
    # Removed first: osacompile refuses to overwrite a bundle it did not write, and
    # leaving a half-updated one behind would keep launching the old settings.
    shutil.rmtree(bundle, ignore_errors=True)
    result = run("/usr/bin/osacompile", "-o", str(bundle), "-e", script, check=False, capture=True)
    if result.returncode != 0:
        raise Failure(f"could not write {bundle}: {(result.stderr or result.stdout).strip()}")
    draw_icon(bundle, browser)
    print(f"Wrote {bundle}")
    print("Drag it to the Dock to keep it there.")


def draw_icon(bundle: Path, browser: cibbrowsers.Browser) -> None:
    """Give the launcher an icon that says what it opens.

    Chrome's own icns stops at 256 px, so copying it leaves the Dock upscaling —
    and it would be indistinguishable from the Chrome already on the machine,
    which is the one thing this launcher is not. cibicon draws the name instead:
    the browser coming up out of a cardboard box.
    """
    resources = bundle / "Contents" / "Resources"
    if not resources.is_dir():
        return
    try:
        with tempfile.TemporaryDirectory() as scratch:
            _build_icns(resources / "applet.icns", Path(scratch), browser.palette, browser.mark)
        # osacompile writes both CFBundleIconFile and CFBundleIconName, and on
        # modern macOS the name wins — it resolves out of the asset catalog, where
        # the AppleScript applet artwork lives. Replacing applet.icns alone then
        # changes nothing at all, which is a confusing way to fail.
        run(
            "/usr/bin/plutil",
            "-remove",
            "CFBundleIconName",
            str(bundle / "Contents" / "Info.plist"),
            check=False,
            capture=True,
        )
        (resources / "Assets.car").unlink(missing_ok=True)
    except (OSError, Failure):
        # Cosmetic. A launcher that works and looks plain beats refusing to write.
        return
    # Finder caches the icon against the bundle's modification date.
    bundle.touch()


# What macOS asks for. A missing size is not left blank — it is upscaled from
# whichever one is nearest, which is what "the icon looks soft" turns out to mean.
ICON_SIZES = (16, 32, 64, 128, 256, 512, 1024)


def _build_icns(target: Path, scratch: Path, palette: tuple = (), mark: str = "wheel") -> None:
    """Rasterise the drawing at every size macOS wants, then pack it."""
    source = scratch / "icon.pdf"
    source.write_bytes(cibicon.pdf(palette, mark))
    iconset = scratch / "cib.iconset"
    iconset.mkdir()
    for size in ICON_SIZES:
        # Each size is named twice — 32 is both icon_32x32 and icon_16x16@2x — and
        # iconutil wants the file present under both names.
        names = [f"icon_{size}x{size}.png"]
        if size > ICON_SIZES[0]:
            half = size // 2
            names.append(f"icon_{half}x{half}@2x.png")
        scaled = iconset / names[0]
        # Vector in, so every size is drawn rather than resampled from one bitmap.
        run(
            "/usr/bin/sips",
            "-s",
            "format",
            "png",
            "-z",
            str(size),
            str(size),
            str(source),
            "--out",
            str(scaled),
            check=False,
            capture=True,
        )
        if not scaled.exists():
            raise Failure(f"could not draw the icon at {size}px")
        for extra in names[1:]:
            shutil.copyfile(scaled, iconset / extra)
    if run(
        "/usr/bin/iconutil", "-c", "icns", "-o", str(target), str(iconset), check=False
    ).returncode:
        raise Failure(f"could not write {target}")


def applescript_string(text: str) -> str:
    """AppleScript has one escape and it is the backslash, applied to itself first."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
            "with.\n"
            "To re-install the key: 'cib vm down', then 'cib vm prepare', then "
            "'cib vm up' — prepare refuses while the guest is running, and the guest "
            "has to be running for you to have seen this."
        )


def cmd_vm_setup(tart: str, vm: VmConfig) -> None:
    """Finish the guest from here: everything after Setup Assistant."""
    ip = vm_ip(tart, vm)
    print(f"Installing Chrome on {vm.user}@{ip} ...")
    if install_browsers(vm, ip, guest_password()) != 0:
        raise Failure(
            f"installing Chrome on the guest at {ip} failed (see above). The offline "
            "build turns Remote Login on and installs cib's key; if the connection "
            "itself was refused, re-install both with 'cib vm down', then "
            "'cib vm prepare', then 'cib vm up'."
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
    # Before the delete: on a host still in the flat layout the per-name paths do
    # not exist yet, so the unlinks below would hit nothing and the very next
    # command would migrate the originals straight back in.
    migrate_flat_secrets()
    result = run(tart, "delete", vm.name, check=False, capture=True)
    if result.returncode != 0:
        # Only on success. A delete that failed leaves the guest where it was, and
        # taking its key and password away would lock cib out of a live VM.
        raise Failure(
            f"could not delete {vm.name!r}: {(result.stderr or result.stdout).strip()}\n"
            "Its password and keys are kept, so nothing is locked out."
        )
    # The password and the keys belong to the VM that is gone. Left behind, the next
    # build silently reuses them — and 'cib vm password' keeps printing a password
    # for a guest that no longer exists.
    #
    # The whole directory goes, rather than a list of names. SECRETS is per VM name,
    # so nothing else lives here; a list is what left vm-last-ip behind, so a fresh
    # build inherited the deleted guest's address and probed it forever.
    #
    # Checked again right here, not just in VmConfig.check(): SECRETS is built at
    # import from the raw environment, so a name that would widen this to every VM's
    # secrets — or to ~/.config — must not reach rmtree even if the check moves.
    expected = config_root() / vm.name
    if SECRETS.resolve() != expected.resolve():
        raise Failure(f"refusing to delete {SECRETS}: that is not {vm.name!r}'s own directory")
    shutil.rmtree(SECRETS, ignore_errors=True)
    print("Deleted.")


VM_ACTIONS = {
    "create": cmd_vm_create,
    "up": cmd_vm_up,
    "prepare": cmd_vm_prepare,
    "setup": cmd_vm_setup,
    "ssh": cmd_vm_ssh,
    "ip": cmd_vm_ip,
    "viewer": cmd_vm_viewer,
    "open": cmd_vm_open,
    "icon": cmd_vm_icon,
    "password": cmd_vm_password,
    "login": cmd_vm_login,
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
  create   build it, start it and install Chrome — the whole thing, one command
  prepare  redo just the offline preparation on an already-built VM
  up       start it again after 'cib vm down'; a window opens
  setup    redo just the Chrome and clipboard-agent install, over SSH
  ssh      open a shell in the guest
  ip       print the guest's address
  viewer   print the address of the guest's screen (CIB_VM_VIEWER=vnc only)
  open     start it if it is stopped, then put its screen in front of you
  icon     write a clickable app into ~/Applications that runs 'vm open'
  password print the generated guest account password (copy it, do not retype it)
  login    print the guest account name and password together
  down     stop it
  status   list VMs and their state
  delete   delete the VM and everything in it (asks first)

`create` never shows Setup Assistant. Instead of typing into it, it writes the
state Setup Assistant would have produced onto the guest's disk before its first
real boot: the account, autologin, Remote Login, and this host's keyboard layout.
That one step needs sudo on the host; nothing else does. The account password is
generated, so you never have to type it.

`cib vm up` stays in the foreground and Ctrl-C there stops the guest, so run
anything after it from a second terminal. `create` does not: it starts the guest
as a child that outlives it, which is what lets one command finish the job.

`create` finishes with a running guest that has Chrome in it: it boots the VM
itself, waits for it to answer, and installs Chrome, the clipboard agent and the
shared downloads folder. `up`, `setup` and `prepare` are still there for redoing
one part without the rest. That connection is by key, not by password: the build generates
one and installs it, along with the guest's own host key, so cib can verify the
guest on the very first connection. Nothing asks you to type anything.

Downloads in the guest land in ~/Downloads/chrome-vm on the host (CIB_VM_SHARE).

One thing stays manual, because Apple makes it interactive on purpose: signing in
to the Apple Account. That switches iCloud Keychain on by itself, so there is no
second toggle to find — joining the sync circle is a handshake with a device
already in it, not a setting any script could write."""


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
            "       CIB_VM_VIEWER=vnc for a window that can go full screen,\n"
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
            # Once, here, rather than in whichever helper happens to read the
            # secrets first: 'cib vm ssh' reads them through ssh_options() without
            # ever calling guest_password(), so it was the one command that failed
            # on a pre-1.4 install while every other one repaired it.
            migrate_flat_secrets()
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
