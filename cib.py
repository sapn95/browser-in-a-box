#!/usr/bin/env python3
"""chrome-in-a-box — real Google Chrome in an isolated container, used from your
own browser tab.

Single file, standard library only: no venv, no pip install. Everything runs on a
container engine (podman or docker); the web UI is bound to 127.0.0.1, so the
browser is never reachable from the network.
"""

from __future__ import annotations

import argparse
import os
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NoReturn

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
xrandr -s "$RES" >/dev/null || echo "could not set mode $RES (KasmVNC ships a fixed mode list)" >&2
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


@dataclass(frozen=True)
class Config:
    # Read at instantiation, not at import, so the environment is always current.
    image: str = field(default_factory=lambda: _env("CIB_IMAGE", DEFAULT_IMAGE))
    name: str = field(default_factory=lambda: _env("CIB_NAME", "chrome-in-a-box"))
    volume: str = field(default_factory=lambda: _env("CIB_VOLUME", "chrome-in-a-box-profile"))
    port: int = field(default_factory=lambda: int(_env("CIB_PORT", "6901")))
    resolution: str = field(default_factory=lambda: _env("CIB_RESOLUTION", "1920x1200"))
    password: str = field(default_factory=lambda: _env("CIB_PASSWORD", "chromeinabox"))
    wait_secs: int = field(default_factory=lambda: int(_env("CIB_WAIT_SECS", "120")))
    log_tail: str = field(default_factory=lambda: _env("CIB_LOG_TAIL", "200"))

    @property
    def url(self) -> str:
        return f"https://localhost:{self.port}/?resize=scale"

    def check(self) -> None:
        """Reject the settings that are known to kill the container, with an
        explanation, rather than failing obscurely minutes later."""
        if len(self.password) < MIN_PASSWORD_LEN:
            raise Failure(
                f"CIB_PASSWORD must be at least {MIN_PASSWORD_LEN} characters; "
                "KasmVNC refuses to start with a shorter one"
            )
        try:
            width, height = (int(part) for part in self.resolution.lower().split("x"))
        except ValueError:
            raise Failure(
                f"CIB_RESOLUTION must look like 1920x1200, got {self.resolution!r}"
            ) from None
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
    engine: str, *args: str, check: bool = True, capture: bool = False
) -> subprocess.CompletedProcess[str]:
    # Argument list is built here and never passed through a shell.
    return subprocess.run(  # noqa: S603
        [engine, *args],
        check=check,
        capture_output=capture,
        text=True,
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
    raise Failure(f"the web UI did not come up within {cfg.wait_secs}s; check './run.sh logs'")


def ensure_desktop(engine: str, cfg: Config) -> bool:
    """Apply the resolution and make sure Chrome is running. Returns False and
    warns on trouble, rather than failing the whole command."""
    result = run(
        engine,
        "exec",
        "-e",
        f"RES={cfg.resolution}",
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
    result = run(engine, "rm", "-f", cfg.name, check=False, capture=True)
    if result.returncode == 0:
        print(f"Stopped. The browser profile is kept in volume {cfg.volume!r}.")
    else:
        print("Not running.")


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cib",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment overrides: CIB_PORT, CIB_RESOLUTION, CIB_WAIT_SECS, CIB_ENGINE,\n"
            "CIB_IMAGE, CIB_NAME, CIB_VOLUME, CIB_PASSWORD, CIB_LOG_TAIL,\n"
            "CIB_FORCE=1 (recreate a running container instead of reusing it)."
        ),
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("up", help="start the container and wait until Chrome is running")
    sub.add_parser("down", help="stop and remove the container (the profile is kept)")
    sub.add_parser("open", help="open the web UI in your browser")
    sub.add_parser("status", help="show the container state")
    logs = sub.add_parser("logs", help="show the last log lines")
    logs.add_argument("-f", "--follow", action="store_true", help="follow the log")
    sub.add_parser("shell", help="open a shell inside the container")
    sub.add_parser("engine", help="print the container engine that will be used")
    sub.add_parser("reset", help="delete the browser profile volume (asks first)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    command = args.command or "up"

    try:
        cfg = Config()
        engine = find_engine()
        if command == "logs":
            cmd_logs(engine, cfg, follow=args.follow)
        else:
            handlers = {
                "up": cmd_up,
                "down": cmd_down,
                "open": cmd_open,
                "status": cmd_status,
                "shell": cmd_shell,
                "engine": cmd_engine,
                "reset": cmd_reset,
            }
            handlers[command](engine, cfg)
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
        return exc.returncode
    except KeyboardInterrupt:
        return 130
    return 0


def _entrypoint() -> NoReturn:
    sys.exit(main())


if __name__ == "__main__":
    _entrypoint()
