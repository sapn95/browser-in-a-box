"""Tests for bib.py.

Nothing here touches a real container engine: `run` is replaced with a recorder,
so the actual command construction is asserted instead of being described.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bib
import bibbrowsers
import bibicon
import bibpatch


@pytest.fixture(autouse=True)
def isolate_secrets(tmp_path, monkeypatch):
    """No test may reach the real ~/.config/browser-in-a-box.

    Found the hard way, during a real build: running `pytest` deleted a live VM's
    password and both key pairs, because two delete tests called cmd_vm_delete
    against the module-level paths. Per-test fixtures were not enough — this has to
    hold for every test, including ones written later.
    """
    home = tmp_path / "isolated-home"
    secrets_dir = home / ".config" / "browser-in-a-box" / "browser-vm"
    secrets_dir.mkdir(parents=True)
    monkeypatch.setattr(bib.Path, "home", classmethod(lambda cls: home))
    # Derived, not a hand-written list of names. The list version held CREDENTIALS,
    # VM_KEY, VM_HOST_KEY and KNOWN_HOSTS but not the remembered address, added later —
    # so the suite wrote its 10.0.0.9 test constant into a live VM's real
    # vm-last-ip. Anything added tomorrow is covered without a person remembering.
    for name, value in list(vars(bib).items()):
        if isinstance(value, Path) and value.parent == bib.SECRETS:
            monkeypatch.setattr(bib, name, secrets_dir / value.name)
    monkeypatch.setattr(bib, "SECRETS", secrets_dir)
    # And no test may start a real VM. start_detached uses subprocess.Popen
    # directly, so a test that replaces only `bib.run` would spawn tart for real and
    # then sit in wait_for_guest for five minutes. Tests that care replace these.
    real = SimpleNamespace(start_detached=bib.start_detached, wait_for_guest=bib.wait_for_guest)
    monkeypatch.setattr(bib, "start_detached", lambda tart, vm: _FakeBoot())
    monkeypatch.setattr(bib, "wait_for_guest", lambda tart, vm, boot: "192.168.1.50")
    # Handed back, so the two tests that exercise the real ones can ask for them
    # rather than reaching around the guard.
    return real


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """bib is configured by BIB_* variables, so a developer who actually uses the
    tool would otherwise fail its tests."""
    # The settings file is read once at import, from the real home. Left alone, a
    # developer who actually configured bib would be testing their own settings —
    # the same way a hand-written path list once let tests reach real secrets.
    monkeypatch.setattr(bib, "CONFIG", {})
    for name in [k for k in os.environ if k.startswith("BIB_")]:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def calls(monkeypatch):
    """Record every engine invocation and return success by default."""

    class Calls(list):
        env: ClassVar[list] = []

    recorded = Calls()
    recorded_env: list = []

    def fake_run(engine, *args, check=True, capture=False, env=None):
        recorded.append([engine, *args])
        recorded_env.append(env)
        return subprocess.CompletedProcess([engine, *args], 0, stdout="", stderr="")

    monkeypatch.setattr(bib, "run", fake_run)
    monkeypatch.setattr(bib, "find_engine", lambda: "podman")
    recorded.env = recorded_env
    return recorded


def flat(calls: list[list[str]]) -> str:
    return "\n".join(" ".join(call) for call in calls)


# --- configuration ------------------------------------------------------------


def test_defaults_are_the_values_the_container_needs():
    cfg = bib.Config()
    assert len(cfg.password) >= bib.MIN_PASSWORD_LEN
    assert cfg.resolution == ""  # dynamic: the desktop follows the browser window
    assert cfg.port == 6901
    cfg.check()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("password", "abc", "at least 6 characters"),
        ("resolution", "2560x1600", "not one of the modes KasmVNC"),
        ("resolution", "1920x1201", "not one of the modes KasmVNC"),
        ("resolution", "huge", "must look like"),
        ("resolution", "1920", "must look like"),
    ],
)
def test_settings_that_kill_the_container_are_rejected(field, value, expected):
    with pytest.raises(bib.Failure, match=expected):
        bib.Config(**{field: value}).check()


def test_every_browser_image_is_pinned_to_a_version():
    # A floating :latest turns "it worked yesterday" into a coin toss, and a broken
    # image would arrive without a commit anywhere to point at.
    # expand(), not BROWSERS: "all" is a VM mode, not a browser, and has no image.
    for browser in bibbrowsers.expand(bibbrowsers.ALL):
        assert ":latest" not in browser.image, browser.key
        assert browser.image.startswith(f"docker.io/kasmweb/{browser.key}:"), browser.key


def test_an_unknown_browser_is_refused_by_name(monkeypatch):
    monkeypatch.setenv("BIB_BROWSER", "safari")
    with pytest.raises(bib.Failure, match="chrome, chromium, firefox"):
        bib.chosen_browser()


def test_the_jpeg_quality_stays_in_the_range_kasmvnc_accepts():
    # DynamicQualityMax=10 makes Xvnc exit with a fatal error.
    for key in ("DynamicQualityMin", "DynamicQualityMax"):
        value = int(bib.VNC_OPTIONS.split(f"{key}=")[1].split()[0])
        assert 0 <= value <= 9


def test_the_login_prompt_stays_disabled():
    assert "-DisableBasicAuth=1" in bib.VNC_OPTIONS


# --- engine resolution --------------------------------------------------------


def test_engine_prefers_podman(monkeypatch):
    monkeypatch.delenv("BIB_ENGINE", raising=False)
    monkeypatch.setattr(bib.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert bib.find_engine() == "/usr/bin/podman"


def test_engine_falls_back_to_docker(monkeypatch):
    monkeypatch.delenv("BIB_ENGINE", raising=False)
    monkeypatch.setattr(
        bib.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    assert bib.find_engine() == "/usr/bin/docker"


def test_an_unusable_engine_override_fails_loudly(monkeypatch):
    monkeypatch.setenv("BIB_ENGINE", "nope")
    monkeypatch.setattr(bib.shutil, "which", lambda name: None)
    with pytest.raises(bib.Failure, match="not on PATH"):
        bib.find_engine()


def test_no_engine_at_all_fails_loudly(monkeypatch):
    monkeypatch.delenv("BIB_ENGINE", raising=False)
    monkeypatch.setattr(bib.shutil, "which", lambda name: None)
    with pytest.raises(bib.Failure, match="need podman or docker"):
        bib.find_engine()


# --- up -----------------------------------------------------------------------


def test_up_binds_the_ui_to_localhost_only(calls, monkeypatch):
    monkeypatch.setattr(bib, "container_running", lambda *a: False)
    monkeypatch.setattr(bib, "wait_for_ui", lambda *a: None)
    monkeypatch.setattr(bib, "ensure_desktop", lambda *a: True)
    bib.cmd_up("podman", bib.Config())
    assert "-p 127.0.0.1:6901:6901" in flat(calls)


def test_up_asks_for_a_bridge_network(calls, monkeypatch):
    # kasm's startup script waits forever for a veth; rootless podman's default
    # network namespace has none, so the desktop never comes up without this.
    monkeypatch.setattr(bib, "container_running", lambda *a: False)
    monkeypatch.setattr(bib, "wait_for_ui", lambda *a: None)
    monkeypatch.setattr(bib, "ensure_desktop", lambda *a: True)
    bib.cmd_up("podman", bib.Config())
    assert "--network bridge" in flat(calls)


def test_up_reuses_a_healthy_container(calls, monkeypatch):
    monkeypatch.delenv("BIB_FORCE", raising=False)
    monkeypatch.setattr(bib, "container_running", lambda *a: True)
    monkeypatch.setattr(bib, "serves_requested_browser", lambda *a: True)
    monkeypatch.setattr(bib, "ui_is_up", lambda *a: True)
    monkeypatch.setattr(bib, "ensure_desktop", lambda *a: True)
    bib.cmd_up("podman", bib.Config())
    assert "run -d" not in flat(calls)
    assert "rm -f" not in flat(calls)


def test_a_container_serving_another_browser_is_not_reused(calls, monkeypatch):
    # `bib box up`, then `BIB_BROWSER=firefox bib box up`. The container's name does
    # not carry the browser and its UI answers either way, so this took the reuse
    # branch and printed "Already running" over a Chrome the user had not asked for.
    monkeypatch.delenv("BIB_FORCE", raising=False)
    monkeypatch.setattr(bib, "container_running", lambda *a: True)
    monkeypatch.setattr(bib, "serves_requested_browser", lambda *a: False)
    monkeypatch.setattr(bib, "ui_is_up", lambda *a: True)
    monkeypatch.setattr(bib, "wait_for_ui", lambda *a: None)
    monkeypatch.setattr(bib, "ensure_desktop", lambda *a: True)
    monkeypatch.setenv("BIB_BROWSER", "firefox")
    bib.cmd_up("podman", bib.Config())
    recorded = flat(calls)
    assert "rm -f" in recorded
    assert "run -d" in recorded
    assert bibbrowsers.BROWSERS["firefox"].image in recorded


def test_bib_force_recreates_a_healthy_container(calls, monkeypatch):
    monkeypatch.setenv("BIB_FORCE", "1")
    monkeypatch.setattr(bib, "container_running", lambda *a: True)
    # True, so that BIB_FORCE is the only thing left that can cause the recreate.
    monkeypatch.setattr(bib, "serves_requested_browser", lambda *a: True)
    monkeypatch.setattr(bib, "ui_is_up", lambda *a: True)
    monkeypatch.setattr(bib, "wait_for_ui", lambda *a: None)
    monkeypatch.setattr(bib, "ensure_desktop", lambda *a: True)
    bib.cmd_up("podman", bib.Config())
    assert "run -d" in flat(calls)


@pytest.mark.parametrize(
    "container_id,image_id,image_rc,expected",
    [
        ("sha256:aaa", "sha256:aaa", 0, True),
        ("sha256:aaa", "sha256:bbb", 0, False),
        # The wanted image is not pulled yet: not what was asked for, and recreating
        # is what pulls it.
        ("sha256:aaa", "", 125, False),
        # Neither inspect said anything. Two empty strings are equal, and without a
        # guard that read as "the same image".
        ("", "", 0, False),
    ],
)
def test_the_running_image_is_compared_by_id(
    monkeypatch, container_id, image_id, image_rc, expected
):
    def fake_run(engine, *args, check=True, capture=False, env=None):
        if args[0] == "image":
            return subprocess.CompletedProcess([], image_rc, stdout=image_id + "\n", stderr="")
        return subprocess.CompletedProcess([], 0, stdout=container_id + "\n", stderr="")

    monkeypatch.setattr(bib, "run", fake_run)
    assert bib.serves_requested_browser("podman", bib.Config()) is expected


def test_up_rejects_a_bad_setting_before_touching_the_engine(calls, monkeypatch):
    monkeypatch.setenv("BIB_PASSWORD", "abc")
    with pytest.raises(bib.Failure):
        bib.cmd_up("podman", bib.Config())
    assert calls == []


# --- readiness ----------------------------------------------------------------


def test_wait_for_ui_returns_once_the_ui_answers(monkeypatch):
    monkeypatch.setattr(bib, "ui_status", lambda cfg: 200)
    bib.wait_for_ui("podman", bib.Config())


def test_wait_for_ui_reports_a_returning_login_prompt(monkeypatch):
    monkeypatch.setattr(bib, "ui_status", lambda cfg: 401)
    with pytest.raises(bib.Failure, match="asking for a login"):
        bib.wait_for_ui("podman", bib.Config())


def test_wait_for_ui_reports_a_container_that_died_at_boot(calls, monkeypatch):
    monkeypatch.setattr(bib, "ui_status", lambda cfg: None)
    monkeypatch.setattr(bib, "container_running", lambda *a: False)
    with pytest.raises(bib.Failure, match="exited during boot"):
        bib.wait_for_ui("podman", bib.Config())


def test_wait_for_ui_gives_up_after_the_deadline(monkeypatch):
    monkeypatch.setattr(bib, "ui_status", lambda cfg: None)
    monkeypatch.setattr(bib, "container_running", lambda *a: True)
    monkeypatch.setattr(bib.time, "sleep", lambda seconds: None)
    with pytest.raises(bib.Failure, match="did not come up within 0s"):
        bib.wait_for_ui("podman", bib.Config(wait_secs=0))


# --- the remaining commands ---------------------------------------------------


def test_logs_does_not_follow_by_default(calls):
    bib.cmd_logs("podman", bib.Config())
    assert "logs --tail 200 browser-in-a-box" in flat(calls)
    assert "-f" not in flat(calls)


def test_logs_follows_when_asked(calls):
    bib.cmd_logs("podman", bib.Config(), follow=True)
    assert "logs -f browser-in-a-box" in flat(calls)


def test_reset_needs_confirmation(calls, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    bib.cmd_reset("podman", bib.Config())
    assert "volume rm" not in flat(calls)
    assert "Cancelled." in capsys.readouterr().out


def test_reset_deletes_the_volume_when_confirmed(calls, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    bib.cmd_reset("podman", bib.Config())
    assert "volume rm browser-in-a-box-profile" in flat(calls)


def test_reset_treats_a_closed_stdin_as_no(calls, monkeypatch):
    def raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    bib.cmd_reset("podman", bib.Config())
    assert "volume rm" not in flat(calls)


def test_down_removes_the_container(calls):
    bib.cmd_down("podman", bib.Config())
    assert "rm -f browser-in-a-box" in flat(calls)


def test_ensure_desktop_clears_a_stale_profile_lock_and_sets_the_mode():
    script = bib.desktop_script(bibbrowsers.BROWSERS["chrome"])
    assert "Singleton*" in script
    assert 'xrandr -s "$RES"' in script
    # Firefox leaves a different pair behind, and never a Singleton.
    firefox = bib.desktop_script(bibbrowsers.BROWSERS["firefox"])
    assert ".parentlock" in firefox
    assert "Singleton" not in firefox


def test_ensure_desktop_warns_instead_of_failing(monkeypatch, capsys):
    def failing_run(engine, *args, check=True, capture=False):
        return subprocess.CompletedProcess(
            [engine, *args], 1, stdout="", stderr="no such container"
        )

    monkeypatch.setattr(bib, "run", failing_run)
    assert bib.ensure_desktop("podman", bib.Config()) is False
    assert "warning" in capsys.readouterr().err


# --- the macOS VM variant -----------------------------------------------------


def test_the_vm_variant_refuses_on_a_non_mac(monkeypatch):
    monkeypatch.setattr(bib.platform, "system", lambda: "Linux")
    with pytest.raises(bib.Failure, match="needs macOS"):
        bib.find_tart()


def test_the_vm_variant_refuses_on_intel(monkeypatch):
    monkeypatch.setattr(bib.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(bib.platform, "machine", lambda: "x86_64")
    with pytest.raises(bib.Failure, match="Apple silicon"):
        bib.find_tart()


def test_a_missing_tart_points_at_the_install_command(monkeypatch):
    monkeypatch.setattr(bib.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(bib.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(bib.shutil, "which", lambda name: None)
    # Not on PATH and not at either Homebrew prefix. The fallback exists because a
    # Dock click has no shell profile and so no /opt/homebrew on PATH — but it must
    # not turn "tart is not installed" into a silent success on a machine that has
    # a stale binary at one of those paths.
    monkeypatch.setattr(bib.os, "access", lambda path, mode: False)
    with pytest.raises(bib.Failure, match="brew install"):
        bib.find_tart()


def test_tart_is_found_at_the_homebrew_prefix_when_path_is_bare(monkeypatch):
    """A Dock click runs with PATH=/usr/bin:/bin:/usr/sbin:/sbin and nothing else.

    The clickable app failed with "tart is not on PATH" on a machine where tart
    was plainly installed, because a shell profile is what puts Homebrew there.
    """
    monkeypatch.setattr(bib.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(bib.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(bib.shutil, "which", lambda name: None)
    monkeypatch.setattr(bib.os, "access", lambda path, mode: path == "/opt/homebrew/bin/tart")
    assert bib.find_tart() == "/opt/homebrew/bin/tart"


def test_vm_up_refuses_before_create(calls, monkeypatch):
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    with pytest.raises(bib.Failure, match="vm create"):
        bib.cmd_vm_up("tart", bib.VmConfig())


def test_vm_delete_needs_confirmation(calls, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    bib.cmd_vm_delete("tart", bib.VmConfig())
    assert "delete" not in flat(calls)


def test_vm_delete_removes_the_vm_when_confirmed(calls, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    bib.cmd_vm_delete("tart", bib.VmConfig())
    assert "delete browser-vm" in flat(calls)


def test_every_vm_action_is_reachable_from_the_cli(monkeypatch):
    parser = bib.build_parser()
    action = next(a for a in parser._actions if isinstance(a, bib.argparse._SubParsersAction))
    vm_parser = action.choices["vm"]
    choices = next(a.choices for a in vm_parser._actions if a.dest == "action")
    assert set(choices) == set(bib.VM_ACTIONS)


# --- cli ----------------------------------------------------------------------


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit) as excinfo:
        bib.main(["frobnicate"])
    assert excinfo.value.code != 0


def test_bare_invocation_prints_help_and_fails(capsys):
    assert bib.main([]) == 2
    assert "box" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["frobnicate"], ["box"], ["box", "frobnicate"], ["vm", "nope"]])
def test_unusable_invocations_are_rejected(argv):
    with pytest.raises(SystemExit) as excinfo:
        bib.main(argv)
    assert excinfo.value.code != 0


@pytest.mark.parametrize("action", sorted(bib.BOX_ACTIONS))
def test_every_box_action_dispatches(action, monkeypatch):
    monkeypatch.setattr(bib, "find_engine", lambda: "podman")
    called = []
    monkeypatch.setitem(bib.BOX_ACTIONS, action, lambda *a, **k: called.append(action))
    if action == "logs":
        monkeypatch.setattr(bib, "cmd_logs", lambda *a, **k: called.append(action))
    assert bib.main(["box", action]) == 0
    assert called == [action]


@pytest.mark.parametrize("action", sorted(bib.VM_ACTIONS))
def test_every_vm_action_dispatches(action, monkeypatch):
    monkeypatch.setattr(bib, "find_tart", lambda: "tart")
    called = []
    monkeypatch.setitem(bib.VM_ACTIONS, action, lambda *a, **k: called.append(action))
    assert bib.main(["vm", action]) == 0
    assert called == [action]


def test_the_readme_documents_every_command_the_cli_registers():
    # `bib vm viewer` was registered and left out of the table for two releases. A
    # list maintained by hand beside a dict is a list that drifts.
    readme = (Path(bib.__file__).resolve().parent / "README.md").read_text()
    for variant, actions in (("box", bib.BOX_ACTIONS), ("vm", bib.VM_ACTIONS)):
        for action in actions:
            assert f"`bib {variant} {action}`" in readme, f"bib {variant} {action} is undocumented"


def test_the_help_names_both_variants_and_their_trade_off(capsys):
    with pytest.raises(SystemExit):
        bib.main(["--help"])
    out = capsys.readouterr().out
    assert "bib box" in out and "bib vm" in out
    assert "iCloud Keychain" in out
    assert "Touch ID" in out


def test_follow_reaches_the_logs_command(monkeypatch):
    monkeypatch.setattr(bib, "find_engine", lambda: "podman")
    seen = {}
    monkeypatch.setattr(bib, "cmd_logs", lambda e, c, follow=False: seen.update(follow=follow))
    bib.main(["box", "logs", "-f"])
    assert seen == {"follow": True}


def test_failures_are_reported_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(bib, "find_engine", lambda: (_ for _ in ()).throw(bib.Failure("boom")))
    assert bib.main(["box", "status"]) == 1
    assert "error: boom" in capsys.readouterr().err


# --- the Homebrew formula updater ---------------------------------------------


def _formula() -> str:
    return (Path(__file__).resolve().parents[1] / "Formula" / "bib.rb").read_text()


def _updater():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import update_formula

    return update_formula


def test_init_asks_and_writes_a_file_the_rest_of_bib_reads_back(tmp_path, monkeypatch):
    # The settings file existed for a month and nobody knew, because finding it
    # meant reading the README. Asking is the discoverable version of that.
    path = tmp_path / "bib.yaml"
    monkeypatch.setenv("BIB_CONFIG", str(path))
    answers = iter(["firefox", "all", "vm-two", "", "random", "", ""])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    bib.cmd_init()

    bib.CONFIG = bib.load_config()
    assert bib.Config().browser == "firefox"
    assert bib.VmConfig().browser == bibbrowsers.ALL, "the vm section names its own browser"
    assert bib.VmConfig().name == "vm-two"
    assert bib.VmConfig().user == "admin", "return keeps the default"


def test_init_re_asks_rather_than_rejecting_a_typo(monkeypatch):
    # Seven questions, and losing the six right ones to a typo in the last is not
    # an acceptable way to answer a questionnaire.
    answers = iter(["chrom", "chrome"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert bib.ask("Browser", "chrome", ("chrome", "firefox")) == "chrome"


def test_init_leaves_an_existing_file_alone_unless_told(tmp_path, monkeypatch):
    path = tmp_path / "bib.yaml"
    path.write_text("box:\n  port: 7000\n")
    monkeypatch.setenv("BIB_CONFIG", str(path))
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    bib.cmd_init()
    assert path.read_text() == "box:\n  port: 7000\n"


def test_the_readme_documents_exactly_the_settings_and_commands_that_exist():
    """The README is the only documentation there is, so a setting it forgets is a
    setting nobody finds, and one it invents is worse.

    Checked rather than proof-read: this file has been renamed twice and gained a
    browser table, and every pass through it by hand missed something.
    """
    root = Path(bib.__file__).resolve().parent
    readme = (root / "README.md").read_text()
    code = (root / "bib.py").read_text()

    in_code = set(re.findall(r'"(BIB_[A-Z_]+)"', code))
    in_readme = set(re.findall(r"\bBIB_[A-Z_]+", readme))
    assert not in_code - in_readme, f"undocumented settings: {sorted(in_code - in_readme)}"
    assert not in_readme - in_code, f"settings that do not exist: {sorted(in_readme - in_code)}"

    for noun, actions, pattern in (
        ("box", bib.BOX_ACTIONS, r"`bib box (\w+)`"),
        ("vm", bib.VM_ACTIONS, r"`bib vm (\w+)`"),
    ):
        documented = set(re.findall(pattern, readme))
        assert not set(actions) - documented, f"{noun}: undocumented {set(actions) - documented}"
        assert not documented - set(actions), f"{noun}: invented {documented - set(actions)}"


def test_the_formula_points_at_the_version_this_code_is(monkeypatch):
    # The fourth place the version is written, and the only one nothing checked.
    # The rename rewrote the formula's asset names to bib-* and left version "2.0.0"
    # in place, so every url pointed at a v2.0.0 release that published cib-* — a
    # 404 for anyone following the README's `brew install`. The release guard checks
    # bib.py, pyproject.toml and uv.lock against the tag; this covers the formula.
    formula = _formula()
    assert f'version "{bib.__version__}"' in formula
    for arch in ("macos-arm64", "linux-arm64", "linux-x86_64"):
        expected = (
            f"https://github.com/sapn95/browser-in-a-box/releases/download/"
            f"v{bib.__version__}/bib-{arch}.tar.gz"
        )
        assert expected in formula, f"{arch} url does not match the version"


def test_the_formula_description_passes_the_rules_brew_style_enforces():
    # The description is the tap's now, not just this repository's. The tap used
    # to rewrite only the version and the checksums, so a formula body here
    # reached nobody; once it started taking the whole file, `brew style` on the
    # tap began reporting this repository's description. It said "A real,
    # unmanaged browser in a box", and Homebrew does not allow an article there.
    #
    # Checked here rather than by running `brew style`, which would mean putting
    # Homebrew on a Linux runner to lint one string.
    line = next(row for row in _formula().splitlines() if row.strip().startswith("desc "))
    description = line.split('"', 1)[1].rsplit('"', 1)[0]

    first = description.split(" ", 1)[0].lower()
    assert first not in {"a", "an", "the"}, f"desc starts with an article: {description}"
    assert not description.lower().startswith("bib"), (
        f"desc repeats the formula name: {description}"
    )
    # "etc." is the one ending Homebrew lets through, and it is the only way to
    # write that word.
    assert not description.endswith(".") or description.endswith("etc."), (
        f"desc ends with a full stop: {description}"
    )
    # The limit is on "name: desc", not on the source line: measuring the line
    # counts the indentation, the keyword and two quotes, and would report a
    # length nobody can act on.
    labelled = f"bib: {description}"
    assert len(labelled) <= 80, f"'{labelled}' is {len(labelled)} characters"


ZERO = "0" * 64
ONE = "1" * 64


def test_the_formula_updater_sets_version_urls_and_every_checksum():
    out = _updater().update(
        _formula(),
        "2.3.4",
        {"macos-arm64": ONE, "linux-arm64": ONE, "linux-x86_64": ONE},
    )
    assert 'version "2.3.4"' in out
    assert out.count("/download/v2.3.4/") == 3
    for digest in re.findall(r'sha256 "([0-9a-f]{64})"', _formula()):
        assert digest not in out
    assert out.count(ONE) == 3


def test_the_formula_updater_refuses_an_asset_it_cannot_find():
    with pytest.raises(SystemExit, match="expected 1"):
        _updater().update(_formula(), "2.3.4", {"windows-x86_64": ONE})


def test_the_formula_updater_is_idempotent():
    once = _updater().update(_formula(), "2.3.4", {"macos-arm64": ONE})
    assert _updater().update(once, "2.3.4", {"macos-arm64": ONE}) == once


def test_the_formula_updater_rejects_a_bad_checksum(tmp_path):
    formula = tmp_path / "bib.rb"
    formula.write_text(_formula())
    with pytest.raises(SystemExit, match="no valid sha256"):
        _updater().main(["2.3.4", str(formula), "macos-arm64=nope"])


# --- VM networking ------------------------------------------------------------


def test_the_vm_uses_bridged_networking_by_default():
    # Shared networking hands out a vmnet gateway that does not always answer DNS,
    # which leaves the guest with an address but no name resolution.
    args = bib.vm_run_args(bib.VmConfig())
    assert "--net-bridged=en0" in args
    assert args[-1] == "browser-vm"


def test_the_vm_network_mode_and_interface_are_overridable(monkeypatch):
    monkeypatch.setenv("BIB_VM_INTERFACE", "en1")
    assert "--net-bridged=en1" in bib.vm_run_args(bib.VmConfig())
    monkeypatch.setenv("BIB_VM_NET", "shared")
    args = bib.vm_run_args(bib.VmConfig())
    assert not any(a.startswith("--net-") for a in args)
    monkeypatch.setenv("BIB_VM_NET", "host")
    assert "--net-host" in bib.vm_run_args(bib.VmConfig())


def test_a_failed_bridged_start_explains_the_alternatives(monkeypatch):
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: True)
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    with pytest.raises(bib.Failure, match="BIB_VM_NET=shared"):
        bib.cmd_vm_up("tart", bib.VmConfig())


# --- taking the guest over from here ------------------------------------------


@pytest.fixture
def resolving(monkeypatch):
    """Record engine calls and answer the ip lookup with an address."""
    recorded: list[list[str]] = []

    def fake_run(engine, *args, check=True, capture=False, env=None):
        recorded.append([engine, *args])
        return subprocess.CompletedProcess([engine, *args], 0, stdout="192.168.1.50\n", stderr="")

    monkeypatch.setattr(bib, "run", fake_run)
    return recorded


def test_a_bridged_guest_is_resolved_by_arp(resolving):
    # Bridged guests get their address from the real network, so tart's default
    # DHCP-lease resolver has nothing to read.
    assert bib.vm_ip("tart", bib.VmConfig()) == "192.168.1.50"
    assert "ip --resolver arp --wait 60 browser-vm" in flat(resolving)


def test_a_shared_guest_is_resolved_by_dhcp(resolving, monkeypatch):
    monkeypatch.setenv("BIB_VM_NET", "shared")
    bib.vm_ip("tart", bib.VmConfig())
    assert "--resolver dhcp" in flat(resolving)


def test_an_unresolvable_guest_is_reported_clearly(monkeypatch):
    # Not "past Setup Assistant": the default path never shows one, so naming it
    # sent people looking for a screen that does not exist.
    monkeypatch.setattr(
        bib,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="no such VM"),
    )
    with pytest.raises(bib.Failure, match="bib vm status") as caught:
        bib.vm_ip("tart", bib.VmConfig())
    assert "Setup Assistant" not in str(caught.value)
    assert "no such VM" in str(caught.value), "tart's own reason must reach the user"


def test_the_ssh_command_verifies_the_host_key_it_planted(credentials):
    # It used to be StrictHostKeyChecking=no against /dev/null, which accepts any
    # peer answering on the guest's address — and the script sent over that
    # connection carries the guest's password for sudo.
    cmd = bib.ssh_command(bib.VmConfig(), "192.168.1.50")
    assert cmd[0].endswith("ssh")
    assert "StrictHostKeyChecking=yes" in cmd
    assert "StrictHostKeyChecking=no" not in cmd
    assert f"UserKnownHostsFile={bib.KNOWN_HOSTS}" in cmd
    assert "PasswordAuthentication=no" in cmd
    assert cmd[cmd.index("-i") + 1] == str(bib.VM_KEY)
    assert cmd[-1] == "admin@192.168.1.50"


def test_the_known_hosts_entry_pins_the_guests_key_at_any_address(credentials):
    # The guest's address changes with every lease, and this file is used for
    # nothing but connections to that one guest, so a wildcard is the pin.
    bib.ensure_vm_keys()
    entry = bib.KNOWN_HOSTS.read_text().strip()
    assert entry.startswith("* ssh-ed25519 ")
    assert entry.endswith(bib.VM_HOST_KEY.with_suffix(".pub").read_text().strip())


def test_the_keys_are_generated_once_and_kept(tmp_path, monkeypatch):
    # Regenerating them would lock bib out of the guest it already built.
    monkeypatch.setattr(bib, "VM_KEY", tmp_path / "vm-key")
    monkeypatch.setattr(bib, "VM_HOST_KEY", tmp_path / "vm-host-key")
    monkeypatch.setattr(bib, "KNOWN_HOSTS", tmp_path / "vm-known-hosts")
    bib.ensure_vm_keys()  # real ssh-keygen
    assert (tmp_path / "vm-key").exists()
    first = (tmp_path / "vm-key").read_bytes()
    assert (tmp_path / "vm-key.pub").read_text().startswith("ssh-ed25519 ")
    bib.ensure_vm_keys()
    assert (tmp_path / "vm-key").read_bytes() == first


def test_a_keygen_that_produced_nothing_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(bib, "VM_KEY", tmp_path / "vm-key")
    monkeypatch.setattr(bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0))
    with pytest.raises(bib.Failure, match="did not produce"):
        bib.ensure_vm_keys()


def test_the_ssh_user_is_overridable(monkeypatch):
    monkeypatch.setenv("BIB_VM_USER", "sapn")
    assert bib.ssh_command(bib.VmConfig(), "10.0.0.1")[-1] == "sapn@10.0.0.1"


def test_setup_installs_chrome_and_is_idempotent():
    assert "googlechrome.dmg" in bib.guest_install_script("pw")
    assert "already installed" in bib.guest_install_script("pw")
    assert bib.guest_install_script("pw").startswith("set -eu")


def test_setup_points_at_prepare_not_at_a_switch_it_already_turned_on(
    credentials, monkeypatch, capsys
):
    # The offline build enables Remote Login itself, so telling the user to go and
    # turn it on sent them looking for a setting that is already set.
    bib.guest_password(create=True)
    monkeypatch.setattr(bib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(bib, "guest_ssh", lambda *a, **k: 255)
    with pytest.raises(bib.Failure, match="bib vm prepare") as caught:
        bib.cmd_vm_setup("tart", bib.VmConfig())
    assert "turn on" not in str(caught.value)


def test_setup_passes_the_install_script_to_the_guest(credentials, monkeypatch):
    seen = {}
    password = bib.guest_password(create=True)
    monkeypatch.setattr(bib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(
        bib, "guest_ssh", lambda vm, ip, script=None: seen.update(script=script) or 0
    )
    bib.cmd_vm_setup("tart", bib.VmConfig())
    # The generated password has to reach the guest: sshd runs this with no tty and
    # no cached credential, so sudo there can only be fed one.
    assert seen["script"] == bib.guest_install_script(password, bib.host_time_zone()[0])
    assert password in seen["script"]


def test_the_guest_password_never_reaches_a_process_list(credentials, monkeypatch):
    # `ssh host "<script>"` would put the whole script, password included, in this
    # host's argv. It is read on stdin instead.
    # A generated one on purpose: the default password is the word "admin", which is
    # also the account name and therefore legitimately in the command. Asserting on
    # that would pass on the username and prove nothing about the password.
    monkeypatch.setenv("BIB_VM_PASSWORD", bib.RANDOM_PASSWORD)
    password = bib.guest_password(create=True)
    script = bib.guest_install_script(password)
    argv = bib.ssh_command(bib.VmConfig(), "192.168.1.50", script)
    assert password not in " ".join(argv)
    assert argv[-2:] == ["/bin/sh", "-s"]


def test_an_interactive_shell_keeps_this_terminals_stdin(credentials, monkeypatch):
    # 'bib vm ssh' has no script; feeding it one would close stdin immediately.
    seen = {}
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or subprocess.CompletedProcess(cmd, 0),
    )
    bib.guest_ssh(bib.VmConfig(), "192.168.1.50")
    assert seen["kw"]["input"] is None
    assert "-s" not in seen["cmd"]


# --- the unattended build -----------------------------------------------------


@pytest.fixture
def credentials():
    # Nothing is redirected here. isolate_secrets is autouse and has already moved
    # every module-level path under SECRETS into this test's own directory; a second
    # hand-written list of names beside it is what let the state file slip through once.
    # Stand-ins rather than real ssh-keygen output: these tests replace `run`, so a
    # real keygen would never happen. ensure_vm_keys() then only rewrites
    # known_hosts, which is the part they care about.
    for key in (bib.VM_KEY, bib.VM_HOST_KEY):
        key.write_text(f"PRIVATE {key.name}\n")
        key.with_suffix(".pub").write_text(f"ssh-ed25519 AAAAC3Nz-{key.name} bib\n")
    return bib.CREDENTIALS


def test_no_module_level_path_still_points_at_the_real_home(tmp_path):
    """The guard has to cover paths nobody has written yet.

    Twice now a path added to bib.py was not added to the fixture, and the suite
    wrote into the real ~/.config/browser-in-a-box — the second time replacing a
    live VM's remembered address with 10.0.0.9. This fails the moment a new
    module-level path escapes, instead of waiting for a user to notice.
    """
    # Not Path.home(): isolate_secrets patches that, so it would return the fake one
    # and every path would look safe. os.path.expanduser reads HOME, untouched here.
    # PATCHER and PACKER_TEMPLATE are deliberately not covered: they are read-only
    # paths into the installed package, and bib never writes to them.
    real = Path(os.path.expanduser("~")) / ".config" / "browser-in-a-box"
    escaped = [
        name
        for name, value in vars(bib).items()
        if isinstance(value, Path) and (value == real or real in value.parents)
    ]
    assert escaped == [], f"these still point at the real secrets directory: {escaped}"
    # Non-vacuous: there are paths of this shape, and they were moved rather than
    # simply absent.
    assert bib.SECRETS.is_relative_to(tmp_path)
    assert bib.STATE.is_relative_to(tmp_path)


def test_the_guest_password_defaults_to_admin_and_is_remembered(credentials):
    first = bib.guest_password(create=True)
    assert first == "admin"
    assert bib.guest_password() == first
    assert credentials.stat().st_mode & 0o777 == 0o600


def test_the_guest_password_can_be_generated_on_request(credentials, monkeypatch):
    monkeypatch.setenv("BIB_VM_PASSWORD", bib.RANDOM_PASSWORD)
    first = bib.guest_password(create=True)
    assert len(first) >= 20
    assert first != bib.DEFAULT_VM_PASSWORD
    # Remembered like any other: the value is written down, not re-derived.
    assert bib.guest_password() == first


def test_asking_for_a_password_before_the_build_says_so(credentials):
    with pytest.raises(bib.Failure, match="build the VM first"):
        bib.guest_password()


def test_create_drives_packer_with_the_generated_password(calls, credentials, monkeypatch):
    monkeypatch.setenv("BIB_VM_PACKER", "1")  # this covers the fallback path
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "find_packer", lambda: "packer")
    bib.cmd_vm_create("tart", bib.VmConfig())
    out = flat(calls)
    assert "packer build" in out
    assert str(bib.PACKER_TEMPLATE) in out
    # The password travels in the environment, never in argv.
    assert "password=" not in out
    assert any(
        e and e.get("PKR_VAR_password") == credentials.read_text().strip() for e in calls.env
    )
    assert "memory_gb=8" in out  # 8192 MB, passed to packer in GB


def test_a_missing_packer_points_at_the_install_command(monkeypatch):
    monkeypatch.setattr(bib.shutil, "which", lambda name: None)
    with pytest.raises(bib.Failure, match="brew install hashicorp/tap/packer"):
        bib.find_packer()


def test_the_template_drives_setup_assistant_and_keeps_gatekeeper():
    template = (Path(__file__).resolve().parents[1] / bib.PACKER_TEMPLATE).read_text()
    assert "boot_command" in template
    # The region is typed on a US layout during setup; the Swiss layout is applied
    # afterwards, by name and with an integer id (a string id is ignored).
    assert "united states" in template
    assert '"Swiss German"' in template
    assert "keyboard_layout_id" in template
    # sudo has no tty in a provisioner, so it must be fed the password.
    assert "sudo -S systemsetup" in template
    # The time-zone field searches for a city, not an Olson id.
    assert "timezone_city" in template
    # The upstream template it is based on disables Gatekeeper for CI images.
    assert "spctl --global-disable" not in template
    assert "assessments enabled" in template
    # The Apple ID pane is skipped: 2FA cannot be automated.
    assert "skip signing in with an Apple ID" in template


def test_the_desktop_follows_the_window_unless_a_size_is_forced():
    # ?resize=remote asks KasmVNC to match the client; pinning a mode would fight it.
    assert "resize=remote" in bib.Config().url
    assert 'if [ -n "$RES" ]' in bib.desktop_script(bibbrowsers.BROWSERS["chrome"])


def test_an_empty_resolution_passes_preflight():
    bib.Config(resolution="").check()


# --- what the review found ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("BIB_PORT", "abc", "whole number"),
        ("BIB_VM_MEMORY", "8g", "whole number"),
        ("BIB_VM_DISK", "abc", "whole number"),
        ("BIB_VM_CPUS", "0", "at least 2"),
        ("BIB_VM_MEMORY", "512", "at least 4096"),
    ],
)
def test_a_bad_numeric_setting_is_an_error_not_a_traceback(name, value, expected, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(bib.Failure, match=expected):
        bib.Config() if name == "BIB_PORT" else bib.VmConfig()


def test_memory_is_rounded_up_not_truncated(calls, credentials, monkeypatch):
    monkeypatch.setenv("BIB_VM_PACKER", "1")  # this covers the fallback path
    # 8000 MB is 8 GB worth of intent; truncating gives the guest 7.
    monkeypatch.setenv("BIB_VM_MEMORY", "8000")
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "find_packer", lambda: "packer")
    bib.cmd_vm_create("tart", bib.VmConfig())
    assert "memory_gb=8" in flat(calls)


def test_the_generated_password_avoids_layout_dependent_characters(credentials, monkeypatch):
    # It is typed into the guest as keystrokes, and -, _, y, z move between the US
    # and Swiss German layouts, so such a password would never match what was saved.
    # Generate real ones rather than restating the alphabet.
    monkeypatch.setenv("BIB_VM_PASSWORD", bib.RANDOM_PASSWORD)
    forbidden = set("yzYZ-_/")
    for _ in range(200):
        credentials.unlink(missing_ok=True)
        password = bib.guest_password(create=True)
        assert len(password) >= 20
        assert not (set(password) & forbidden), password


def test_the_default_password_is_typeable_in_the_guest_too():
    # admin goes in through the same keystroke path as a generated one, so it has to
    # obey the same rule about keys that move between layouts.
    assert not set(bib.DEFAULT_VM_PASSWORD) & set("yzYZ-_/")


def test_an_empty_credentials_file_is_not_treated_as_a_password(credentials):
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("  \n")
    with pytest.raises(bib.Failure, match="build the VM first"):
        bib.guest_password()


def test_the_password_file_is_never_briefly_world_readable(credentials, monkeypatch):
    seen = {}
    real_open = os.open

    def spy(path, flags, mode=0o777, **kwargs):
        if str(path) == str(credentials):
            seen["mode"] = mode
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    bib.guest_password(create=True)
    assert seen["mode"] == 0o600


def test_an_unknown_network_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("BIB_VM_NET", "bridge")  # a typo for "bridged"
    with pytest.raises(bib.Failure, match="BIB_VM_NET must be"):
        bib.vm_run_args(bib.VmConfig())


def test_a_user_name_cannot_become_an_ssh_option(monkeypatch):
    monkeypatch.setenv("BIB_VM_USER", "-oProxyCommand=touch /tmp/pwn")
    with pytest.raises(bib.Failure, match="not a usable account name"):
        bib.ssh_command(bib.VmConfig(), "192.168.1.50")


def test_follow_is_rejected_where_it_means_nothing(monkeypatch):
    monkeypatch.setattr(bib, "find_engine", lambda: "podman")
    assert bib.main(["box", "status", "-f"]) == 1


def test_create_runs_packer_init_first(calls, credentials, monkeypatch):
    monkeypatch.setenv("BIB_VM_PACKER", "1")  # this covers the fallback path
    # Without it, every first-time user hits "Did you run packer init".
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "find_packer", lambda: "packer")
    bib.cmd_vm_create("tart", bib.VmConfig())
    out = flat(calls)
    assert out.index("packer init") < out.index("packer build")


def test_the_template_is_resolved_next_to_the_module_not_the_cwd():
    # Otherwise `bib vm create` only works from a checkout, in the right directory.
    assert bib.PACKER_TEMPLATE.is_absolute()
    assert bib.PACKER_TEMPLATE.parent.parent == Path(bib.__file__).resolve().parent


def test_the_guest_shares_a_host_folder_for_downloads(tmp_path, monkeypatch):
    # Downloads should land on the host, not inside the VM's disk image.
    monkeypatch.setenv("BIB_VM_SHARE", str(tmp_path / "dl"))
    args = bib.vm_run_args(bib.VmConfig())
    assert f"--dir=downloads:{tmp_path / 'dl'}" in args
    assert (tmp_path / "dl").is_dir()  # created if missing, so tart does not fail


# sudo as sshd actually presents it: no tty, no cached credential. -S reads a
# password from stdin and works; -n refuses, exactly as the real one does. A fake
# that stripped -n could not notice a regression to the flag that never worked.
_FAKE_SUDO = """#!/bin/sh
got_stdin=""
while [ $# -gt 0 ]; do
  case "$1" in
    -S) got_stdin=1; shift ;;
    -n) echo "sudo: a password is required" >&2; exit 1 ;;
    -p) shift 2 ;;
    *) break ;;
  esac
done
[ -n "$got_stdin" ] || { echo "sudo: no tty present" >&2; exit 1; }
read -r _pw || { echo "sudo: no password on stdin" >&2; exit 1; }
exec "$@"
"""

# install that creates its destination, so `test -x` at the end of the script
# observes whether the agent was really installed.
_FAKE_INSTALL = """#!/bin/sh
# -d means "create this directory", not "write a file here". Without it the shim
# tried to write over a directory and reported "Is a directory", which reads like
# the script's fault rather than the fake's.
for a in "$@"; do case "$a" in -d) MKDIR=1 ;; esac; done
for dst in "$@"; do :; done
if [ -n "$MKDIR" ]; then mkdir -p "$dst"; exit 0; fi
# A destination outside this sandbox is a real system path — /etc/ssh/sshd_config.d
# and /usr/local/bin. The script is being run to prove it runs, not to be given the
# machine, so those are recorded as done and nothing is written.
case "$dst" in
  "$HOME"/*|/tmp/*|/private/tmp/*) ;;
  /*) exit 0 ;;
esac
mkdir -p "$(dirname "$dst")"
printf '#!/bin/sh\\nexit 0\\n' > "$dst"
chmod 0755 "$dst"
"""


def _run_guest_script(
    script: str, home, share_exists: bool, extra_bin=None, override_bin=None, browser=None
):
    """Execute the guest script the way ssh would: /bin/sh -e, with fakes."""
    bin_dir = home / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("curl", "hdiutil", "tar", "defaults", "pmset", "sysadminctl", "systemsetup"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / name).chmod(0o755)
    # cp creates its destination, so the staged move that follows is real: a cp
    # that only exits 0 would let a broken install sequence pass.
    (bin_dir / "cp").write_text(
        '#!/bin/sh\nfor a in "$@"; do prev="$last"; last="$a"; done\n'
        'case "$last" in */) mkdir -p "$last$(basename "$prev")" ;; '
        '*) mkdir -p "$last" ;; esac\n'
    )
    (bin_dir / "cp").chmod(0o755)
    if extra_bin:
        for tool in extra_bin.iterdir():
            (bin_dir / tool.name).write_text(tool.read_text())
            (bin_dir / tool.name).chmod(0o755)
    # macOS-only tools the guest script uses. The suite runs on Linux in CI, where
    # they simply do not exist and every one of them is an exit 127.
    for name, body_text in (("sudo", _FAKE_SUDO), ("install", _FAKE_INSTALL)):
        (bin_dir / name).write_text(body_text)
        (bin_dir / name).chmod(0o755)
    # Applied last, so a test can replace even sudo or install — which the two
    # loops above would otherwise write back over.
    for name, body_text in (override_bin or {}).items():
        (bin_dir / name).write_text(body_text)
        (bin_dir / name).chmod(0o755)
    share = Path(bib.GUEST_SHARE)
    body = script.replace(str(share), str(home / "share"))
    # Per browser, not hard-coded to Chrome: the point of running the script at all
    # is that it works for whichever one it was rendered for.
    chosen = browser or bibbrowsers.BROWSERS["chrome"]
    # Through shlex.quote, because that is what the script did: it only adds quotes
    # when the path needs them, so Chrome's is quoted and Firefox's is not.
    body = body.replace(shlex.quote(chosen.binary), "true")
    body = body.replace(shlex.quote(chosen.app), shlex.quote(str(home / "browser.app")))
    body = body.replace(chosen.app, str(home / "browser.app"))
    body = body.replace(bib.AGENT_BIN, str(home / "agent"))
    body = body.replace(bib.AGENT_PLIST_PATH, str(home / "agent.plist"))
    # launchctl needs a real session to talk to, which an ssh-less test does not
    # have; record the calls instead so they can be asserted.
    (bin_dir / "launchctl").write_text(
        f'#!/bin/sh\necho "$@" >> {home}/launchctl.log\n[ "$1" = print ] && exit 0\nexit 0\n'
    )
    (bin_dir / "launchctl").chmod(0o755)
    # No rewriting of scratch paths: the script keeps its own under $HOME/.cache,
    # and pytest gives every test a different HOME. Two suites can run at once.
    if share_exists:
        (home / "share").mkdir(exist_ok=True)
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-e", "-c", body],
        env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def test_the_guest_script_fails_when_the_agent_did_not_end_up_executable(tmp_path):
    """Without the agent there is no copy-paste, and the password is unguessable.

    Reporting success here is worse than failing: the user is left with a guest
    whose generated password can only be typed in by hand.
    """
    (tmp_path / "Downloads").mkdir()
    # install that leaves the file there but not executable — a plausible failure,
    # and the one thing `test -x` is there to catch.
    limp = '#!/bin/sh\nfor dst in "$@"; do :; done\nmkdir -p "$(dirname "$dst")"\n: > "$dst"\n'
    result = _run_guest_script(
        bib.guest_install_script("pw"),
        tmp_path,
        share_exists=True,
        override_bin={"install": limp},
    )
    assert result.returncode != 0


def test_ssh_never_offers_another_key_or_falls_back_to_a_password(tmp_path):
    """Each of these has to be present, and the reason differs per option.

    Without IdentitiesOnly ssh offers every key the agent holds before ours, so a
    guest that has forgotten our key prompts for a password instead of failing —
    and the script sent over that connection carries the guest's password.
    """
    options = bib.ssh_options()
    for expected in (
        "IdentitiesOnly=yes",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "NumberOfPasswordPrompts=0",
        "StrictHostKeyChecking=yes",
    ):
        assert expected in options, f"{expected} is no longer passed to ssh"
    assert str(bib.VM_KEY) in options
    assert f"UserKnownHostsFile={bib.KNOWN_HOSTS}" in options


def test_the_screen_address_carries_the_account_password(tmp_path, credentials):
    """Screen Sharing prompts otherwise, for a generated 24-character password.

    tart's --vnc is not a VNC server of its own — it opens macOS Screen Sharing at
    the guest — so the credential here is the guest account's, and bib is the only
    thing that knows it.
    """
    bib.CREDENTIALS.write_text("pa/ss word\n")
    url = bib.screen_url(bib.VmConfig(user="admin"), "10.0.0.5")
    # Percent-encoded, or a password with a slash or a space silently truncates the
    # address into something that points somewhere else.
    assert url == "vnc://admin:pa%2Fss%20word@10.0.0.5"


def test_the_screen_address_says_so_when_the_guest_is_not_sharing(tmp_path, monkeypatch):
    """macOS 26 only takes Screen Sharing from the guest's own System Settings.

    Nothing on the host can turn it on, so the useful thing to do is say that
    rather than hand back an address that connects and immediately drops.
    """
    monkeypatch.setattr(bib, "vm_running", lambda tart, vm: True)
    monkeypatch.setattr(bib, "vm_ip", lambda tart, vm: "10.0.0.5")
    monkeypatch.setattr(bib, "guest_answers", lambda ip, port=22: False)
    with pytest.raises(bib.Failure, match="System Settings"):
        bib.screen_address("tart", bib.VmConfig(viewer="vnc"))


def test_a_detached_start_leaves_the_callers_process_group(tmp_path, isolate_secrets):
    """Otherwise the guest dies with whatever started it.

    A child in the caller's process group takes the SIGHUP sent when that group
    goes away, so closing the terminal — or a wrapper script exiting, which is
    what the Dock icon does — killed the VM seconds after it appeared. A
    different process group is what "detached" has to mean.
    """
    faketart = tmp_path / "faketart"
    faketart.write_text("#!/bin/sh\nsleep 30\n")
    faketart.chmod(0o755)
    boot = isolate_secrets.start_detached(str(faketart), bib.VmConfig())
    try:
        assert os.getpgid(boot.pid) != os.getpgid(0)
    finally:
        boot.kill()
        boot.wait()


def test_a_detached_start_records_what_tart_printed(tmp_path, isolate_secrets):
    """The regression itself: stdout went to DEVNULL, so the URL was unrecoverable."""
    faketart = tmp_path / "faketart"
    faketart.write_text("#!/bin/sh\necho 'VNC server is running at vnc://a:b@10.0.0.5:5900'\n")
    faketart.chmod(0o755)
    boot = isolate_secrets.start_detached(str(faketart), bib.VmConfig(viewer="vnc"))
    boot.wait()
    assert "vnc://a:b@10.0.0.5:5900" in bib.BOOT_LOG.read_text()


def test_deleting_the_vm_takes_the_remembered_address_with_it(tmp_path, monkeypatch):
    """A list of names left vm-last-ip behind, and the next build inherited it."""
    bib.SECRETS.mkdir(parents=True, exist_ok=True)
    bib.write_state(last_ip="10.0.0.9")
    bib.CREDENTIALS.write_text("secret\n")
    monkeypatch.setattr("builtins.input", lambda _: "y")
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="", stderr="")
    )
    bib.cmd_vm_delete("tart", bib.VmConfig())
    assert not bib.STATE.exists()
    assert not bib.CREDENTIALS.exists()


def test_the_guest_script_fails_when_the_share_is_missing(tmp_path):
    # It used to warn, install Chrome, exit 0 — and bib then printed "Done".
    (tmp_path / "Downloads").mkdir()
    result = _run_guest_script(bib.guest_install_script("pw"), tmp_path, share_exists=False)
    assert result.returncode != 0
    assert "not mounted" in result.stderr


def test_a_share_path_with_a_colon_is_rejected(monkeypatch, tmp_path):
    # tart parses --dir as name:path:options, so a colon would silently mis-parse.
    monkeypatch.setenv("BIB_VM_SHARE", str(tmp_path / "a:b"))
    with pytest.raises(bib.Failure, match="colon"):
        bib.vm_run_args(bib.VmConfig())


def test_an_unusable_share_path_is_reported_not_raised(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("BIB_VM_SHARE", str(blocker / "share"))
    with pytest.raises(bib.Failure, match="cannot use"):
        bib.vm_run_args(bib.VmConfig())


def test_a_zero_resolution_is_rejected():
    with pytest.raises(bib.Failure, match="must be positive"):
        bib.Config(resolution="0x0").check()


def test_the_password_never_reaches_the_argument_list(calls, credentials, monkeypatch):
    # Generated, so the assertion cannot pass on the account name: the default
    # password is "admin" and so is the user.
    monkeypatch.setenv("BIB_VM_PASSWORD", bib.RANDOM_PASSWORD)
    monkeypatch.setenv("BIB_VM_PACKER", "1")  # this covers the fallback path
    # argv is world-readable while the build runs, and is printed on failure.
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "find_packer", lambda: "packer")
    bib.cmd_vm_create("tart", bib.VmConfig())
    assert credentials.read_text().strip() not in flat(calls)


def test_the_template_no_longer_carries_a_second_copy_of_the_install():
    # It kept `--install-daemon=launchd` for three rounds after the guest script's
    # copy was fixed, because there were two copies. Chrome and the agent are now
    # installed in one place, by 'bib vm setup', for both build paths.
    template = (Path(bib.__file__).resolve().parent / "packer" / "browser-vm.pkr.hcl").read_text()
    assert "--install-daemon" not in template
    assert "googlechrome.dmg" not in template
    assert "tart-guest-agent" not in template


def test_the_template_installs_the_key_bib_will_connect_with():
    template = (Path(bib.__file__).resolve().parent / "packer" / "browser-vm.pkr.hcl").read_text()
    assert "authorized_keys" in template
    assert "ssh_host_ed25519_key" in template
    for name in ("authorized_key", "host_private_key", "host_public_key"):
        assert f'variable "{name}"' in template, f"{name} is used but never declared"


def test_the_packer_path_generates_and_passes_the_keys(calls, credentials, monkeypatch):
    # Without them the `bib vm setup` the build recommends could never connect.
    monkeypatch.setenv("BIB_VM_PACKER", "1")
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "find_packer", lambda: "/usr/bin/packer")
    seen = {}
    monkeypatch.setattr(
        bib,
        "run",
        lambda engine, *a, **k: (
            seen.update(env=k.get("env") or seen.get("env"))
            or subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ),
    )
    bib.cmd_vm_create("tart", bib.VmConfig())
    env = seen["env"]
    assert env["PKR_VAR_authorized_key"].startswith("ssh-ed25519 ")
    assert env["PKR_VAR_host_private_key"]
    assert env["PKR_VAR_host_public_key"].startswith("ssh-ed25519 ")
    assert bib.KNOWN_HOSTS.exists(), "the host key has to be pinned for bib to use it"


def test_an_already_running_vm_is_not_a_networking_failure(calls, monkeypatch):
    # tart exits non-zero for a VM that is already up; blaming the bridge for that
    # sent the user off to degrade their DNS for no reason.
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: True)
    monkeypatch.setattr(bib, "vm_running", lambda *a, **k: True)
    bib.cmd_vm_up("tart", bib.VmConfig())
    assert "run" not in flat(calls)


def test_down_says_nothing_was_running_when_there_was_nothing(monkeypatch, capsys):
    # podman's `rm -f` exits 0 for a container that never existed.
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    bib.cmd_down("podman", bib.Config())
    assert "Not running." in capsys.readouterr().out


def test_the_resolution_is_normalised_before_it_reaches_xrandr(calls, monkeypatch):
    # xrandr reads anything but lowercase <int>x<int> as a mode index.
    monkeypatch.setenv("BIB_RESOLUTION", "1280 X 800")
    bib.ensure_desktop("podman", bib.Config())
    assert "RES=1280x800" in flat(calls)


def test_a_non_zero_remote_shell_is_not_a_connection_failure(monkeypatch):
    # ssh passes the remote shell's exit status through; only 255 is ssh's own.
    monkeypatch.setattr(bib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(bib, "guest_ssh", lambda *a, **k: 1)
    bib.cmd_vm_ssh("tart", bib.VmConfig())  # must not raise
    monkeypatch.setattr(bib, "guest_ssh", lambda *a, **k: 255)
    with pytest.raises(bib.Failure, match="Remote Login"):
        bib.cmd_vm_ssh("tart", bib.VmConfig())


def test_error_messages_name_commands_that_exist(monkeypatch):
    # The CLI is variant-scoped now, so "bib logs" would be rejected by argparse.
    source = Path(bib.__file__).read_text()
    for stale in ("'bib logs'", "'bib up'", "'bib down'", "'bib status'"):
        assert stale not in source, stale


def test_the_release_archive_has_a_top_level_directory():
    # Otherwise `tar -xzf` scatters ~44 files into the current directory.
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text()
    assert "tar -czf dist/bib-macos-arm64.tar.gz -C dist bib-macos-arm64" in workflow
    assert '-C dist "bib-linux-${{ matrix.arch }}"' in workflow


# --- the offline guest patcher ------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_the_password_verifier_matches_what_macos_expects():
    import plistlib

    blob = plistlib.loads(bibpatch.shadow_hash_data("hunter2"))
    entry = blob["SALTED-SHA512-PBKDF2"]
    # Wrong parameters do not fail loudly; the account just refuses every password.
    assert entry["iterations"] == 50_000
    assert len(entry["salt"]) == 32
    assert len(entry["entropy"]) == 128
    # The verifier has to reach the record, under the name macOS reads: dropping it
    # leaves an account that refuses every password, and no other test noticed.
    record = bibpatch.user_record(bibpatch.Account("admin", "hunter2"), "GUID")
    assert list(record) == [
        "name",
        "realname",
        "uid",
        "gid",
        "home",
        "shell",
        "generateduid",
        "authentication_authority",
        "passwd",
        "ShadowHashData",
        "_writers_passwd",
    ]
    assert plistlib.loads(record["ShadowHashData"][0])["SALTED-SHA512-PBKDF2"]
    assert record["authentication_authority"] == [";ShadowHash;HASHLIST:<SALTED-SHA512-PBKDF2>"]
    assert record["shell"] == ["/bin/zsh"], "a wrong shell breaks every ssh takeover"
    assert record["home"] == ["/Users/admin"], "own_home would chown a different tree"
    # Every value is a list: DirectoryService silently ignores a plain string.
    assert all(isinstance(v, list) for v in record.values())


def test_the_verifier_is_salted_differently_every_time():
    import plistlib

    salts = {
        plistlib.loads(bibpatch.shadow_hash_data("same"))["SALTED-SHA512-PBKDF2"]["salt"]
        for _ in range(5)
    }
    assert len(salts) == 5


def test_kcpassword_is_padded_to_the_key_length():
    # Without the padding macOS reads past the end of the password.
    for password in ("a", "elevenchars", "exactly-eleven"):
        assert len(bibpatch.kcpassword(password)) % len(bibpatch.KCPASSWORD_KEY) == 0


def test_kcpassword_round_trips():
    key = bibpatch.KCPASSWORD_KEY
    encoded = bibpatch.kcpassword("s3cret")
    decoded = bytes(b ^ key[i % len(key)] for i, b in enumerate(encoded))
    assert decoded.startswith(b"s3cret\x00")


def test_the_user_record_stores_every_value_as_a_list():
    # DirectoryService silently ignores a plain string here.
    record = bibpatch.user_record(bibpatch.Account("admin", "pw"))
    assert all(isinstance(v, list) for v in record.values())
    assert record["name"] == ["admin"]
    assert record["uid"] == ["501"]


def test_patching_a_directory_that_is_not_a_guest_volume_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    with pytest.raises(bibpatch.PatchError, match="Data volume"):
        bibpatch.patch(tmp_path, bibpatch.Account("admin", "pw"))


def test_patching_without_a_first_boot_state_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    (tmp_path / "private/var/db").mkdir(parents=True)
    with pytest.raises(bibpatch.PatchError, match="booted once"):
        bibpatch.patch(tmp_path, bibpatch.Account("admin", "pw"))


class _FakeBoot:
    """Stands in for the `tart run` child: alive until stopped, like a real boot."""

    returncode = 0
    stderr = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def kill(self):
        return None


def _fake_guest_disk(monkeypatch, tmp_path):
    """A stand-in for ~/.tart/vms/<name>/disk.img, so nothing patches Path.exists
    globally — that would also make the credentials file look present."""
    disk = tmp_path / ".tart" / "vms" / "browser-vm" / "disk.img"
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.touch()
    monkeypatch.setattr(bib.Path, "home", classmethod(lambda cls: tmp_path))
    return disk


def test_create_prepares_the_guest_offline_by_default(calls, credentials, monkeypatch, tmp_path):
    # Typing into Setup Assistant is the fallback now, not the default.
    seen = {}
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bib.subprocess,
        "Popen",
        lambda *a, **k: _FakeBoot(),
    )
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        # Only the patcher: create now goes on to boot the guest and ssh into it, so
        # "the last call" is no longer the one under test.
        lambda cmd, **kw: (
            (
                seen.update(cmd=cmd, stdin=kw.get("input"))
                if any("bibpatch" in str(c) for c in cmd)
                else None
            )
            or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    bib.cmd_vm_create("tart", bib.VmConfig())
    out = flat(calls)
    assert "create --from-ipsw=latest --disk-size=100 browser-vm" in out
    assert "packer" not in out
    # Only the patch runs as root, and the password goes in on stdin so it never
    # appears in the process list.
    assert seen["cmd"][0] == "/usr/bin/sudo"
    assert "bibpatch.py" in " ".join(seen["cmd"])
    assert not any("password" in str(a) for a in seen["cmd"])
    assert seen["stdin"].strip() == credentials.read_text().strip()


def test_the_packer_path_is_still_reachable(calls, credentials, monkeypatch):
    monkeypatch.setenv("BIB_VM_PACKER", "1")
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "find_packer", lambda: "packer")
    bib.cmd_vm_create("tart", bib.VmConfig())
    assert "packer build" in flat(calls)


def test_a_failed_patch_names_the_fallback(calls, credentials, monkeypatch, tmp_path):
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bib.subprocess,
        "Popen",
        lambda *a, **k: _FakeBoot(),
    )

    # The sudo probe must succeed so we reach the patch itself.
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1 if any("bibpatch" in str(c) for c in cmd) else 0
        ),
    )
    with pytest.raises(bib.Failure, match="BIB_VM_PACKER=1"):
        bib.cmd_vm_create("tart", bib.VmConfig())


def _apfs_listing(store: str, volumes: list[dict]) -> bytes:
    import plistlib

    return plistlib.dumps(
        {"Containers": [{"PhysicalStores": [{"DeviceIdentifier": store}], "Volumes": volumes}]}
    )


def test_the_data_volume_is_chosen_by_role_not_by_name(monkeypatch):
    # The System volume is sealed; writing to it silently achieves nothing.
    listing = _apfs_listing(
        "disk5s2",
        [
            {"DeviceIdentifier": "disk5s1", "Name": "Macintosh HD", "Roles": ["System"]},
            {"DeviceIdentifier": "disk5s5", "Name": "Macintosh HD - Data", "Roles": ["Data"]},
        ],
    )
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    assert bibpatch.data_volume("/dev/disk5") == "/dev/disk5s5"


def test_a_disk_without_a_data_volume_is_reported(monkeypatch):
    import plistlib

    empty = plistlib.dumps({"Containers": []})
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=empty, stderr=b""),
    )
    with pytest.raises(bibpatch.PatchError, match="physical store"):
        bibpatch.data_volume("/dev/disk9")


def test_group_membership_records_the_users_guid_not_the_groups(tmp_path):
    # Writing the group's own GUID into groupmembers leaves the account a member by
    # name only, and macOS believes whichever list it consults first.
    import plistlib

    root = tmp_path
    groups = root / "private/var/db/dslocal/nodes/Default/groups"
    groups.mkdir(parents=True)
    for name in ("admin", "staff"):
        with (groups / f"{name}.plist").open("wb") as fh:
            plistlib.dump(
                {
                    "users": ["root"],
                    "groupmembers": ["GROUP-OWN-GUID"],
                    "generateduid": ["GROUP-OWN-GUID"],
                },
                fh,
                fmt=plistlib.FMT_BINARY,
            )
    account = bibpatch.Account("admin", "pw")
    bibpatch.add_to_group(root, "admin", account, "USER-GUID-1234")
    with (groups / "admin.plist").open("rb") as fh:
        record = plistlib.load(fh)
    assert "USER-GUID-1234" in record["groupmembers"]
    assert "admin" in record["users"]
    assert "root" in record["users"]  # existing members are kept


def test_the_account_and_its_group_entries_share_one_guid(tmp_path, monkeypatch):
    import plistlib

    root = tmp_path
    for sub in ("users", "groups"):
        (root / f"private/var/db/dslocal/nodes/Default/{sub}").mkdir(parents=True)
    groups = root / "private/var/db/dslocal/nodes/Default/groups"
    for name in ("admin", "staff"):
        with (groups / f"{name}.plist").open("wb") as fh:
            plistlib.dump({"users": [], "groupmembers": []}, fh, fmt=plistlib.FMT_BINARY)
    monkeypatch.setattr(bibpatch.os, "chown", lambda *a: None)
    account = bibpatch.Account("admin", "pw")
    bibpatch.create_account(root, account)
    with (root / "private/var/db/dslocal/nodes/Default/users/admin.plist").open("rb") as fh:
        guid = plistlib.load(fh)["generateduid"][0]
    with (groups / "admin.plist").open("rb") as fh:
        assert guid in plistlib.load(fh)["groupmembers"]


def test_the_container_is_found_through_its_physical_store(monkeypatch):
    # Attaching an image synthesises the APFS container onto a *different* disk
    # number, so listing the attached device alone finds nothing. This is what the
    # first real run failed on.
    listing = _apfs_listing(
        "disk10s2",
        [{"DeviceIdentifier": "disk11s2", "Name": "Data", "Roles": ["Data"]}],
    )
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    assert bibpatch.data_volume("/dev/disk10") == "/dev/disk11s2"


def test_patching_without_root_says_so_instead_of_a_traceback(tmp_path, monkeypatch):
    # Writing dslocal and setting root ownership needs root; PermissionError is an
    # OSError, so it would have escaped the PatchError handler entirely.
    (tmp_path / "private/var/db").mkdir(parents=True)
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 501)
    with pytest.raises(bibpatch.PatchError, match="needs root"):
        bibpatch.patch(tmp_path, bibpatch.Account("admin", "pw"))


def test_the_home_tree_is_owned_after_every_file_exists(tmp_path, monkeypatch):
    # suppress_setup_assistant creates ~/Library/Preferences. Chowning the home
    # before that leaves those directories root-owned, and cfprefsd then silently
    # fails to write any preference the guest sets.
    chowned: list[str] = []
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: chowned.append(str(p)))
    root = tmp_path
    for sub in ("users", "groups"):
        (root / f"private/var/db/dslocal/nodes/Default/{sub}").mkdir(parents=True)
    import plistlib

    for name in ("admin", "staff"):
        path = root / f"private/var/db/dslocal/nodes/Default/groups/{name}.plist"
        with path.open("wb") as fh:
            plistlib.dump({"users": [], "groupmembers": []}, fh, fmt=plistlib.FMT_BINARY)
    bibpatch.patch(root, bibpatch.Account("admin", "pw"))
    prefs = root / "Users/admin/Library/Preferences"
    assert prefs.is_dir()
    assert str(prefs) in chowned, "the per-user preferences directory was never owned"


def test_the_data_volume_is_matched_on_its_real_name(monkeypatch):
    # The APFS volume is called "Data", not "Macintosh HD - Data" — that is a Finder
    # display name.
    listing = _apfs_listing("disk9s2", [{"DeviceIdentifier": "disk10s2", "Name": "Data"}])
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    assert bibpatch.data_volume("/dev/disk9") == "/dev/disk10s2"


def test_the_patcher_is_run_with_a_real_interpreter(calls, credentials, monkeypatch, tmp_path):
    # Under Nuitka sys.executable is the compiled binary, which cannot run a script.
    monkeypatch.setenv("BIB_VM_PACKER", "0")
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bib.subprocess,
        "Popen",
        lambda *a, **k: _FakeBoot(),
    )
    monkeypatch.setattr(bib.sys, "executable", "/opt/homebrew/bin/bib")  # a binary
    monkeypatch.setattr(bib.shutil, "which", lambda n: "/usr/bin/python3")
    seen = {}
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        # Only the patcher: create now goes on to boot the guest and ssh into it, so
        # "the last call" is no longer the one under test.
        lambda cmd, **kw: (
            (seen.update(cmd=cmd) if any("bibpatch" in str(c) for c in cmd) else None)
            or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    bib.cmd_vm_create("tart", bib.VmConfig())
    assert seen["cmd"][:2] == ["/usr/bin/sudo", "-n"]
    assert seen["cmd"][2] == "/usr/bin/python3"
    assert not seen["cmd"][2].endswith("/bib")


def test_a_missing_sudo_credential_is_named_before_anything_is_tried(
    credentials, monkeypatch, tmp_path
):
    # sudo prompts on its own tty and cannot ask for anything when bib runs
    # detached; that used to surface as a bare "preparing the guest failed".
    _fake_guest_disk(monkeypatch, tmp_path)
    # Stubbed, so the failure asserted on is the sudo check rather than the
    # interpreter probe, which runs first and would fail on the same fake.
    monkeypatch.setattr(bib, "find_guest_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(
        bib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )
    with pytest.raises(bib.Failure, match="sudo -v"):
        bib._prepare_guest(bib.VmConfig(), "pw")


def test_prepare_can_be_retried_without_rebuilding(calls, credentials, monkeypatch, tmp_path):
    # Building takes half an hour; a failed patch must not cost that again.
    bib.guest_password(create=True)  # as a real build would have left it
    _fake_guest_disk(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: True)
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: (
            (seen.update(cmd=cmd) if any("bibpatch" in str(c) for c in cmd) else None)
            or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    bib.cmd_vm_prepare("tart", bib.VmConfig())
    assert "bibpatch.py" in " ".join(seen["cmd"])
    assert "create" not in flat(calls)


def test_a_first_boot_that_never_happened_is_not_called_built(
    calls, credentials, monkeypatch, tmp_path
):
    # Otherwise bib patches a guest that never booted and prints "Built."
    import io

    class Died(_FakeBoot):
        returncode = 2
        stderr = io.StringIO("VM is already running!")

        def poll(self):
            return 2

    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "sudo_is_cached", lambda: True)
    monkeypatch.setattr(bib, "find_guest_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    monkeypatch.setattr(bib.subprocess, "Popen", lambda *a, **k: Died())
    _fake_guest_disk(monkeypatch, tmp_path)
    with pytest.raises(bib.Failure, match="exited immediately"):
        bib.cmd_vm_create("tart", bib.VmConfig())


@pytest.mark.parametrize("name", ["../../etc/pam.d/x", "-oProxyCommand=x", "My User", "/abs"])
def test_a_dangerous_account_name_is_refused_before_root_runs(name, monkeypatch):
    # This value reaches a root-privileged patcher that builds paths from it.
    monkeypatch.setenv("BIB_VM_USER", name)
    with pytest.raises(bib.Failure, match="not a usable account name"):
        bib.validate_vm_user(bib.VmConfig().user)


def test_the_patcher_refuses_a_dangerous_name_itself(tmp_path, monkeypatch):
    # The privileged half does not trust its caller either.
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    with pytest.raises(bibpatch.PatchError, match="refusing to use"):
        bibpatch.patch(tmp_path, bibpatch.Account("../../etc/x", "pw"))


def test_the_disk_is_looked_for_where_tart_actually_puts_it(credentials, monkeypatch, tmp_path):
    # tart honours TART_HOME; looking under ~/.tart would miss the disk entirely.
    monkeypatch.setenv("TART_HOME", str(tmp_path / "elsewhere"))
    # Stubbed, so the failure this asserts on is the sudo check and not the
    # interpreter probe, which runs first and uses the same faked subprocess.
    monkeypatch.setattr(bib, "find_guest_python", lambda: "/usr/bin/python3")
    disk = tmp_path / "elsewhere" / "vms" / "browser-vm" / "disk.img"
    disk.parent.mkdir(parents=True)
    disk.touch()
    monkeypatch.setattr(
        bib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )
    with pytest.raises(bib.Failure, match="sudo"):  # got past the disk lookup
        bib._prepare_guest(bib.VmConfig(), "pw")


def test_mount_failures_report_the_reason(monkeypatch):
    # diskutil writes its diagnosis to stderr; reporting stdout gave a bare colon.
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="Failed to find disk"),
    )
    with pytest.raises(bibpatch.PatchError, match="Failed to find disk"):
        bibpatch.mount("/dev/disk99s9")


def test_setup_installs_the_clipboard_agent_too():
    # It used to be installed only by the packer path, while bib told the user that
    # `vm setup` had done it.
    assert "tart-guest-agent" in bib.guest_install_script("pw")
    assert bib.AGENT_PLIST_PATH in bib.guest_install_script("pw")
    assert bib.GUEST_AGENT_VERSION in bib.guest_install_script("pw")


def test_the_image_volume_is_attached_with_ownership(monkeypatch):
    # macOS mounts an image volume noowners, where chown returns success and writes
    # nothing — so every file the patcher creates would stay root-owned.
    #
    # Asserted on the argv hdiutil is actually handed. Grepping the source for the
    # flag name passed even when the value beside it was wrong.
    seen = {}
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda cmd, **kw: (
            seen.update(cmd=cmd)
            or subprocess.CompletedProcess(cmd, 0, stdout="/dev/disk9\n", stderr="")
        ),
    )
    assert bibpatch.attach(Path("/x/disk.img")) == "/dev/disk9"
    cmd = seen["cmd"]
    assert cmd[cmd.index("-owners") + 1] == "on"
    assert "-nomount" in cmd


def test_owning_the_home_never_follows_a_link(tmp_path, monkeypatch):
    # A symlink stored in the guest resolves against the HOST filesystem, so
    # following one here would let the guest steer a root chown at any host path.
    calls_seen: list[tuple] = []
    monkeypatch.setattr(
        bibpatch.os,
        "chown",
        lambda p, u, g, **kw: calls_seen.append((str(p), kw.get("follow_symlinks"))),
    )
    home = tmp_path / "Users/admin"
    home.mkdir(parents=True)
    (home / "real").write_text("x")
    (home / "escape").symlink_to("/etc")
    bibpatch.own_home(tmp_path, bibpatch.Account("admin", "pw"))
    assert calls_seen, "nothing was owned"
    assert all(kw is False for _, kw in calls_seen), "a chown followed links"
    assert not any(path == "/etc" for path, _ in calls_seen)


def test_a_symlinked_home_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(bibpatch.os, "chown", lambda *a, **k: None)
    (tmp_path / "Users").mkdir()
    (tmp_path / "Users/admin").symlink_to("/")
    with pytest.raises(bibpatch.PatchError, match="symlink"):
        bibpatch.own_home(tmp_path, bibpatch.Account("admin", "pw"))


def test_the_disk_is_sized_when_it_is_created(calls, credentials, monkeypatch, tmp_path):
    # tart create installs macOS onto its default 50 GB disk; growing the image
    # afterwards leaves the partitions where the installer put them.
    monkeypatch.setenv("BIB_VM_DISK", "120")
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    monkeypatch.setattr(bib.subprocess, "Popen", lambda *a, **k: _FakeBoot())
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(
        bib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0)
    )
    bib.cmd_vm_create("tart", bib.VmConfig())
    out = flat(calls)
    assert "--disk-size=120" in out
    assert "set browser-vm --cpu" in out and "--disk-size" not in out.split("set browser-vm")[1]


def test_prepare_refuses_a_running_guest(calls, credentials, monkeypatch):
    # Patching a mounted disk that the guest is also writing means two writers.
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: True)
    monkeypatch.setattr(bib, "vm_running", lambda *a, **k: True)
    with pytest.raises(bib.Failure, match="bib vm down"):
        bib.cmd_vm_prepare("tart", bib.VmConfig())


# --- the guest's keyboard, which the offline path used never to set ------------


def _hitoolbox(sources: list[dict]) -> str:
    import plistlib

    return plistlib.dumps({"AppleSelectedInputSources": sources}, fmt=plistlib.FMT_XML).decode()


_SWISS = {
    "InputSourceKind": "Keyboard Layout",
    "KeyboardLayout ID": 19,
    "KeyboardLayout Name": "Swiss German",
}


def test_the_guest_gets_the_hosts_keyboard_layout(monkeypatch):
    # The generated password is pasted, but everything typed in the guest afterwards
    # is typed by hand, and a guest left on U.S. moves the punctuation.
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=_hitoolbox([_SWISS])),
    )
    assert bib.host_keyboard_layout() == (19, "Swiss German")


def test_an_input_method_is_not_mistaken_for_a_keyboard_layout(monkeypatch):
    # Press-And-Hold sits in the same list and carries no layout at all.
    sources = [
        {"InputSourceKind": "Non Keyboard Input Method", "Bundle ID": "com.apple.PressAndHold"},
        _SWISS,
    ]
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=_hitoolbox(sources)),
    )
    assert bib.host_keyboard_layout() == (19, "Swiss German")


@pytest.mark.parametrize(
    "returncode,stdout",
    [
        (1, ""),  # no such preference domain
        (0, "not a plist"),  # something else answered
        (0, None),  # nothing captured at all
        (0, _hitoolbox([{"InputSourceKind": "Keyboard Layout"}])),  # a layout with no id
    ],
)
def test_an_unreadable_keyboard_preference_falls_back_to_us(monkeypatch, returncode, stdout):
    # Guessing a layout would be worse than the one macOS installs with.
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode, stdout=stdout),
    )
    assert bib.host_keyboard_layout() == bib.DEFAULT_KEYBOARD


def test_the_layout_is_written_where_the_guest_reads_it(tmp_path):
    # Selecting a layout that is not also enabled leaves the guest on U.S.
    import plistlib

    bibpatch.set_keyboard_layout(
        tmp_path, bibpatch.Account("admin", "pw"), bibpatch.Keyboard(19, "Swiss German")
    )
    path = tmp_path / "Users/admin/Library/Preferences/com.apple.HIToolbox.plist"
    record = plistlib.loads(path.read_bytes())
    assert record["AppleEnabledInputSources"] == [_SWISS]
    assert record["AppleSelectedInputSources"] == [_SWISS]


def test_the_hosts_layout_reaches_the_patcher(credentials, monkeypatch, tmp_path):
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(bib, "host_keyboard_layout", lambda: (19, "Swiss German"))
    seen = {}
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: (
            (seen.update(cmd=cmd) if any("bibpatch" in str(c) for c in cmd) else None)
            or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    bib._prepare_guest(bib.VmConfig(), "pw")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--keyboard-id") + 1] == "19"
    assert cmd[cmd.index("--keyboard-name") + 1] == "Swiss German"


def test_patching_leaves_the_guest_on_the_layout_it_was_told(tmp_path, monkeypatch):
    import plistlib

    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    for sub in ("users", "groups"):
        (tmp_path / f"private/var/db/dslocal/nodes/Default/{sub}").mkdir(parents=True)
    for name in ("admin", "staff"):
        path = tmp_path / f"private/var/db/dslocal/nodes/Default/groups/{name}.plist"
        with path.open("wb") as fh:
            plistlib.dump({"users": [], "groupmembers": []}, fh, fmt=plistlib.FMT_BINARY)
    bibpatch.patch(tmp_path, bibpatch.Account("admin", "pw"), bibpatch.Keyboard(19, "Swiss German"))
    record = plistlib.loads(
        (tmp_path / "Users/admin/Library/Preferences/com.apple.HIToolbox.plist").read_bytes()
    )
    assert record["AppleSelectedInputSources"] == [_SWISS]


# --- pins, managers, and messages that pointed the wrong way -------------------


def test_the_guest_agent_is_pinned_in_exactly_one_place():
    # It used to be pinned twice, in bib.py and in the packer template, with a test
    # asserting the two agreed. The template no longer installs the agent at all,
    # so the twin — and the way it could drift — is gone.
    root = Path(bib.__file__).resolve().parent
    template = (root / "packer" / "browser-vm.pkr.hcl").read_text()
    assert "guest_agent_version" not in template
    assert re.search(r'^GUEST_AGENT_VERSION = "[\d.]+"$', (root / "bib.py").read_text(), re.M)


def test_every_renovate_marker_anywhere_has_a_manager_that_matches_it():
    # Generalised from bib.py alone: the packer template carries markers too, and a
    # marker with no manager reads as configured while updating nothing.
    import json

    root = Path(bib.__file__).resolve().parent
    config = json.loads((root / "renovate.json").read_text())
    by_file: dict[str, list[str]] = {}
    for manager in config["customManagers"]:
        target = manager["managerFilePatterns"][0].strip("/^$").replace("\\", "")
        by_file.setdefault(target, []).extend(manager["matchStrings"])
    # Every tracked file, not a list of two. "Anywhere" excluded bibbrowsers.py,
    # which is where the per-browser split moved the three kasmweb image pins — so
    # the one file whose markers were managed by nothing was the one not looked at,
    # and this test passed while the box's images were frozen for good.
    marked = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not path.is_file() or any(part.startswith(".") for part in relative.parts):
            continue
        if "__pycache__" in relative.parts or relative.parts[0] in ("dist", "build"):
            continue
        try:
            if "# renovate:" in path.read_text():
                marked.add(str(relative))
        except (OSError, UnicodeDecodeError):
            continue
    assert marked, "no renovate markers found at all — has the layout changed?"
    assert "bibbrowsers.py" in marked, "the browser table's image pins carry markers"
    for name in marked:
        source = (root / name).read_text()
        covered = {
            match.group(0).splitlines()[0].strip()
            for pattern in by_file.get(name, [])
            for match in re.finditer(re.sub(r"\(\?<(?![=!])", "(?P<", pattern), source)
        }
        markers = {ln.strip() for ln in re.findall(r"^\s*# renovate:.*$", source, flags=re.M)}
        assert markers == covered, f"{name}: {markers - covered} matched by no manager"


def test_every_renovate_marker_in_bib_has_a_manager_that_matches_it():
    # A marker with no manager reads as configured and updates nothing: the
    # tart-guest-agent pin sat unmanaged behind one for seven review rounds.
    import json

    root = Path(bib.__file__).resolve().parent
    config = json.loads((root / "renovate.json").read_text())
    source = (root / "bib.py").read_text()
    patterns = [
        pattern
        for manager in config["customManagers"]
        if manager["managerFilePatterns"] == ["/^bib\\.py$/"]
        for pattern in manager["matchStrings"]
    ]
    # Renovate's regexes are JavaScript, where a named group is (?<name>...);
    # Python spells the same thing (?P<name>...). Lookbehind is left alone.
    covered = {
        match.group(0).splitlines()[0]
        for pattern in patterns
        for match in re.finditer(re.sub(r"\(\?<(?![=!])", "(?P<", pattern), source)
    }
    assert set(re.findall(r"^# renovate:.*$", source, flags=re.M)) == covered


def test_a_half_built_vm_is_pointed_at_prepare_not_only_at_up(capsys, monkeypatch):
    # 'vm up' on a VM whose preparation failed lands on Setup Assistant, which is
    # the one thing the offline path exists to avoid.
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: True)
    bib.cmd_vm_create("tart", bib.VmConfig())
    assert "bib vm prepare" in capsys.readouterr().out


def test_a_guest_that_will_not_stop_is_pointed_at_prepare(calls, credentials, monkeypatch):
    # It has already been killed here, so telling the user to stop it is advice
    # they cannot act on.
    class Stuck(_FakeBoot):
        def wait(self, timeout=None):
            if timeout:
                raise subprocess.TimeoutExpired("tart", timeout)
            return 0

    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "sudo_is_cached", lambda: True)
    monkeypatch.setattr(bib, "find_guest_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    monkeypatch.setattr(bib.subprocess, "Popen", lambda *a, **k: Stuck())
    with pytest.raises(bib.Failure, match="bib vm prepare"):
        bib.cmd_vm_create("tart", bib.VmConfig())


def test_the_packer_fallback_is_offered_with_the_delete_that_makes_it_work(monkeypatch, tmp_path):
    # 'vm create' on an existing VM only reports that it exists, so suggesting the
    # fallback without the delete suggests a no-op.
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(bib.Path, "exists", lambda self: "bibpatch" not in str(self))
    with pytest.raises(bib.Failure, match="bib vm delete"):
        bib._prepare_guest(bib.VmConfig(), "pw")


# --- the disk layer, where every real failure so far has happened --------------


def test_the_disk_is_put_back_even_when_patching_fails(monkeypatch):
    # A half-finished run must never leave the guest's disk attached to the host.
    undone: list[tuple[str, str]] = []
    monkeypatch.setattr(bibpatch, "attach", lambda disk: "/dev/disk9")
    monkeypatch.setattr(bibpatch, "data_volume", lambda device: "/dev/disk9s1")
    monkeypatch.setattr(bibpatch, "mount", lambda volume: Path("/Volumes/Data"))
    monkeypatch.setattr(bibpatch, "unmount", lambda volume: undone.append(("unmount", volume)))
    monkeypatch.setattr(bibpatch, "detach", lambda device: undone.append(("detach", device)))
    monkeypatch.setattr(
        bibpatch, "patch", lambda *a, **k: (_ for _ in ()).throw(bibpatch.PatchError("no"))
    )
    with pytest.raises(bibpatch.PatchError):
        bibpatch.prepare(Path("/x/disk.img"), bibpatch.Account("admin", "pw"))
    assert undone == [("unmount", "/dev/disk9s1"), ("detach", "/dev/disk9")]


def test_a_disk_that_will_not_attach_is_reported_with_its_reason(monkeypatch):
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="no such file"),
    )
    with pytest.raises(bibpatch.PatchError, match="no such file"):
        bibpatch.attach(Path("/x/disk.img"))


def test_a_volume_mounted_without_ownership_is_refused(monkeypatch):
    # chown returns success and writes nothing on a noowners mount, so the guest's
    # home would stay root-owned and it could not write its own preferences.
    import plistlib

    mounted = "/dev/disk9s1 on /Volumes/Data (apfs, noowners)\n"
    info = plistlib.dumps({"MountPoint": "/Volumes/Data"})

    def fake_run(cmd, *a, **k):
        if cmd[0] == "/sbin/mount":
            return subprocess.CompletedProcess(cmd, 0, stdout=mounted)
        if "info" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=info)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(bibpatch.subprocess, "run", fake_run)
    with pytest.raises(bibpatch.PatchError, match="without ownership"):
        bibpatch.mount("/dev/disk9s1")


def test_the_patcher_passes_the_layout_it_was_given(monkeypatch):
    import io

    seen = {}
    typed = "on-stdin-not-argv"
    monkeypatch.setattr(
        bibpatch, "prepare", lambda d, a, k, ks: seen.update(disk=d, account=a, kb=k, keys=ks)
    )
    monkeypatch.setattr(bibpatch.sys, "stdin", io.StringIO(typed + "\n"))
    bibpatch.main(
        [
            "--disk",
            "/x/disk.img",
            "--user",
            "admin",
            "--keyboard-id",
            "19",
            "--keyboard-name",
            "Swiss German",
        ]
    )
    assert seen["kb"] == bibpatch.Keyboard(19, "Swiss German")
    # Never in argv, so it is never in the process list.
    assert seen["account"].password == typed


def test_the_patcher_defaults_to_us_when_it_is_told_no_layout(monkeypatch):
    import io

    seen = {}
    monkeypatch.setattr(bibpatch, "prepare", lambda d, a, k, ks: seen.update(kb=k))
    monkeypatch.setattr(bibpatch.sys, "stdin", io.StringIO("pw\n"))
    bibpatch.main(["--disk", "/x/disk.img", "--user", "admin"])
    assert seen["kb"] == bibpatch.Keyboard()


def test_the_patcher_refuses_to_run_without_a_password(monkeypatch):
    # An empty password would produce an account nothing can log in to.
    import io

    monkeypatch.setattr(bibpatch.sys, "stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit, match="no password"):
        bibpatch.main(["--disk", "/x/disk.img", "--user", "admin"])


# --- the guest cannot redirect a root write onto this host ---------------------


def _guest_volume(tmp_path):
    """A stand-in for a mounted guest Data volume, with the groups patch() needs."""
    import plistlib

    root = tmp_path / "volume"
    (root / "private/var/db/dslocal/nodes/Default/users").mkdir(parents=True)
    groups = root / "private/var/db/dslocal/nodes/Default/groups"
    groups.mkdir(parents=True)
    for name in ("admin", "staff"):
        with (groups / f"{name}.plist").open("wb") as fh:
            plistlib.dump({"users": [], "groupmembers": []}, fh, fmt=plistlib.FMT_BINARY)
    return root


@pytest.mark.parametrize(
    "planted",
    [
        "private/var/db",  # .AppleSetupDone, dslocal, launchd all hang off this
        "private/etc",  # kcpassword
        "Library/Preferences",  # loginwindow and SetupAssistant
        "Library",  # the User Template marker
        "Users/admin/Library/Preferences",  # the per-user plists
        "Users/admin",  # the home itself
    ],
)
def test_a_symlink_planted_in_the_guest_never_redirects_a_root_write(tmp_path, planted):
    # This runs as root on the host and the guest volume is a host directory, so a
    # link stored in the guest resolves against the host. Round 7 guarded the home;
    # four earlier steps still wrote through whatever link they were handed.
    root = _guest_volume(tmp_path)
    target = tmp_path / "host"
    target.mkdir()
    witness = target / "precious"
    witness.write_text("host file")
    link = root / planted
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.exists():
        import shutil as _shutil

        _shutil.rmtree(link)
    link.symlink_to(target)

    with pytest.raises(bibpatch.PatchError, match="symlink"):
        bibpatch.guest_path(root, f"{planted}/precious")
    assert witness.read_text() == "host file", "a host file was written through the link"


def test_the_whole_patch_refuses_a_guest_that_planted_a_link(tmp_path, monkeypatch):
    # End to end: patch() must stop, not write half the guest and then notice.
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    target = tmp_path / "host"
    target.mkdir()
    (target / "AppleSetupDone-decoy").write_text("host file")
    (root / "private/etc").mkdir(parents=True, exist_ok=True)
    (root / "private/etc").rmdir()
    (root / "private/etc").symlink_to(target)
    with pytest.raises(bibpatch.PatchError, match="symlink"):
        bibpatch.patch(root, bibpatch.Account("admin", "pw"))
    assert sorted(p.name for p in target.iterdir()) == ["AppleSetupDone-decoy"]


def test_a_guest_volume_with_no_links_still_patches(tmp_path, monkeypatch):
    # The guard must not refuse the ordinary case it was added to protect.
    import plistlib

    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    bibpatch.patch(root, bibpatch.Account("admin", "pw"), bibpatch.Keyboard(19, "Swiss German"))
    assert (root / "private/var/db/.AppleSetupDone").exists()
    assert (root / "private/etc/kcpassword").exists()
    record = plistlib.loads((root / "Library/Preferences/com.apple.loginwindow.plist").read_bytes())
    assert record["autoLoginUser"] == "admin"


# --- what the mutation pass showed the suite was not watching -------------------


def test_the_kcpassword_key_is_apples_not_merely_self_consistent():
    # A round-trip test XORs with the key it encoded with, so any typo in the key
    # round-trips perfectly — while loginwindow deobfuscates the real file with
    # Apple's real key, gets nonsense, and autologin fails.
    assert bytes([0x7D, 0x89, 0x52, 0x23, 0xD2, 0xBC, 0xDD, 0xEA, 0xA3, 0xB9, 0x1F]) == (
        bibpatch.KCPASSWORD_KEY
    )
    assert bibpatch.kcpassword("test") == bytes.fromhex("09ec2157d2bcddeaa3b91f")


def test_the_padding_is_observable_for_a_password_the_key_length_divides():
    # The documented failure ("macOS reads past the password") only happens when the
    # length is a multiple of 11, which is the case the old assertion could not see.
    encoded = bibpatch.kcpassword("elevenchars")
    assert len(encoded) == 22, "an 11-character password must still get its terminator"
    assert encoded[11] == bibpatch.KCPASSWORD_KEY[0], "byte 11 is the NUL, XOR-ed"


def test_autologin_writes_the_keys_loginwindow_actually_reads(tmp_path):
    import plistlib

    account = bibpatch.Account("admin", "pw")
    bibpatch.enable_autologin(tmp_path, account)
    record = plistlib.loads(
        (tmp_path / "Library/Preferences/com.apple.loginwindow.plist").read_bytes()
    )
    # Misspell either and loginwindow ignores the setting: the guest stops at a login
    # window and the generated 24-character password has to be typed by hand.
    assert record["autoLoginUser"] == "admin"
    assert record["autoLoginUserUID"] == 501
    kc = tmp_path / "private/etc/kcpassword"
    assert kc.read_bytes() == bibpatch.kcpassword("pw")
    assert kc.stat().st_mode & 0o777 == 0o600, "the guest's password must not be world-readable"


def test_remote_login_is_recorded_where_launchd_looks(tmp_path):
    import plistlib

    bibpatch.enable_remote_login(tmp_path)
    record = plistlib.loads(
        (tmp_path / "private/var/db/com.apple.xpc.launchd/disabled.plist").read_bytes()
    )
    # False means "not disabled". True, or a missing key, and 'bib vm setup' can
    # never reach the guest.
    assert record["com.openssh.sshd"] is False


def test_the_two_keyboard_defaults_are_the_same_fact_twice():
    # bib decides the fallback and bibpatch carries its own; a test that compares
    # each against itself would not notice them drifting apart.
    assert bib.DEFAULT_KEYBOARD == (0, "U.S.")
    assert (bibpatch.Keyboard().layout_id, bibpatch.Keyboard().name) == bib.DEFAULT_KEYBOARD


def _run_desktop_script(
    home,
    resolution: str,
    browser: str = "chrome",
    already_running: bool = False,
    starts: bool = True,
):
    """Execute desktop_script the way the engine does, with recording fakes.

    The pgrep fake answers the way the real one would: nothing is running until a
    launch has been recorded, unless `starts` says the browser is one that cannot
    run in this image at all — which is the case the script has to report.
    """
    bin_dir = home / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "xrandr").write_text(f'#!/bin/sh\necho "$@" >> {home}/xrandr.log\n')
    if already_running:
        answer = "exit 0\n"
    elif starts:
        answer = f"[ -f {home}/launch.log ] && exit 0\nexit 1\n"
    else:
        answer = "exit 1\n"
    (bin_dir / "pgrep").write_text(f"#!/bin/sh\n{answer}")
    (bin_dir / "nohup").write_text(f'#!/bin/sh\necho "$@" >> {home}/launch.log\n')
    # Recorded rather than performed: the lock paths are the container's absolute
    # ones, so a real rm here would reach outside the temporary directory — and
    # what the removal was asked to delete is worth asserting anyway.
    (bin_dir / "rm").write_text(f'#!/bin/sh\necho "$@" >> {home}/rm.log\n')
    # Faked as well, so a test never reads a real log — and BIB_LOG below keeps the
    # script from creating one in the host's /tmp in the first place.
    (bin_dir / "tail").write_text('#!/bin/sh\necho "(tail $*)"\n')
    for name in ("xrandr", "pgrep", "nohup", "rm", "tail"):
        (bin_dir / name).chmod(0o755)
    # Shortened, or the failure case would sit out the full production wait.
    original_wait = bib.LAUNCH_WAIT_SECS
    bib.LAUNCH_WAIT_SECS = 1
    try:
        script = bib.desktop_script(bibbrowsers.BROWSERS[browser])
    finally:
        bib.LAUNCH_WAIT_SECS = original_wait
    result = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", script],
        env={
            "HOME": str(home),
            "RES": resolution,
            "BIB_LOG": str(home / f"{browser}.log"),
            "PATH": f"{bin_dir}:/usr/bin:/bin",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    def read(name: str) -> str:
        path = home / name
        return path.read_text() if path.exists() else ""

    return SimpleNamespace(
        mode=read("xrandr.log"),
        launch=read("launch.log"),
        removed=read("rm.log"),
        stderr=result.stderr,
    )


@pytest.mark.parametrize("key", ["chrome", "chromium", "firefox"])
def test_the_desktop_script_launches_the_browser_the_image_actually_holds(key):
    # It was Chrome's binary on all three images, so this passed for one of them and
    # started nothing at all for the other two.
    import tempfile

    browser = bibbrowsers.BROWSERS[key]
    with tempfile.TemporaryDirectory() as tmp:
        run = _run_desktop_script(Path(tmp), "1920x1200", browser=key)
    assert "-s 1920x1200" in run.mode
    assert browser.container_bin in run.launch
    assert run.stderr == ""
    profile = f"{bib.KASM_HOME}/{browser.container_profile}"
    if browser.settings == "firefox":
        # Firefox rejects both of these, and would have opened them as file names.
        assert "--user-data-dir" not in run.launch
        assert "--no-sandbox" not in run.launch
        # Its stale lock is a pair of files inside the profile, never a Singleton.
        assert f"{profile}/*/.parentlock" in run.removed
        assert "Singleton" not in run.removed
    else:
        assert f"--user-data-dir={profile}" in run.launch
        assert "--no-sandbox" in run.launch
        assert f"{profile}/Singleton*" in run.removed


def test_the_desktop_script_does_not_start_a_second_browser():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run = _run_desktop_script(Path(tmp), "1920x1200", already_running=True)
    assert run.launch == "", "a second browser over a live one loses the first one's tabs"
    assert run.removed == "", "and its live profile lock is not something to delete"


def test_the_desktop_script_skips_xrandr_when_no_mode_was_asked_for():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run = _run_desktop_script(Path(tmp), "")
    assert run.mode == "", "an empty BIB_RESOLUTION means follow the browser window"
    assert bibbrowsers.BROWSERS["chrome"].container_bin in run.launch


def test_the_desktop_script_reports_a_browser_that_never_came_up():
    # `nohup ... &` exits 0 the moment the shell forks, so a browser that is not in
    # the image at all used to be a silent success: a black desktop, and bib saying
    # everything was fine. ensure_desktop warns on any output, so saying it here is
    # what makes it visible.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        run = _run_desktop_script(Path(tmp), "", browser="firefox", starts=False)
    assert run.launch != "", "it still has to try before it complains"
    assert "Firefox did not start" in run.stderr
    assert "firefox.log" in run.stderr, "the log it wrote is the first thing to look at"


@pytest.mark.parametrize(
    "returncode,stdout,expected",
    [
        (0, "true\n", True),
        (0, "false\n", False),  # a stopped container: inspect still exits 0
        (1, "", False),  # no such container
    ],
)
def test_container_running_reads_the_state_not_just_the_exit_code(
    monkeypatch, returncode, stdout, expected
):
    # With `or` instead of `and`, a stopped container reports as running and
    # `box up` prints "Already running" for something dead.
    monkeypatch.setattr(
        bib,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=""),
    )
    assert bib.container_running("podman", bib.Config()) is expected


def test_ui_status_reports_the_code_and_none_when_nothing_answers(monkeypatch):
    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class _Opener:
        def __init__(self, answer):
            self.answer = answer

        def open(self, *a, **k):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    monkeypatch.setattr(bib.urllib.request, "build_opener", lambda *h: _Opener(_Response()))
    assert bib.ui_status(bib.Config()) == 200
    assert bib.ui_is_up(bib.Config()) is True

    monkeypatch.setattr(
        bib.urllib.request,
        "build_opener",
        lambda *h: _Opener(urllib.error.URLError("connection refused")),
    )
    assert bib.ui_status(bib.Config()) is None
    assert bib.ui_is_up(bib.Config()) is False


def test_the_health_check_never_goes_through_a_proxy(monkeypatch):
    # urlopen honours HTTPS_PROXY. This only talks to localhost, so a proxy could
    # only break it: a healthy container was reported dead and then rebuilt.
    handlers = {}

    class _Opener:
        def open(self, *a, **k):
            raise urllib.error.URLError("x")

    def fake_build_opener(*given):
        handlers["given"] = given
        return _Opener()

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setattr(bib.urllib.request, "build_opener", fake_build_opener)
    bib.ui_status(bib.Config())
    proxy = next(h for h in handlers["given"] if isinstance(h, bib.urllib.request.ProxyHandler))
    assert proxy.proxies == {}, "an empty proxy map is what bypasses the environment"


# --- what round 11 confirmed and the last commit did not close -----------------


def test_a_missing_patcher_is_named_before_the_download_starts(credentials, monkeypatch, tmp_path):
    # It used to be found only in the patch step, so a build that could never
    # finish still downloaded ~15 GB and installed macOS first.
    calls: list[str] = []
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "sudo_is_cached", lambda: True)
    monkeypatch.setattr(bib, "find_guest_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(bib, "PATCHER", tmp_path / "not-shipped" / "bibpatch.py")
    monkeypatch.setattr(bib, "run", lambda *a, **k: calls.append(a) or None)
    with pytest.raises(bib.Failure, match="patcher is missing"):
        bib.cmd_vm_create("tart", bib.VmConfig())
    assert calls == [], "nothing may be downloaded before the check"


def test_a_missing_sudo_credential_is_named_before_the_download_starts(
    credentials, monkeypatch, tmp_path
):
    calls: list[str] = []
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "sudo_is_cached", lambda: False)
    monkeypatch.setattr(bib, "run", lambda *a, **k: calls.append(a) or None)
    with pytest.raises(bib.Failure, match="Nothing has been downloaded yet"):
        bib.cmd_vm_create("tart", bib.VmConfig())
    assert calls == []


def test_a_second_account_on_the_same_uid_is_refused(tmp_path, monkeypatch):
    # uid is fixed at 501, so 'BIB_VM_USER=bob bib vm prepare' over a guest built as
    # 'admin' would give bob full access to admin's home, Chrome profile and login
    # keychain — and nothing said so.
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    bibpatch.patch(root, bibpatch.Account("admin", "pw"))
    with pytest.raises(bibpatch.PatchError, match="already has an account 'admin'"):
        bibpatch.patch(root, bibpatch.Account("bob", "pw"))


def test_preparing_the_same_account_twice_is_still_allowed(tmp_path, monkeypatch):
    # 'bib vm prepare' after a failed patch is the documented retry, so the guard
    # must not turn it into an error.
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    bibpatch.patch(root, bibpatch.Account("admin", "pw"))
    bibpatch.patch(root, bibpatch.Account("admin", "pw"))  # must not raise


def test_logs_reports_an_engine_failure_as_a_failure(monkeypatch):
    # It used to exit 0 whatever the engine did, so `bib box logs > out.txt || handle`
    # never fired and out.txt was silently empty.
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    with pytest.raises(bib.Failure, match="bib box status"):
        bib.cmd_logs("podman", bib.Config())


def test_logs_stays_quiet_when_the_engine_is_happy(monkeypatch):
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    bib.cmd_logs("podman", bib.Config())  # must not raise


def test_the_guest_script_keeps_its_scratch_space_out_of_tmp():
    # /tmp in the guest is writable by every account there, so a staged Chrome.app
    # could be swapped between the copy and the move into /Applications. It also
    # made two concurrent test runs share host paths and delete each other's.
    script = bib.guest_install_script("pw")
    body = "\n".join(ln for ln in script.split("\n") if not ln.lstrip().startswith("#"))
    # Spelled this way so the assertion itself is not a hardcoded temp path.
    assert not re.search(r"/tm[p]/", body)
    assert 'BIB_WORK="$HOME/.cache/bib"' in body
    assert "trap cleanup EXIT" in body, "the scratch space must be cleaned up"
    # The detach has to come before the rm: rm -rf over a mounted DMG recurses into
    # a read-only volume, fails, and leaves the image attached — so the next run
    # aborts here instead of installing anything.
    opened = body.index("cleanup() {")
    cleanup = body[opened : body.index("}", opened)]
    assert cleanup.index("hdiutil detach") < cleanup.index("rm -rf")


def test_the_guest_script_cleans_up_after_itself_even_when_it_fails(tmp_path):
    # The trap is the only cleanup: an aborted install must not leave a half-copied
    # Chrome and a mounted disk image in the account's home for ever.
    result = _run_guest_script(bib.guest_install_script("pw"), tmp_path, share_exists=False)
    assert result.returncode != 0, "a missing share is still a failure"
    assert not (tmp_path / ".cache" / "bib").exists()


# --- the clipboard agent, which was started by a flag that does not exist -------


def test_the_clipboard_agent_is_started_by_launchd_not_an_invented_flag():
    # `--install-daemon=launchd` was never a real flag; the agent's own README says
    # it is started from a launchd plist. So it was downloaded, installed, and never
    # ran — and copy-paste, which the generated password exists for, never worked.
    script = bib.guest_install_script("pw")
    assert "--install-daemon" not in script
    assert "--run-agent" in script, "the session agent is the one that sees the pasteboard"
    assert "--run-daemon" not in script, "a root daemon cannot reach the pasteboard"
    assert bib.AGENT_PLIST_PATH in script
    assert "launchctl bootstrap" in script


def test_the_agent_plist_is_a_plist_launchd_will_accept():
    import plistlib

    record = plistlib.loads(bib.AGENT_PLIST.encode())
    assert record["Label"] == bib.AGENT_LABEL
    assert record["ProgramArguments"] == [bib.AGENT_BIN, "--run-agent"]
    assert record["RunAtLoad"] is True
    assert record["KeepAlive"] is True


def test_the_agent_plist_survives_the_shell_that_writes_it(tmp_path):
    # It is written with printf from a single-quoted string; an unescaped quote or a
    # mangled newline would install a plist launchd silently ignores.
    import plistlib

    script = bib.guest_install_script("pw")
    end_marker = '> "$BIB_WORK/agent.plist"'
    end = script.index(end_marker) + len(end_marker)
    # The first printf in the script belongs to sudo_pw; this is the last one before
    # the plist is written.
    start = script.rindex("printf ", 0, end)
    out = tmp_path / "agent.plist"
    statement = script[start:end].replace('"$BIB_WORK/agent.plist"', str(out))
    subprocess.run(["/bin/sh", "-c", statement], check=True, capture_output=True)  # noqa: S603
    assert plistlib.loads(out.read_bytes())["Label"] == bib.AGENT_LABEL


# --- settings that used to fail late, or quietly do the wrong thing ------------


def test_an_empty_share_is_refused_rather_than_sharing_the_current_directory(monkeypatch):
    # "~/Downloads/browser-vm" expands to the working directory when empty, and the
    # guest would get whatever happened to be there.
    monkeypatch.setenv("BIB_VM_SHARE", "  ")
    with pytest.raises(bib.Failure, match="BIB_VM_SHARE is empty"):
        bib.VmConfig().check()


@pytest.mark.parametrize("value", ["1920", "1920*1200", "big", "1920x"])
def test_a_malformed_display_is_refused_like_the_box_variant_does(monkeypatch, value):
    monkeypatch.setenv("BIB_VM_DISPLAY", value)
    with pytest.raises(bib.Failure, match="BIB_VM_DISPLAY"):
        bib.VmConfig().check()


def test_a_well_formed_display_and_share_pass(monkeypatch):
    monkeypatch.setenv("BIB_VM_DISPLAY", "1280x800")
    bib.VmConfig().check()  # must not raise


def test_deleting_the_vm_takes_its_password_and_keys_with_it(credentials, monkeypatch):
    # Left behind, the next build silently reuses them, and 'bib vm password' keeps
    # printing a password for a guest that no longer exists.
    bib.guest_password(create=True)
    bib.ensure_vm_keys()
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    bib.cmd_vm_delete("tart", bib.VmConfig())
    for gone in (bib.CREDENTIALS, bib.VM_KEY, bib.VM_HOST_KEY, bib.KNOWN_HOSTS):
        assert not gone.exists(), f"{gone.name} outlived the VM"


def test_a_cancelled_delete_keeps_everything(credentials, monkeypatch):
    bib.guest_password(create=True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    bib.cmd_vm_delete("tart", bib.VmConfig())
    assert bib.CREDENTIALS.exists()


@pytest.mark.parametrize(
    "link,expected",
    [
        ("/var/db/timezone/zoneinfo/Europe/Zurich", ("Europe/Zurich", "Zurich")),
        ("/var/db/timezone/zoneinfo/UTC", ("UTC", "UTC")),
        ("/var/db/timezone/zoneinfo/America/Argentina/Buenos_Aires", None),
        ("/somewhere/else", ("Europe/Zurich", "Zurich")),
    ],
)
def test_the_host_time_zone_is_read_from_the_localtime_link(monkeypatch, link, expected):
    # A single-component zone such as UTC is its own city, not a reason to fall back
    # to the hardcoded default.
    monkeypatch.setattr(bib.os, "readlink", lambda p: link)
    result = bib.host_time_zone()
    assert result == (expected or ("America/Argentina/Buenos_Aires", "Buenos Aires"))


def test_the_guest_gets_the_key_and_the_host_key_it_will_be_checked_against(tmp_path, monkeypatch):
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    keys = bibpatch.Keys(
        authorized="ssh-ed25519 AAAAPUB bib",
        host_private="-----BEGIN OPENSSH PRIVATE KEY-----\nx\n",
        host_public="ssh-ed25519 AAAAHOST bib-guest",
    )
    bibpatch.patch(root, bibpatch.Account("admin", "pw"), None, keys)
    authorized = root / "Users/admin/.ssh/authorized_keys"
    assert authorized.read_text().strip() == "ssh-ed25519 AAAAPUB bib"
    assert authorized.stat().st_mode & 0o777 == 0o600
    assert (root / "Users/admin/.ssh").stat().st_mode & 0o777 == 0o700
    host = root / "private/etc/ssh/ssh_host_ed25519_key"
    assert host.read_text() == keys.host_private
    assert host.stat().st_mode & 0o777 == 0o600, "sshd refuses a world-readable host key"
    assert host.with_suffix(".pub").read_text().strip() == keys.host_public


def test_a_secret_is_never_briefly_world_readable(tmp_path):
    # Creating the file and chmod-ing it afterwards leaves a window in between.
    target = tmp_path / "secret"
    bibpatch.write_private(target, b"x")
    assert target.stat().st_mode & 0o777 == 0o600


def test_a_planted_fifo_does_not_hang_the_root_patcher(tmp_path):
    # guest_path only refused symlinks. Opening a FIFO blocks until something opens
    # the other end, which nothing ever does — so the patch hung for ever, as root.
    # Sockets and device nodes are refused by the same check.
    root = tmp_path / "volume"
    (root / "private/etc").mkdir(parents=True)
    os.mkfifo(root / "private/etc/kcpassword")
    with pytest.raises(bibpatch.PatchError, match="neither a directory nor a regular file"):
        bibpatch.guest_path(root, "private/etc/kcpassword")


def test_a_fifo_in_the_middle_of_the_path_is_refused_too(tmp_path):
    root = tmp_path / "volume"
    (root / "private").mkdir(parents=True)
    os.mkfifo(root / "private/etc")
    with pytest.raises(bibpatch.PatchError, match="neither a directory nor a regular file"):
        bibpatch.guest_path(root, "private/etc/kcpassword")


# --- round 13's remainder ------------------------------------------------------


def test_shell_refuses_instead_of_reporting_a_session_it_never_opened(monkeypatch):
    # `bib box shell && echo attached` printed the engine's refusal and then
    # "attached", because the exec's exit code was ignored.
    monkeypatch.setattr(bib, "container_running", lambda *a, **k: False)
    with pytest.raises(bib.Failure, match="bib box up"):
        bib.cmd_shell("podman", bib.Config())


def test_shell_opens_when_the_container_is_there(calls, monkeypatch):
    monkeypatch.setattr(bib, "container_running", lambda *a, **k: True)
    bib.cmd_shell("podman", bib.Config())
    assert "exec" in flat(calls)


def test_the_first_pull_is_visible_instead_of_several_silent_gigabytes(calls, monkeypatch):
    # `run -d` is captured, which also swallowed the whole first pull: one line of
    # output and then nothing at all for several GB.
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    seen = {}

    def fake(engine, *args, **kwargs):
        seen.setdefault("cmds", []).append((args, kwargs))
        return subprocess.CompletedProcess([], 0 if args[0] == "pull" else 1, stdout="", stderr="")

    monkeypatch.setattr(bib, "run", fake)
    bib.ensure_image("podman", bib.Config())
    pull = next(a for a, _ in seen["cmds"] if a[0] == "pull")
    kwargs = next(k for a, k in seen["cmds"] if a[0] == "pull")
    assert "--platform" in pull
    assert not kwargs.get("capture"), "capturing the pull is what hid it"


def test_an_image_already_present_in_the_right_architecture_is_not_pulled_again(monkeypatch):
    pulled = []
    monkeypatch.setattr(
        bib,
        "run",
        lambda e, *a, **k: (
            pulled.append(a[0]) or subprocess.CompletedProcess([], 0, stdout="amd64\n", stderr="")
        ),
    )
    bib.ensure_image("podman", bib.Config())
    assert "pull" not in pulled


def test_an_image_of_the_wrong_architecture_is_pulled_again_visibly(monkeypatch):
    # An arm64 copy of the same tag satisfies `image inspect`, and `run --platform
    # linux/amd64` then pulls the amd64 one anyway — captured, so silently.
    seen = []
    monkeypatch.setattr(
        bib,
        "run",
        lambda e, *a, **k: (
            seen.append((a[0], k.get("capture")))
            or subprocess.CompletedProcess([], 0, stdout="arm64\n", stderr="")
        ),
    )
    bib.ensure_image("podman", bib.Config())
    assert ("pull", None) in seen or ("pull", False) in seen


def test_a_failed_pull_is_reported(monkeypatch):
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    with pytest.raises(bib.Failure, match="could not pull"):
        bib.ensure_image("podman", bib.Config())


@pytest.mark.parametrize("value,expected", [["1920X1200", "1920x1200"], ["1280 x 800", "1280x800"]])
def test_the_vm_display_is_normalised_like_the_box_one(monkeypatch, value, expected):
    # tart takes 1920X1200 without complaint and then ignores it, leaving the guest
    # at 1024x768 with nothing said.
    monkeypatch.setenv("BIB_VM_DISPLAY", value)
    vm = bib.VmConfig()
    vm.check()
    assert vm.normalised_display == expected


def test_an_account_the_guest_already_has_is_not_overwritten(tmp_path, monkeypatch):
    # root, daemon and _spotlight all match the name rules. Replacing root's record
    # with a uid-501 one leaves the guest with no working sudo at all.
    import plistlib

    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    users = root / "private/var/db/dslocal/nodes/Default/users"
    with (users / "root.plist").open("wb") as fh:
        plistlib.dump({"name": ["root"], "uid": ["0"]}, fh, fmt=plistlib.FMT_BINARY)
    with pytest.raises(bibpatch.PatchError, match="already has an account called 'root'"):
        bibpatch.patch(root, bibpatch.Account("root", "pw"))
    assert plistlib.loads((users / "root.plist").read_bytes())["uid"] == ["0"]


# --- round 12's remainder ------------------------------------------------------


@pytest.mark.parametrize(
    "mount_output,expected",
    [
        ("/dev/disk9s1 on /Volumes/Data (apfs, local, journaled)\n", True),
        ("/dev/disk9s1 on /Volumes/Data (apfs, local, noowners)\n", False),
        # The line that used to answer for a different volume entirely.
        ("/dev/disk3s1 on /Volumes/Data Backup (apfs, local, journaled)\n", False),
        (
            "/dev/disk3s1 on /Volumes/Data Backup (apfs, journaled)\n"
            "/dev/disk9s1 on /Volumes/Data (apfs, noowners)\n",
            False,
        ),
        ("", False),
    ],
)
def test_ownership_is_read_from_this_volumes_line_only(monkeypatch, mount_output, expected):
    # A substring match on " on /Volumes/Data " also matched "/Volumes/Data Backup",
    # so another disk on the host could vouch for this one — and every chown
    # afterwards would be a silent no-op.
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=mount_output, stderr=""),
    )
    assert bibpatch.ownership_is_honoured(Path("/Volumes/Data")) is expected


@pytest.mark.parametrize("content", [[1, 2, 3], "a string", 42])
def test_a_plist_that_is_not_a_dictionary_is_not_a_traceback(tmp_path, content):
    # A plist root can be any type, and this runs as root: an array where a dict was
    # expected used to come out as a traceback rather than a message.
    import plistlib

    target = tmp_path / "Library" / "Preferences" / "com.apple.loginwindow.plist"
    target.parent.mkdir(parents=True)
    with target.open("wb") as fh:
        plistlib.dump(content, fh, fmt=plistlib.FMT_BINARY)
    assert bibpatch.read_plist(tmp_path, "Library/Preferences/com.apple.loginwindow.plist") == {}


def test_a_diskutil_that_will_not_describe_the_volume_is_reported(monkeypatch):
    def fake(cmd, *a, **k):
        if "info" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"could not find disk")
        if cmd[0] == "/sbin/mount":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(bibpatch.subprocess, "run", fake)
    with pytest.raises(bibpatch.PatchError, match="could not find disk"):
        bibpatch.mount("/dev/disk9s1")


def test_the_agent_directory_is_created_before_the_agent_is_installed():
    # BSD install does not create its target directory, and a fresh guest can have
    # /usr/local with no bin in it.
    script = bib.guest_install_script("pw")
    assert f'sudo_pw install -d -m 0755 "$(dirname {bib.AGENT_BIN})"' in script
    assert script.index("install -d") < script.index(
        f'"$BIB_WORK/tart-guest-agent" {bib.AGENT_BIN}'
    )


def test_a_python3_that_cannot_run_is_not_treated_as_an_interpreter(monkeypatch):
    # /usr/bin/python3 exists on every Mac and is a stub without the Command Line
    # Tools: it is executable, and exits non-zero the moment it runs.
    monkeypatch.setattr(bib.shutil, "which", lambda name: None)
    monkeypatch.setattr(bib.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1))
    with pytest.raises(bib.Failure, match="xcode-select --install"):
        bib.find_guest_python()


def test_a_working_python3_is_used_as_it_is(monkeypatch):
    monkeypatch.setattr(bib.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0))
    assert bib.find_guest_python() == "/usr/bin/python3"


# --- round 14 and 15 -----------------------------------------------------------


def test_a_delete_that_failed_keeps_the_password_and_keys(credentials, monkeypatch):
    # Wiping them on a failed delete locks bib out of a guest that is still there.
    bib.guest_password(create=True)
    bib.ensure_vm_keys()
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(
        bib,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="VM is running"),
    )
    with pytest.raises(bib.Failure, match="nothing is locked out"):
        bib.cmd_vm_delete("tart", bib.VmConfig())
    for kept in (bib.CREDENTIALS, bib.VM_KEY, bib.VM_HOST_KEY, bib.KNOWN_HOSTS):
        assert kept.exists(), f"{kept.name} was destroyed by a delete that did not happen"


def test_ssh_refuses_every_way_of_being_asked_for_a_password(credentials):
    # PasswordAuthentication=no alone leaves keyboard-interactive, which sshd offers
    # the same password through.
    cmd = bib.ssh_command(bib.VmConfig(), "192.168.1.50")
    assert "PasswordAuthentication=no" in cmd
    assert "KbdInteractiveAuthentication=no" in cmd
    assert "NumberOfPasswordPrompts=0" in cmd


def test_the_interpreter_is_checked_before_the_download_too(credentials, monkeypatch, tmp_path):
    # The README promises the preflight catches this; it used to run after the build.
    calls: list = []
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib, "sudo_is_cached", lambda: True)
    monkeypatch.setattr(bib, "find_patcher", lambda: tmp_path / "bibpatch.py")
    monkeypatch.setattr(bib.shutil, "which", lambda name: None)
    monkeypatch.setattr(bib.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1))
    monkeypatch.setattr(bib, "run", lambda *a, **k: calls.append(a) or None)
    with pytest.raises(bib.Failure, match="xcode-select --install"):
        bib.cmd_vm_create("tart", bib.VmConfig())
    assert calls == [], "nothing may be downloaded before the check"


def test_preparing_twice_keeps_the_accounts_generated_uid(tmp_path, monkeypatch):
    # Group membership records the GUID: a fresh one on every prepare leaves admin
    # and staff pointing at a user that no longer exists under that id.
    import plistlib

    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    users = root / "private/var/db/dslocal/nodes/Default/users"
    groups = root / "private/var/db/dslocal/nodes/Default/groups"
    bibpatch.patch(root, bibpatch.Account("admin", "pw"))
    first = plistlib.loads((users / "admin.plist").read_bytes())["generateduid"][0]
    bibpatch.patch(root, bibpatch.Account("admin", "pw"))
    assert plistlib.loads((users / "admin.plist").read_bytes())["generateduid"][0] == first
    members = plistlib.loads((groups / "admin.plist").read_bytes())["groupmembers"]
    assert members == [first], "the group must still name the account that exists"


def test_a_malformed_xml_plist_is_not_a_traceback(tmp_path):
    # Well-formed-looking XML that is not valid raises ExpatError, which is none of
    # the exceptions already caught — out of a step running as root.
    target = tmp_path / "Library" / "Preferences" / "com.apple.loginwindow.plist"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'<?xml version="1.0"?>\n<plist version="1.0"><dict><key>x')
    assert bibpatch.read_plist(tmp_path, "Library/Preferences/com.apple.loginwindow.plist") == {}


def test_each_vm_name_keeps_its_own_password_and_keys(monkeypatch, tmp_path):
    # They used to sit flat in one directory, so a second BIB_VM_NAME reused the
    # first one's key — and deleting either took the other's away.
    monkeypatch.setattr(bib.Path, "home", classmethod(lambda c: tmp_path))
    first = bib.secrets_dir()
    assert first.name == "browser-vm"
    monkeypatch.setenv("BIB_VM_NAME", "other")
    assert bib.secrets_dir() != first
    assert bib.secrets_dir().name == "other"
    assert bib.secrets_dir().parent == first.parent


def test_the_six_files_that_have_to_be_migrated_are_named_here_too():
    # All three migration tests seed and assert with SECRET_NAMES, so the tuple was
    # only ever compared with itself: dropping "vm-credentials" from it left the
    # password stranded in the flat directory with the suite green, and the first
    # command an upgrading user ran died on a guest that exists.
    assert set(bib.SECRET_NAMES) == {
        "vm-credentials",
        "vm-key",
        "vm-key.pub",
        "vm-host-key",
        "vm-host-key.pub",
        "vm-known-hosts",
    }
    # And they are the files the rest of bib actually uses.
    assert bib.CREDENTIALS.name in bib.SECRET_NAMES
    assert bib.VM_KEY.name in bib.SECRET_NAMES
    assert bib.VM_KEY.with_suffix(".pub").name in bib.SECRET_NAMES
    assert bib.VM_HOST_KEY.name in bib.SECRET_NAMES
    assert bib.VM_HOST_KEY.with_suffix(".pub").name in bib.SECRET_NAMES
    assert bib.KNOWN_HOSTS.name in bib.SECRET_NAMES


def test_secrets_an_older_bib_left_flat_go_to_the_default_vm(monkeypatch, tmp_path):
    # Nothing on disk says which guest they belong to. Moving them into whichever
    # name runs first takes them away from the guest actually using them — whose
    # disk was patched with that key pair, and which has no password fallback left.
    monkeypatch.setattr(bib.Path, "home", classmethod(lambda c: tmp_path))
    flat = tmp_path / ".config" / "browser-in-a-box"
    flat.mkdir(parents=True)
    for name in bib.SECRET_NAMES:
        (flat / name).write_text(f"old {name}\n")
    monkeypatch.setenv("BIB_VM_NAME", "work")  # a second name runs first
    bib.migrate_flat_secrets()
    for name in bib.SECRET_NAMES:
        assert (flat / bib.DEFAULT_VM_NAME / name).read_text() == f"old {name}\n"
        assert not (flat / name).exists(), "moved, not copied"
        assert not (flat / "work" / name).exists(), "they are not the second VM's"


def test_the_migration_does_not_overwrite_what_is_already_there(monkeypatch, tmp_path):
    monkeypatch.setattr(bib.Path, "home", classmethod(lambda c: tmp_path))
    flat = tmp_path / ".config" / "browser-in-a-box"
    (flat / bib.DEFAULT_VM_NAME).mkdir(parents=True)
    (flat / "vm-credentials").write_text("old\n")
    (flat / bib.DEFAULT_VM_NAME / "vm-credentials").write_text("current\n")
    bib.migrate_flat_secrets()
    assert (flat / bib.DEFAULT_VM_NAME / "vm-credentials").read_text() == "current\n"


def test_delete_removes_what_an_older_bib_left_flat_too(credentials, monkeypatch, tmp_path):
    # It used to unlink per-name paths that did not exist yet, print "Deleted.",
    # and the next command migrated the flat originals back in.
    monkeypatch.setattr(bib.Path, "home", classmethod(lambda c: tmp_path))
    flat = tmp_path / ".config" / "browser-in-a-box"
    flat.mkdir(parents=True)
    for name in bib.SECRET_NAMES:
        (flat / name).write_text("old\n")
    monkeypatch.setattr(bib, "SECRETS", flat / bib.DEFAULT_VM_NAME)
    monkeypatch.setattr(bib, "CREDENTIALS", bib.SECRETS / "vm-credentials")
    monkeypatch.setattr(bib, "VM_KEY", bib.SECRETS / "vm-key")
    monkeypatch.setattr(bib, "VM_HOST_KEY", bib.SECRETS / "vm-host-key")
    monkeypatch.setattr(bib, "KNOWN_HOSTS", bib.SECRETS / "vm-known-hosts")
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    bib.cmd_vm_delete("tart", bib.VmConfig())
    for name in bib.SECRET_NAMES:
        assert not (flat / name).exists(), f"{name} survived a delete that said Deleted."
        assert not (flat / bib.DEFAULT_VM_NAME / name).exists()


def test_the_shell_reports_an_exec_the_engine_refused(monkeypatch):
    # Checking the container first was not enough: the exec itself can fail, and
    # its exit code was still thrown away.
    monkeypatch.setattr(bib, "container_running", lambda *a, **k: True)
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 125, stdout="", stderr="")
    )
    with pytest.raises(bib.Failure, match="could not start a shell"):
        bib.cmd_shell("podman", bib.Config())


def test_a_shell_that_exits_non_zero_is_not_a_bib_failure(monkeypatch):
    # The user's own shell exiting 1 is their business, not a bib error.
    monkeypatch.setattr(bib, "container_running", lambda *a, **k: True)
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    bib.cmd_shell("podman", bib.Config())  # must not raise


@pytest.mark.parametrize("tty,expected", [(True, "-it"), (False, "-i")])
def test_a_pty_is_only_asked_for_when_there_is_a_terminal(monkeypatch, tty, expected):
    # podman blocks for ever allocating a pty for a stdin that is a pipe, so
    # `bib box shell` from a script or CI hung instead of failing.
    monkeypatch.setattr(bib, "container_running", lambda *a, **k: True)
    monkeypatch.setattr(bib.sys.stdin, "isatty", lambda: tty, raising=False)
    seen = {}
    monkeypatch.setattr(
        bib,
        "run",
        lambda e, *a, **k: seen.update(args=a) or subprocess.CompletedProcess([], 0),
    )
    bib.cmd_shell("podman", bib.Config())
    assert seen["args"][1] == expected


@pytest.mark.parametrize("mode", ["1600x900", "800x600", "1919x1199"])
def test_a_resolution_kasmvnc_does_not_ship_is_refused_not_merely_bounded(monkeypatch, mode):
    # In range is not the same as available: 1600x900 is smaller than the largest
    # mode and still not there, and xrandr then leaves the desktop at 1024x768
    # while bib warned three times and reported "Ready."
    monkeypatch.setenv("BIB_RESOLUTION", mode)
    with pytest.raises(bib.Failure, match="not one of the modes KasmVNC"):
        bib.Config().check()


@pytest.mark.parametrize("mode", ["1920x1200", "1280x800", "1024x768"])
def test_the_modes_kasmvnc_does_ship_are_accepted(monkeypatch, mode):
    monkeypatch.setenv("BIB_RESOLUTION", mode)
    bib.Config().check()  # must not raise


def test_the_sdist_carries_everything_its_tests_read():
    # Shipping tests/ without the files they open means the tests are there and
    # cannot run, which is the only reason to ship them.
    # Read as text, not with tomllib: that is 3.11+, and this project supports 3.10.
    root = Path(bib.__file__).resolve().parent
    source = (root / "pyproject.toml").read_text()
    block = re.search(
        r"\[tool\.hatch\.build\.targets\.sdist\].*?include\s*=\s*\[(.*?)\]", source, re.S
    )
    assert block, "the sdist include list is not where it was"
    included = re.findall(r'"([^"]+)"', block.group(1))
    for needed in ("scripts", "Formula", "renovate.json", "uv.lock", ".github", "tests"):
        assert needed in included, f"the tests read {needed}, so the sdist has to carry it"


def test_the_default_build_installs_the_key_it_will_connect_with(
    credentials, monkeypatch, tmp_path
):
    # The packer path had a test for this and the default path had none, so both
    # `ensure_vm_keys()` and the four argv entries could be deleted with the suite
    # green — producing exactly the "a VM bib could never connect to" the packer
    # fix was written for.
    _fake_guest_disk(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: (
            (seen.update(cmd=cmd) if any("bibpatch" in str(c) for c in cmd) else None)
            or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    bib._prepare_guest(bib.VmConfig(), "pw")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--authorized-key") + 1] == str(bib.VM_KEY.with_suffix(".pub"))
    assert cmd[cmd.index("--host-key") + 1] == str(bib.VM_HOST_KEY)
    assert bib.KNOWN_HOSTS.exists(), "the host key has to be pinned for bib to use it"


def test_the_default_build_generates_the_keys_if_they_are_missing(
    credentials, monkeypatch, tmp_path
):
    for stale in (
        bib.VM_KEY,
        bib.VM_KEY.with_suffix(".pub"),
        bib.VM_HOST_KEY,
        bib.VM_HOST_KEY.with_suffix(".pub"),
    ):
        stale.unlink(missing_ok=True)
    _fake_guest_disk(monkeypatch, tmp_path)
    real = bib.subprocess.run
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: (
            real(cmd, **kw) if "ssh-keygen" in str(cmd[0]) else subprocess.CompletedProcess(cmd, 0)
        ),
    )
    bib._prepare_guest(bib.VmConfig(), "pw")
    assert bib.VM_KEY.with_suffix(".pub").read_text().startswith("ssh-ed25519 ")
    assert bib.VM_HOST_KEY.with_suffix(".pub").read_text().startswith("ssh-ed25519 ")


def test_the_share_the_guest_looks_for_is_the_one_tart_mounts(calls, credentials, monkeypatch):
    # The old assertion compared GUEST_SHARE with itself. Change either side alone
    # and `bib vm setup` aborts before installing anything, because the script's
    # `[ -d "$GUEST_SHARE" ]` fails.
    monkeypatch.setattr(bib, "vm_running", lambda *a, **k: False)
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: True)
    bib.cmd_vm_up("tart", bib.VmConfig())
    shared = next(a for a in flat(calls).split() if a.startswith("--dir="))
    name = shared.removeprefix("--dir=").split(":", 1)[0]
    assert f"/Volumes/My Shared Files/{name}" == bib.GUEST_SHARE, (
        "the guest looks for the share under the name tart was told to use"
    )


# --- what the mutation pass found the suite still did not watch ----------------


def test_a_complete_patch_writes_every_marker_it_documents(tmp_path, monkeypatch):
    # Each of these had a test calling the function directly, so deleting the CALL
    # from patch() left the suite green — and the guest boots into the very Setup
    # Assistant the offline path exists to avoid, or with sshd still disabled.
    import plistlib

    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    keys = bibpatch.Keys(
        authorized="ssh-ed25519 AAAAPUB bib",
        host_private="KEY\n",
        host_public="ssh-ed25519 AAAAHOST g",
    )
    bibpatch.patch(
        root, bibpatch.Account("admin", "pw"), bibpatch.Keyboard(19, "Swiss German"), keys
    )

    assert (root / "private/var/db/.AppleSetupDone").exists(), "the system assistant returns"
    assert (root / "Library/User Template/.skipbuddy").exists(), "later accounts see it again"
    for scope in ("Library/Preferences", "Users/admin/Library/Preferences"):
        seen = plistlib.loads((root / scope / "com.apple.SetupAssistant.plist").read_bytes())
        assert seen["DidSeeCloudSetup"] is True, f"{scope}: the per-user assistant returns"
        assert seen["DidSeePrivacy"] is True
    launchd = plistlib.loads(
        (root / "private/var/db/com.apple.xpc.launchd/disabled.plist").read_bytes()
    )
    assert launchd["com.openssh.sshd"] is False, "without this bib can never reach the guest"
    login = plistlib.loads((root / "Library/Preferences/com.apple.loginwindow.plist").read_bytes())
    assert login["autoLoginUser"] == "admin"
    assert (root / "private/etc/kcpassword").read_bytes() == bibpatch.kcpassword("pw")
    layout = plistlib.loads(
        (root / "Users/admin/Library/Preferences/com.apple.HIToolbox.plist").read_bytes()
    )
    assert layout["AppleSelectedInputSources"][0]["KeyboardLayout ID"] == 19
    assert (root / "Users/admin/.ssh/authorized_keys").read_text().strip() == keys.authorized
    assert (root / "private/etc/ssh/ssh_host_ed25519_key").read_text() == keys.host_private
    assert plistlib.loads(
        (root / "private/var/db/dslocal/nodes/Default/users/admin.plist").read_bytes()
    )["uid"] == ["501"]


def test_up_pulls_the_image_before_it_runs_the_container(monkeypatch):
    # Deleting the ensure_image call puts the pull back inside the captured
    # `run -d`, which is the several silent gigabytes round 13 reported as a hang.
    order: list[str] = []
    monkeypatch.setattr(bib, "container_running", lambda *a, **k: False)
    monkeypatch.setattr(bib, "ensure_image", lambda e, c: order.append("pull"))
    monkeypatch.setattr(bib, "wait_for_ui", lambda e, c: None)
    monkeypatch.setattr(bib, "ensure_desktop", lambda e, c: True)
    monkeypatch.setattr(
        bib,
        "run",
        lambda e, *a, **k: order.append(a[0]) or subprocess.CompletedProcess([], 0, stdout=""),
    )
    bib.cmd_up("podman", bib.Config())
    assert order.index("pull") < order.index("run"), "the pull has to happen outside `run -d`"


def test_the_password_verifier_matches_a_known_answer(monkeypatch):
    # Swapping the password and the salt, or reordering the PBKDF2 arguments, still
    # produces a plausible-looking verifier — one macOS will never match, so the
    # account exists and refuses its own password for ever.
    monkeypatch.setattr(bibpatch.secrets, "token_bytes", lambda n: bytes(range(n)))
    import plistlib

    entry = plistlib.loads(bibpatch.shadow_hash_data("hunter2"))["SALTED-SHA512-PBKDF2"]
    expected = hashlib.pbkdf2_hmac("sha512", b"hunter2", bytes(range(32)), 50_000, 128)
    assert entry["entropy"] == expected
    assert entry["salt"] == bytes(range(32))


def test_the_sudo_probe_can_never_block_on_a_prompt(monkeypatch):
    # Without -n the probe whose docstring promises an exit code instead of a hang
    # waits for a password bib says it will never ask for.
    seen = {}
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: seen.update(cmd=cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert bib.sudo_is_cached() is True
    assert "-n" in seen["cmd"]


def test_the_keepalive_refreshes_without_prompting(monkeypatch):
    # sudo forgets a credential in about five minutes and the build takes thirty to
    # sixty; without this the last step of every unattended build was refused.
    seen: list[list[str]] = []
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    keepalive = bib.SudoKeepalive(interval=0.01)
    with keepalive:
        time.sleep(0.15)
    assert seen, "the credential was never refreshed"
    assert all(cmd[:3] == ["/usr/bin/sudo", "-n", "-v"] for cmd in seen)


@pytest.mark.parametrize(
    "planted,relative",
    [
        ("Users/admin/.ssh", "Users/admin/.ssh/authorized_keys"),
        ("private/etc", "private/etc/kcpassword"),
    ],
)
def test_a_file_where_a_directory_belongs_is_a_message_not_a_traceback(tmp_path, planted, relative):
    root = tmp_path / "volume"
    (root / planted).parent.mkdir(parents=True)
    (root / planted).write_text("not a directory")
    with pytest.raises(bibpatch.PatchError, match="file where a directory has to be"):
        bibpatch.guest_path(root, relative, make_parents=True)


def test_a_directory_where_a_file_belongs_is_a_message_too(tmp_path):
    root = tmp_path / "volume"
    (root / "private/etc/kcpassword").mkdir(parents=True)
    with pytest.raises(bibpatch.PatchError, match="directory in the guest where a file"):
        bibpatch.guest_path(root, "private/etc/kcpassword", make_parents=True)


# --- round 17 ------------------------------------------------------------------


def test_preparing_a_guest_that_already_has_the_key_still_works(tmp_path, monkeypatch):
    # `bib vm prepare` is the documented retry for a half-hour build, and every
    # failure message points at it. The directory guard added for a file where a
    # directory belongs fired on ~/.ssh, which is a directory and is supposed to be.
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    keys = bibpatch.Keys(
        authorized="ssh-ed25519 AAAAPUB bib",
        host_private="KEY\n",
        host_public="ssh-ed25519 AAAAHOST guest",
    )
    bibpatch.patch(root, bibpatch.Account("admin", "pw"), None, keys)
    bibpatch.patch(root, bibpatch.Account("admin", "pw"), None, keys)  # must not raise
    authorized = root / "Users/admin/.ssh/authorized_keys"
    assert authorized.read_text().strip() == keys.authorized
    assert authorized.stat().st_mode & 0o777 == 0o600


def test_a_file_where_the_ssh_directory_belongs_is_still_refused(tmp_path, monkeypatch):
    # The guard has to keep catching the case it was added for.
    monkeypatch.setattr(bibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    (root / "Users/admin").mkdir(parents=True)
    (root / "Users/admin/.ssh").write_text("not a directory")
    with pytest.raises(bibpatch.PatchError, match="file in the guest where a directory"):
        bibpatch.authorise_key(root, bibpatch.Account("admin", "pw"), "ssh-ed25519 AAAA bib")


def test_every_vm_command_migrates_an_older_installs_secrets(monkeypatch, tmp_path):
    # 'bib vm ssh' reads the keys through ssh_options() without ever calling
    # guest_password(), so it was the one command that failed on a pre-1.4 install
    # while every other one repaired it on the way past.
    monkeypatch.setattr(bib.Path, "home", classmethod(lambda c: tmp_path))
    flat = tmp_path / ".config" / "browser-in-a-box"
    flat.mkdir(parents=True)
    for name in bib.SECRET_NAMES:
        (flat / name).write_text(f"old {name}\n")
    monkeypatch.setattr(bib, "SECRETS", flat / bib.DEFAULT_VM_NAME)
    monkeypatch.setattr(bib, "find_tart", lambda: "/usr/bin/tart")
    monkeypatch.setattr(bib, "VM_ACTIONS", {"ssh": lambda tart, vm: None})
    bib.main(["vm", "ssh"])
    for name in bib.SECRET_NAMES:
        assert (flat / bib.DEFAULT_VM_NAME / name).exists(), f"{name} was not migrated"


def test_the_patcher_turns_the_key_paths_it_is_given_into_key_material(monkeypatch, tmp_path):
    # bib's side of this wire is asserted; the patcher's side was not, so main()
    # could throw both arguments away with the suite green.
    import io

    pub, priv = tmp_path / "k.pub", tmp_path / "h"
    pub.write_text("ssh-ed25519 AAAAPUB bib\n")
    priv.write_text("HOSTKEY\n")
    priv.with_suffix(".pub").write_text("ssh-ed25519 AAAAHOST guest\n")
    seen = {}
    monkeypatch.setattr(bibpatch, "prepare", lambda d, a, k, ks: seen.update(keys=ks))
    monkeypatch.setattr(bibpatch.sys, "stdin", io.StringIO("pw\n"))
    bibpatch.main(
        [
            "--disk",
            "/x/disk.img",
            "--user",
            "admin",
            "--authorized-key",
            str(pub),
            "--host-key",
            str(priv),
        ]
    )
    assert seen["keys"].authorized.strip() == "ssh-ed25519 AAAAPUB bib"
    assert seen["keys"].host_private == "HOSTKEY\n"
    assert seen["keys"].host_public.strip() == "ssh-ed25519 AAAAHOST guest"


def _template() -> str:
    return (Path(bib.__file__).resolve().parent / "packer" / "browser-vm.pkr.hcl").read_text()


def _template_command(needle: str, **values: str) -> str:
    """One provisioner line from the packer template, as the guest's shell sees it."""
    line = next(ln for ln in _template().splitlines() if needle in ln).strip()
    command = line.removesuffix(",").strip('"').replace("\\\\n", "\\n")
    for name, value in values.items():
        command = command.replace(f"${{var.{name}}}", value)
    return command


def test_the_templates_key_provisioner_writes_what_ssh_will_look_for():
    # Grepping the template for "authorized_keys" passes even if the provisioner
    # writes it somewhere sshd never reads. `~` is not expanded by an upload, so the
    # destination has to name the account's real home.
    template = _template()
    assert 'destination = "/Users/${var.username}/.ssh/authorized_keys"' in template
    assert 'destination = "/Users/${var.username}/.ssh/host_key"' in template


def test_the_template_never_interpolates_key_material_into_a_command():
    # A key whose comment holds a single quote would close the quoting of an inline
    # command, and the rest of it would run as shell. The file provisioner never
    # goes near a shell, so the content cannot be mistaken for instructions.
    for line in _template().splitlines():
        if not line.strip().startswith('"'):
            continue  # a file provisioner's content = , not an inline command
        for name in ("authorized_key", "host_private_key", "host_public_key"):
            assert f"${{var.{name}}}" not in line, f"{name} is interpolated into: {line.strip()}"


def test_the_template_refuses_to_build_a_guest_with_no_key(tmp_path):
    # An empty key used to build fine: `printf '%s\n' ''` writes a newline, so the
    # file exists, is not empty, and passed every size test on it — leaving a guest
    # that bib can never reach and a build that said it went well. The upload adds
    # that same newline, so the check has to be on the content.
    command = _template_command("grep -q '[^[:space:]]' ~/.ssh/authorized_keys || { echo 'no")
    ssh_dir = tmp_path / ".ssh"
    ssh_dir.mkdir()

    def run_guard(cmd: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", cmd],
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )

    # Exactly what an empty authorized_key is uploaded as.
    (ssh_dir / "authorized_keys").write_text("\n")
    result = run_guard(command)
    assert result.returncode != 0, "a file holding one newline holds no key"
    assert "could never log in" in result.stdout + result.stderr
    (ssh_dir / "authorized_keys").write_text("ssh-ed25519 AAAAPUB bib\n")
    assert run_guard(command).returncode == 0


def test_the_template_refuses_to_build_a_guest_with_no_host_key(tmp_path):
    # Without it bib cannot tell the guest apart from anything else answering on
    # that address, and the build would still report success.
    command = _template_command("test -s ~/.ssh/host_key ||")
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh/host_key").write_text("")

    def run_guard() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["/bin/sh", "-c", command],
            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            check=False,
        )

    result = run_guard()
    assert result.returncode != 0
    assert "could not verify" in result.stdout + result.stderr
    (tmp_path / ".ssh/host_key").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    assert run_guard().returncode == 0


def test_the_template_never_stages_the_private_key_where_others_can_read_it():
    # It used to go through /tmp: `printf ... > /tmp/host_key` creates it 0644, and
    # the install that narrows it to 0600 runs later, so every local user could read
    # the private key in between. An upload cannot be given a mode — it can be put
    # somewhere unreadable instead, which is what the 0700 ~/.ssh above is for.
    lines = _template().splitlines()
    for line in lines:
        if "host_key" in line:
            assert "tmp" not in line, f"the private key goes through a shared directory: {line}"
    narrow = next(i for i, ln in enumerate(lines) if "install -d -m 0700 ~/.ssh" in ln)
    upload = next(i for i, ln in enumerate(lines) if '.ssh/host_key"' in ln)
    assert narrow < upload, "the directory has to be private before the key lands in it"


def test_up_mounts_the_profile_volume_where_the_image_keeps_it(calls, monkeypatch):
    # Drop the -v, or let the container path drift from /home/kasm-user, and the
    # profile stops persisting while `box down` still promises it is kept.
    monkeypatch.setattr(bib, "container_running", lambda *a, **k: False)
    monkeypatch.setattr(bib, "ensure_image", lambda e, c: None)
    monkeypatch.setattr(bib, "wait_for_ui", lambda e, c: None)
    monkeypatch.setattr(bib, "ensure_desktop", lambda e, c: True)
    cfg = bib.Config()
    bib.cmd_up("podman", cfg)
    # The exact pair, not a substring: "/home/kasm-user/Downloads" contains
    # "/home/kasm-user", so the substring form could only ever catch a dropped -v,
    # never the drift the comment above names.
    argv = [a for call in calls for a in call]
    assert argv[argv.index("-v") + 1] == f"{cfg.volume}:/home/kasm-user"
    assert bib.KASM_HOME == "/home/kasm-user"
    for browser in bibbrowsers.expand(bibbrowsers.ALL):
        assert browser.container_profile and not browser.container_profile.startswith("/"), (
            f"{browser.key}: the profile has to live under what is mounted, or it stops persisting"
        )


def test_the_selected_layout_wins_over_the_merely_enabled_ones(monkeypatch):
    # Every keyboard test so far fed AppleSelectedInputSources only, so the loop
    # order — "Selected first: enabled can hold several, and only one of them is in
    # use" — was never exercised. This host really does have three enabled and two
    # selected, so swapping the two keys silently hands the guest U.S.
    import plistlib

    us = {
        "InputSourceKind": "Keyboard Layout",
        "KeyboardLayout ID": 0,
        "KeyboardLayout Name": "U.S.",
    }
    exported = plistlib.dumps(
        {"AppleEnabledInputSources": [us, _SWISS], "AppleSelectedInputSources": [_SWISS]},
        fmt=plistlib.FMT_XML,
    ).decode()
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=exported),
    )
    assert bib.host_keyboard_layout() == (19, "Swiss German")


def test_the_enabled_list_is_used_when_nothing_is_selected(monkeypatch):
    import plistlib

    exported = plistlib.dumps(
        {"AppleEnabledInputSources": [_SWISS], "AppleSelectedInputSources": []},
        fmt=plistlib.FMT_XML,
    ).decode()
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=exported),
    )
    assert bib.host_keyboard_layout() == (19, "Swiss German")


def test_a_secret_written_over_a_loose_file_gets_the_tight_mode(tmp_path):
    # O_CREAT|O_TRUNC applies the mode only when it creates the file, so the unlink
    # is the only thing that makes the docstring true for a path that exists. sshd
    # refuses to start with a group-readable host key, so the guest would silently
    # become unreachable and the documented retry would not repair it.
    target = tmp_path / "ssh_host_ed25519_key"
    target.write_bytes(b"OLD\n")
    target.chmod(0o644)
    bibpatch.write_private(target, b"NEWKEY\n")
    assert target.read_bytes() == b"NEWKEY\n"
    assert target.stat().st_mode & 0o777 == 0o600


# --- round 19 ------------------------------------------------------------------


def test_owning_the_home_never_descends_through_a_link(tmp_path, monkeypatch):
    # The old assertion pinned the chown's follow_symlinks flag, which is only half
    # the guard: os.walk(followlinks=True) descends *through* a symlinked directory
    # and lchowns whatever is inside it — on the host. Both mutations have to fail.
    chowned: list[str] = []
    monkeypatch.setattr(bibpatch.os, "chown", lambda p, u, g, **kw: chowned.append(str(p)))
    outside = tmp_path / "host"
    (outside / "deeper").mkdir(parents=True)
    (outside / "deeper" / "sudoers").write_text("root ALL")
    home = tmp_path / "volume" / "Users" / "admin"
    home.mkdir(parents=True)
    (home / "real").write_text("guest file")
    (home / "escape").symlink_to(outside)
    bibpatch.own_home(tmp_path / "volume", bibpatch.Account("admin", "pw"))
    assert str(home / "real") in chowned
    # The link itself is chowned (harmlessly, as a link). What must never happen is
    # the walk stepping through it.
    assert not any(str(outside) in path for path in chowned), (
        "the walk descended into a directory the guest pointed at"
    )
    assert not any("sudoers" in path for path in chowned)


def test_the_templates_keyboard_provisioner_keeps_the_two_things_that_make_it_work():
    # The offline copy of this logic is pinned by three tests; the packer copy was
    # only grepped for a variable name. Rounds 8/9 and 14/15 were both keyboard
    # regressions.
    template = (Path(bib.__file__).resolve().parent / "packer" / "browser-vm.pkr.hcl").read_text()
    line = next(ln for ln in template.splitlines() if "AppleEnabledInputSources" in ln)
    # An integer, not a string: `defaults write` stores it as a string and HIToolbox
    # then ignores the entry, which is why PlistBuddy is used at all.
    assert "'KeyboardLayout ID' integer" in line
    assert "'KeyboardLayout ID' string" not in line
    # Both lists: writing only the enabled one leaves the layout inactive.
    assert "AppleEnabledInputSources AppleSelectedInputSources" in line


def test_the_clipboard_agent_is_installed_as_a_launch_agent_not_a_daemon():
    # The path was asserted against itself, so it could move to LaunchDaemons and
    # everything still reported success — while launchd loads it as a root daemon,
    # which has no pasteboard, so copy-paste is dead.
    assert f"/Library/LaunchAgents/{bib.AGENT_LABEL}.plist" == bib.AGENT_PLIST_PATH
    assert "/Library/LaunchDaemons" not in bib.guest_install_script("pw")


def test_the_keepalive_refreshes_faster_than_sudo_forgets():
    # sudo's timestamp_timeout is five minutes by default and the build takes thirty
    # to sixty; any interval above that makes the thread do nothing useful, and the
    # last step of every unattended build is refused.
    assert bib.SudoKeepalive().interval <= 120


def test_the_image_is_pulled_before_the_container_is_removed(monkeypatch):
    # A guard has to run before the thing it guards: the rm used to happen first, so
    # a pull that could not succeed destroyed a working container and then reported
    # only the pull.
    order: list[str] = []
    monkeypatch.setattr(bib, "container_running", lambda *a, **k: False)
    monkeypatch.setattr(
        bib,
        "ensure_image",
        lambda e, c: order.append("pull") or (_ for _ in ()).throw(bib.Failure("could not pull")),
    )
    monkeypatch.setattr(
        bib, "run", lambda e, *a, **k: order.append(a[0]) or subprocess.CompletedProcess([], 0)
    )
    with pytest.raises(bib.Failure, match="could not pull"):
        bib.cmd_up("podman", bib.Config())
    assert "rm" not in order, "a working container was removed for a pull that failed"


def test_the_guest_sets_its_own_time_zone_with_its_own_tool(credentials, monkeypatch):
    # Patching /etc/localtime from the host cannot work: on a real Data volume both
    # it and the zoneinfo directory are symlinks into paths that resolve against the
    # host, so the patcher refused them and aborted the whole patch.
    bib.guest_password(create=True)
    monkeypatch.setattr(bib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(bib, "host_time_zone", lambda: ("Europe/Rome", "Rome"))
    seen = {}
    monkeypatch.setattr(
        bib, "guest_ssh", lambda vm, ip, script=None: seen.update(script=script) or 0
    )
    bib.cmd_vm_setup("tart", bib.VmConfig())
    assert "sudo_pw systemsetup -settimezone Europe/Rome" in seen["script"]


def test_a_guest_script_without_a_time_zone_still_runs(tmp_path):
    # The step has to be a no-op, not an empty line that `sh -e` chokes on.
    result = _run_guest_script(bib.guest_install_script("pw"), tmp_path, share_exists=True)
    assert result.returncode == 0, result.stderr


def test_the_time_zone_step_runs_in_the_guest(tmp_path):
    script = bib.guest_install_script("pw", "Europe/Rome")
    # Not "fakebin": that is the harness's own directory, and writing into it
    # means the two overwrite each other.
    bin_dir = tmp_path / "extra"
    bin_dir.mkdir(parents=True)
    (bin_dir / "systemsetup").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/tz.log\n')
    (bin_dir / "systemsetup").chmod(0o755)
    result = _run_guest_script(script, tmp_path, share_exists=True, extra_bin=bin_dir)
    assert result.returncode == 0, result.stderr
    assert "-settimezone Europe/Rome" in (tmp_path / "tz.log").read_text()


def test_a_time_zone_the_guest_rejects_does_not_fail_the_install(tmp_path):
    # A wrong clock is an annoyance; a failed `vm setup` costs Chrome and the
    # clipboard agent. Under `sh -e` the step has to swallow its own failure.
    script = bib.guest_install_script("pw", "Mars/Olympus")
    # Not "fakebin": that is the harness's own directory, and writing into it
    # means the two overwrite each other.
    bin_dir = tmp_path / "extra"
    bin_dir.mkdir(parents=True)
    (bin_dir / "systemsetup").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "systemsetup").chmod(0o755)
    result = _run_guest_script(script, tmp_path, share_exists=True, extra_bin=bin_dir)
    assert result.returncode == 0, result.stderr
    assert "could not set the time zone" in result.stderr


@pytest.mark.parametrize("command", ["ssh", "setup"])
def test_the_repair_advice_names_the_step_that_makes_it_possible(credentials, monkeypatch, command):
    # Both messages can only be printed while the guest is up, and `bib vm prepare`
    # refuses while it is up. Naming prepare alone sent the user to a second error.
    bib.guest_password(create=True)
    monkeypatch.setattr(bib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(bib, "guest_ssh", lambda *a, **k: 255)
    action = bib.cmd_vm_ssh if command == "ssh" else bib.cmd_vm_setup
    with pytest.raises(bib.Failure) as caught:
        action("tart", bib.VmConfig())
    message = str(caught.value)
    assert "bib vm prepare" in message
    assert "bib vm down" in message, "prepare refuses while the guest is running"
    assert message.index("bib vm down") < message.index("bib vm prepare")


def test_the_sudo_message_says_the_credential_is_per_terminal():
    # sudo remembers per tty. "Run 'sudo -v', then re-run" is only true from the
    # same window, and a process with no tty can never satisfy the check at all —
    # which is exactly how this was found, from a tool that has none.
    assert "SAME TERMINAL" in bib.SUDO_MESSAGE
    assert "per tty" in bib.SUDO_MESSAGE


def _say(kwargs, text):
    """Write what tart would have said into the log it was handed.

    Into the file, not a pipe: boot_once holds this child for the whole first boot,
    and a pipe nobody drains stops tart dead as soon as its buffer fills.
    """
    kwargs["stdout"].write(text)
    kwargs["stdout"].flush()


def test_a_boot_blocked_by_the_installers_lock_is_retried(monkeypatch):
    # `tart create` returns before the Virtualization framework lets go of the VM's
    # auxiliary storage, so a boot started straight afterwards fails with EAGAIN.
    # Nothing holds it a moment later: it is a handover, not a conflict.
    attempts = []

    class _Locked(_FakeBoot):
        returncode = 1

        def poll(self):
            return 1

    def spawn(*a, **k):
        attempts.append(1)
        if len(attempts) >= 3:
            return _FakeBoot()
        _say(k, 'VZErrorDomain Code=2 "Failed to lock auxiliary storage."')
        return _Locked()

    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    monkeypatch.setattr(bib.subprocess, "Popen", spawn)
    assert bib.boot_once("tart", bib.VmConfig()).poll() is None
    assert len(attempts) == 3


def test_the_first_boot_makes_no_sound(monkeypatch):
    # Setup Assistant starts VoiceOver by itself when nothing types at it, and this
    # boot is deliberately unattended and has no window to stop it in. tart passes
    # the guest's audio to the host whatever --no-graphics says, so the build spent
    # three minutes talking out loud at whoever started it.
    seen = {}

    def spawn(*a, **k):
        seen["argv"] = a[0]
        return _FakeBoot()

    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    monkeypatch.setattr(bib.subprocess, "Popen", spawn)
    bib.boot_once("tart", bib.VmConfig())
    assert "--no-audio" in seen["argv"]


def test_the_first_boot_is_not_held_open_by_a_pipe(monkeypatch):
    # The child this returns is held for BIB_VM_FIRSTBOOT_SECS, three minutes by
    # default. With stderr on a pipe and nobody reading it, tart blocked once the
    # buffer filled and never saw `tart stop` — a ~40-minute build that ended in
    # "did not shut down in time". start_detached had the same bug and was fixed;
    # this one was missed.
    seen = {}

    def spawn(*a, **k):
        seen.update(k)
        _say(k, "into the log, not a pipe")
        return _FakeBoot()

    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    monkeypatch.setattr(bib.subprocess, "Popen", spawn)
    bib.boot_once("tart", bib.VmConfig())
    assert seen["stderr"] is bib.subprocess.STDOUT
    assert seen["stdout"] is not bib.subprocess.PIPE
    # Written through the handle tart was given, and read back from the path: the
    # two are the same file, which is the whole claim.
    assert "into the log, not a pipe" in bib.BOOT_LOG.read_text()


def test_a_boot_that_failed_for_another_reason_is_not_retried(monkeypatch):
    # A guest that never booted has no first-boot state; patching it produces
    # something that reports "Built." and cannot be logged in to.
    attempts = []

    class _Broken(_FakeBoot):
        returncode = 2

        def poll(self):
            return 2

    def spawn(*a, **k):
        attempts.append(1)
        _say(k, "no such vm")
        return _Broken()

    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    monkeypatch.setattr(bib.subprocess, "Popen", spawn)
    with pytest.raises(bib.Failure, match="no such vm"):
        bib.boot_once("tart", bib.VmConfig())
    assert len(attempts) == 1, "only the lock error is transient"


def test_a_lock_that_never_clears_is_reported(monkeypatch):
    class _Locked(_FakeBoot):
        returncode = 1

        def poll(self):
            return 1

    def spawn(*a, **k):
        _say(k, "Failed to lock auxiliary storage.")
        return _Locked()

    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    monkeypatch.setattr(bib.subprocess, "Popen", spawn)
    with pytest.raises(bib.Failure, match="still locked after"):
        bib.boot_once("tart", bib.VmConfig())


def test_the_data_volume_is_found_in_the_second_container_too(monkeypatch):
    # A macOS disk carries more than one APFS container. The first one on the
    # device holds iSCPreboot, xART, Hardware and Recovery; the guest's own volumes
    # are in the next. Stopping at the first cost a whole real build.
    import plistlib

    listing = plistlib.dumps(
        {
            "Containers": [
                {
                    "PhysicalStores": [{"DeviceIdentifier": "disk4s1"}],
                    "Volumes": [
                        {"DeviceIdentifier": "disk4s1", "Name": "iSCPreboot"},
                        {"DeviceIdentifier": "disk4s2", "Name": "xART"},
                        {"DeviceIdentifier": "disk4s3", "Name": "Hardware"},
                        {"DeviceIdentifier": "disk4s4", "Name": "Recovery"},
                    ],
                },
                {
                    "PhysicalStores": [{"DeviceIdentifier": "disk4s2"}],
                    "Volumes": [
                        {
                            "DeviceIdentifier": "disk5s1",
                            "Name": "Macintosh HD",
                            "Roles": ["System"],
                        },
                        {"DeviceIdentifier": "disk5s2", "Name": "Data", "Roles": ["Data"]},
                    ],
                },
            ]
        }
    )
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    assert bibpatch.data_volume("/dev/disk4") == "/dev/disk5s2"


def test_a_disk_whose_containers_hold_no_data_volume_names_them_all(monkeypatch):
    import plistlib

    listing = plistlib.dumps(
        {
            "Containers": [
                {
                    "PhysicalStores": [{"DeviceIdentifier": "disk4s1"}],
                    "Volumes": [{"DeviceIdentifier": "disk4s1", "Name": "iSCPreboot"}],
                },
                {
                    "PhysicalStores": [{"DeviceIdentifier": "disk4s2"}],
                    "Volumes": [{"DeviceIdentifier": "disk5s1", "Name": "Recovery"}],
                },
            ]
        }
    )
    monkeypatch.setattr(
        bibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    with pytest.raises(bibpatch.PatchError, match="iSCPreboot, Recovery"):
        bibpatch.data_volume("/dev/disk4")


def test_the_suite_cannot_reach_the_real_secrets():
    # Running pytest once deleted a live VM's password and both key pairs. The
    # autouse fixture is what stops that; this is what stops the fixture being
    # dropped.
    real = Path(os.path.expanduser("~")) / ".config" / "browser-in-a-box"
    for path in (bib.SECRETS, bib.CREDENTIALS, bib.VM_KEY, bib.VM_HOST_KEY, bib.KNOWN_HOSTS):
        assert real not in path.parents and path != real, f"{path} is the user's own"
    assert bib.Path.home() != Path(os.path.expanduser("~")), "Path.home() is not redirected"


def test_chrome_is_pointed_at_the_share_rather_than_moving_downloads(tmp_path):
    # macOS protects ~/Downloads against being renamed, and a process arriving over
    # ssh has no TCC grant for it, so `mv` there fails with EPERM no matter what the
    # permissions say. Proven on a real guest.
    import json as _json

    (tmp_path / "Downloads").mkdir()
    result = _run_guest_script(bib.guest_install_script("pw"), tmp_path, share_exists=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "Downloads").is_dir(), "the guest's own Downloads is left alone"
    assert not (tmp_path / "Downloads.local").exists(), "nothing is moved aside any more"
    prefs = tmp_path / "Library/Application Support/Google/Chrome/Default/Preferences"
    written = _json.loads(prefs.read_text())
    # The harness rewrites the share to a directory it owns, so the assertion is
    # that Chrome is pointed at the share — wherever the share is.
    assert written["download"]["default_directory"] == str(tmp_path / "share")
    assert written["download"]["prompt_for_download"] is False


def test_the_share_is_reachable_from_inside_downloads(tmp_path):
    (tmp_path / "Downloads").mkdir()
    _run_guest_script(bib.guest_install_script("pw"), tmp_path, share_exists=True)
    link = tmp_path / "Downloads" / "on-the-host"
    assert link.is_symlink()
    assert str(link.readlink()) == str(tmp_path / "share")


def test_an_existing_chrome_profile_is_not_overwritten(tmp_path):
    # Rewriting Preferences would throw away every setting the user has made.
    (tmp_path / "Downloads").mkdir()
    prefs = tmp_path / "Library/Application Support/Google/Chrome/Default/Preferences"
    prefs.parent.mkdir(parents=True)
    prefs.write_text('{"mine": true}')
    result = _run_guest_script(bib.guest_install_script("pw"), tmp_path, share_exists=True)
    assert result.returncode == 0, result.stderr
    assert prefs.read_text() == '{"mine": true}'
    assert "already has a profile" in result.stderr


def test_the_launcher_says_something_while_it_waits_and_when_it_fails(credentials, monkeypatch):
    # `do shell script` is silent in both directions. A cold start waits tens of
    # seconds with nothing on screen, which looks like the icon did nothing and
    # gets clicked again; and when it fails, the reason goes to a shell nobody is
    # watching. -128 is "user cancelled", which is not worth a dialog.
    written = {}
    monkeypatch.setattr(
        bib,
        "run",
        lambda *a, **k: written.update(script=a[-1]) or subprocess.CompletedProcess(a, 0),
    )
    monkeypatch.setattr(bib, "draw_icon", lambda *a, **k: None)
    monkeypatch.setattr(bib, "guest_answers", lambda *a, **k: False)
    bib.cmd_vm_icon("tart", bib.VmConfig())
    script = written["script"]
    assert "progress total steps to -1" in script
    assert "display alert" in script
    assert "-128" in script


def test_the_setup_assistant_does_not_come_back_after_a_hard_stop():
    # The disk patch drops AccountInfo, which relaunches the whole assistant. macOS
    # also keeps a per-step mark in the user's own preferences, so a guest stopped
    # mid-sign-in came back to the iCloud screen on every boot.
    script = bib.guest_install_script("pw")
    assert "com.apple.SetupAssistant DidSeeCloudSetup -bool true" in script
    assert "LastSeenCloudProductVersion" in script


def test_the_launcher_is_named_for_what_the_guest_actually_holds(credentials, monkeypatch):
    # `BIB_BROWSER=all bib vm create` then a plain `bib vm icon`: the environment is
    # back to its default by then, so the launcher for a guest with three browsers
    # in it was called "Google Chrome in a Box". What a VM holds is a property of
    # that VM, not of whichever shell asks about it later.
    monkeypatch.setattr(bib, "host_time_zone", lambda: ("Europe/Zurich", "Zurich"))
    monkeypatch.setattr(bib, "guest_ssh", lambda vm, ip, script: 0)
    assert bib.install_browsers(bib.VmConfig(browser=bibbrowsers.ALL), "10.0.0.9", "pw") == 0
    assert bib.read_state()["browser"] == bibbrowsers.ALL

    monkeypatch.delenv("BIB_BROWSER", raising=False)
    # The guest cannot be reached here, so this is the remembered answer.
    monkeypatch.setattr(bib, "guest_answers", lambda *a, **k: False)
    assert bib.installed_browser(bib.VmConfig()).key == bibbrowsers.ALL
    # And a guest that has never been built still follows the request.
    bib.STATE.unlink()
    monkeypatch.setenv("BIB_BROWSER", "firefox")
    assert bib.installed_browser(bib.VmConfig()).key == "firefox"


def test_the_launcher_believes_the_guest_over_anything_written_down(credentials, monkeypatch):
    # The disk is the only answer that stays right when a browser is added or
    # removed by hand, so it is asked first and the written-down choice is only the
    # fallback for a guest that is switched off.
    bib.write_state(last_ip="10.0.0.9", browser="chrome")
    monkeypatch.setattr(bib, "guest_answers", lambda *a, **k: True)

    def answer(argv, **kwargs):
        found = "firefox\nchromium\n"
        return subprocess.CompletedProcess(argv, 0, stdout=found, stderr="")

    monkeypatch.setattr(bib.subprocess, "run", answer)
    # Two on the disk is `all`, whatever the state file says was asked for.
    assert bib.installed_browser(bib.VmConfig()).key == bibbrowsers.ALL

    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(argv, 0, stdout="firefox\n", stderr=""),
    )
    assert bib.installed_browser(bib.VmConfig()).key == "firefox"


def test_a_compiled_build_looks_for_neither_a_script_nor_an_interpreter(
    credentials, monkeypatch, tmp_path
):
    # The pre-flight runs before the multi-gigabyte download, and it went on
    # demanding both after the patch step had stopped using either. Every installed
    # 3.2.0 failed at the first step with "the patcher is missing", which is a
    # sentence a compiled build should never be able to say.
    monkeypatch.setattr(bib, "COMPILED", True)
    monkeypatch.setattr(
        bib, "find_patcher", lambda: pytest.fail("no script is looked for when compiled")
    )
    monkeypatch.setattr(
        bib, "find_guest_python", lambda: pytest.fail("no interpreter is looked for either")
    )
    monkeypatch.setattr(bib, "sudo_is_cached", lambda: False)
    # Stops at the sudo check, which is the step straight after the pre-flight.
    with pytest.raises(bib.Failure, match="Nothing has been downloaded yet"):
        bib._create_offline("tart", bib.VmConfig())


def test_a_checkout_still_checks_both_before_downloading(credentials, monkeypatch):
    # And from a checkout it must still say so early: finding out at the end costs
    # the whole build.
    monkeypatch.setattr(bib, "COMPILED", False)
    monkeypatch.setattr(bib, "find_patcher", lambda: (_ for _ in ()).throw(bib.Failure("missing")))
    with pytest.raises(bib.Failure, match="missing"):
        bib._create_offline("tart", bib.VmConfig())


def test_a_compiled_build_re_executes_itself_instead_of_spawning_an_interpreter(
    credentials, monkeypatch, tmp_path
):
    # The whole point of shipping a compiled build is not needing an interpreter.
    # The patch step runs as root and a process cannot elevate itself, so it was
    # spawning `sudo <python> bibpatch.py` — one dependency, on the one step where
    # failing costs the entire build. Compiled, it calls itself back instead.
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(bib, "COMPILED", True)
    # argv[0], because sys.executable is not the binary: Nuitka sets it to `python`
    # beside the binary, so invoking /opt/homebrew/bin/bib produced
    # /opt/homebrew/bin/python — a file that does not exist, handed to sudo, twice,
    # after twenty-minute builds.
    binary = tmp_path / "bib"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(bib.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(bib.sys, "argv", [str(binary), "vm", "prepare"])
    monkeypatch.setattr(bib, "sudo_is_cached", lambda: True)
    monkeypatch.setattr(bib, "host_keyboard_layout", lambda: (5, "Swiss German"))
    monkeypatch.setattr(
        bib, "find_guest_python", lambda: pytest.fail("no interpreter may be looked for")
    )
    seen = {}
    monkeypatch.setattr(
        bib.subprocess,
        "run",
        lambda cmd, **kw: seen.update(argv=cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    bib._prepare_guest(bib.VmConfig(), "pw")
    argv = seen["argv"]
    assert argv[:2] == ["/usr/bin/sudo", "-n"]
    assert argv[2:4] == [str(binary.resolve()), bib.PATCH_ENTRY]
    assert not any("python" in str(a) for a in argv), "no interpreter may appear"
    assert not any(str(a).endswith("bibpatch.py") for a in argv)


def test_the_binary_is_found_through_a_symlink_and_checked(monkeypatch, tmp_path):
    # Homebrew installs bin/bib as a symlink into the Cellar, which is how it is
    # invoked; the target is what must be re-run. And a path that cannot be
    # executed is said here rather than by sudo at the end of a long build.
    real = tmp_path / "cellar" / "bib"
    real.parent.mkdir()
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    link = tmp_path / "bin" / "bib"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(bib.sys, "argv", [str(link)])
    assert bib.own_binary() == real.resolve()

    # Bare name on PATH, the way a shell hands it over.
    monkeypatch.setattr(bib.sys, "argv", ["bib"])
    monkeypatch.setattr(bib.shutil, "which", lambda name: str(link))
    assert bib.own_binary() == real.resolve()

    monkeypatch.setattr(bib.sys, "argv", [str(tmp_path / "gone")])
    monkeypatch.setattr(bib.shutil, "which", lambda name: None)
    with pytest.raises(bib.Failure, match="is not executable"):
        bib.own_binary()


def test_the_hidden_entry_runs_the_patcher_and_nothing_else(monkeypatch):
    # It takes bibpatch's arguments, not bib's, and must not reach bib's parser —
    # which would reject them and exit 2 under sudo, as root, with no explanation.
    called = {}
    monkeypatch.setattr(bibpatch, "main", lambda argv: called.update(argv=argv) or 0)
    assert bib.main([bib.PATCH_ENTRY, "--disk", "/x", "--user", "admin"]) == 0
    assert called["argv"] == ["--disk", "/x", "--user", "admin"]
    # And it is not advertised: a hidden re-entry in the help is an invitation.
    assert bib.PATCH_ENTRY not in bib.build_parser().format_help()


def test_the_patcher_interpreter_is_proven_to_run_before_sudo_gets_it(monkeypatch, tmp_path):
    # Nuitka makes bib itself interpreter-free, but the patch step runs as root and
    # a running process cannot elevate itself, so that one step spawns
    # `sudo <python> bibpatch.py`. It used to take sys.executable whenever the name
    # started with "python" — on a Homebrew install that was
    # /opt/homebrew/bin/python, which does not exist, and it went to sudo as it was:
    # "sudo: /opt/homebrew/bin/python: command not found" after a 20-minute build.
    missing = tmp_path / "python"
    monkeypatch.setattr(bib.sys, "executable", str(missing))
    monkeypatch.setattr(bib.shutil, "which", lambda name: None)
    assert bib.find_guest_python() == "/usr/bin/python3"

    # And one that is there but cannot run — /usr/bin/python3 without the Command
    # Line Tools is exactly this — is not chosen either.
    monkeypatch.setattr(
        bib.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "")
    )
    with pytest.raises(bib.Failure, match="no working python3"):
        bib.find_guest_python()


def test_ssh_into_the_guest_is_key_only(tmp_path):
    # The account password is `admin` and BIB_VM_NET is bridged, so the guest has
    # its own address on the house network. macOS sshd offers publickey, password
    # and keyboard-interactive out of the box: turning Remote Login on without
    # closing the other two handed that network an admin/admin login.
    root = tmp_path / "guest"
    (root / "private/var/db/com.apple.xpc.launchd").mkdir(parents=True)
    bibpatch.enable_remote_login(root)
    conf = root / "private/etc/ssh/sshd_config.d/bib.conf"
    assert conf.exists(), "the disk patch must close it for a guest built from scratch"
    for line in ("PasswordAuthentication no", "KbdInteractiveAuthentication no"):
        assert line in conf.read_text()
    # And again over ssh, so a guest built before this existed is closed by setup
    # rather than left open.
    script = bib.guest_install_script("pw")
    assert "/etc/ssh/sshd_config.d/bib.conf" in script
    assert "PasswordAuthentication no" in script


def test_a_browser_added_later_lands_on_a_guest_that_already_exists():
    # The point of `bib vm setup` on an existing VM. A version that adds a browser —
    # Vivaldi in 3.3.0 — reaches a guest built before it without a rebuild, because
    # `all` covers the new one and each script decides for itself whether there is
    # anything to do.
    assert [b.key for b in bibbrowsers.expand(bibbrowsers.ALL)] == [
        "chrome",
        "firefox",
        "vivaldi",
        "chromium",
    ]
    for browser in bibbrowsers.expand(bibbrowsers.ALL):
        script = bib.guest_install_script("pw", browser=browser)
        # On the binary, not the bundle, and per browser: that test is what makes
        # re-running cheap for the three already there and real for the new one.
        assert f"if [ -x {shlex.quote(browser.binary)} ]" in script
        assert f"echo '{browser.label} is already installed'" in script


def test_the_smoke_matrix_covers_every_browser_there_is():
    """A row added to the table must reach CI, or nothing ever starts it.

    Written out in YAML rather than derived, because a workflow cannot import the
    table — so this is what keeps the two in step. Vivaldi was added and the matrix
    was not, and the full run went green without ever starting it.
    """
    import json

    workflow = (Path(bib.__file__).resolve().parent / ".github/workflows/ci.yml").read_text()
    listed = re.search(r"(\[\"chrome\"[^\]]*\])", workflow)
    assert listed, "the smoke matrix's full browser list is not where this expects it"
    assert set(json.loads(listed.group(1))) == {
        browser.key for browser in bibbrowsers.expand(bibbrowsers.ALL)
    }


def test_vivaldi_is_downloaded_by_looking_its_version_up_first():
    # Vivaldi publishes no unversioned download, so the URL cannot be written down.
    # The version comes out of the Sparkle feed its own updater reads.
    script = bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS["vivaldi"])
    assert "appcast.xml" in script
    assert "sparkle:version" in script
    assert "$BIB_REVISION" in script
    assert "Vivaldi.$BIB_REVISION.universal.dmg" in script
    # A dmg, unlike Chromium, which is the only other one needing a lookup.
    assert "hdiutil attach" in script


def test_the_clipboard_agent_is_installed_before_the_browser():
    # Paste is what carries the generated password and the Apple Account details
    # into the guest, and the browser downloads are hundreds of megabytes — three
    # times over under `all`. Installed after them, the clipboard came up at the end
    # of a ten-minute wait, which is the whole window in which it was needed.
    script = bib.guest_install_script("pw")
    assert script.index("tart-guest-agent/releases") < script.index("googlechrome.dmg")
    assert script.index("launchctl bootstrap") < script.index("googlechrome.dmg")
    # The screen lock goes with it, and for the same reason: set after the browsers,
    # the guest could lock during the download — and the one password you cannot
    # paste, because the agent is not up either, is the one it then asks for.
    assert script.index("askForPassword") < script.index("googlechrome.dmg")


def test_a_firefox_profile_from_an_older_version_is_not_orphaned(tmp_path):
    # The profile directory carries this project's name, so the rename moved it:
    # a guest set up by 2.x has Profiles/cib.default-release and the guard, which
    # tests the new path, did not fire. profiles.ini was then rewritten to point at
    # a fresh empty profile and every saved login became unreachable, with the real
    # profile still sitting on disk.
    (tmp_path / "Downloads").mkdir()
    support = tmp_path / "Library/Application Support/Firefox"
    old = support / "Profiles/cib.default-release"
    old.mkdir(parents=True)
    (old / "logins.json").write_text('{"logins": ["mine"]}')
    ini = support / "profiles.ini"
    ini.write_text(
        "[Profile0]\nName=default-release\nIsRelative=1\nPath=Profiles/cib.default-release\n"
    )
    result = _run_guest_script(
        bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS["firefox"]),
        tmp_path,
        share_exists=True,
        browser=bibbrowsers.BROWSERS["firefox"],
    )
    assert result.returncode == 0, result.stderr
    assert "already has a profile" in result.stderr
    assert "cib.default-release" in ini.read_text(), "profiles.ini was repointed"
    assert (old / "logins.json").exists()


def test_the_chromium_preferences_are_valid_json_and_send_nothing_home():
    # Against bibbrowsers, which is what a guest actually gets. This used to assert
    # against bib.FIRST_RUN_PREFS and bib.LOCAL_STATE_PREFS, two constants left
    # behind by the per-browser split that nothing read any more — so every setting
    # below could have been turned back on with the whole suite still green.
    import json as _json

    written = _json.loads(bibbrowsers.chromium_preferences(bibbrowsers.GUEST_SHARE))
    assert written["download"]["default_directory"] == bibbrowsers.GUEST_SHARE
    # Read once, before the first launch, so anything malformed is silently
    # discarded and every setting here is quietly lost.
    assert written["safebrowsing"] == {"enabled": False, "enhanced": False}
    assert written["search"]["suggest_enabled"] is False
    assert written["alternate_error_pages"]["enabled"] is False
    assert written["spellcheck"]["use_spelling_service"] is False
    assert written["net"]["network_prediction_options"] == 2
    assert written["credentials_enable_service"] is False
    assert written["profile"]["password_manager_leak_detection"] is False
    state = _json.loads(bibbrowsers.CHROMIUM_LOCAL_STATE)
    # Not in Preferences: metrics consent lives beside the profiles, not inside one,
    # so putting it in the profile would look right and do nothing.
    assert state["user_experience_metrics"]["reporting_enabled"] is False


def test_no_browser_is_made_the_default_unless_asked(monkeypatch):
    # Handing the guest's default browser to whatever bib installed is a decision
    # about someone else's machine, and under `all` it would be whichever browser
    # the loop reached first. Safari stays, which is what a fresh macOS does.
    for key in ("chrome", "firefox", "chromium"):
        script = bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS[key])
        assert "--make-default-browser" not in script
        assert "--setDefaultBrowser" not in script
    monkeypatch.setenv("BIB_VM_DEFAULT_BROWSER", "1")
    asked = bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS["chrome"])
    assert "--make-default-browser" in asked


def test_the_first_run_experience_is_skipped_for_every_browser():
    # Every one of these is a click between opening the box and using it, and the
    # welcome tab and sign-in pitch both talk to the vendor before you have typed.
    chrome = bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS["chrome"])
    # The sentinel, not just the preference: the first-run decision is taken before
    # Preferences is read, so without the file the tour runs whatever it says.
    assert bibbrowsers.CHROMIUM_FIRST_RUN_SENTINEL in chrome
    prefs = json.loads(bibbrowsers.chromium_preferences("/x"))
    assert prefs["distribution"]["skip_first_run_ui"] is True
    assert prefs["distribution"]["suppress_first_run_default_browser_prompt"] is True
    assert prefs["browser"]["check_default_browser"] is False
    assert json.loads(bibbrowsers.CHROMIUM_LOCAL_STATE)["browser"]["first_run_finished"] is True

    firefox = dict(
        line.removeprefix("user_pref(").removesuffix(");").split(", ", 1)
        for line in bibbrowsers.firefox_preferences("/x").splitlines()
    )
    assert firefox['"browser.aboutwelcome.enabled"'] == "false"
    assert firefox['"browser.startup.homepage_override.mstone"'] == '"ignore"'
    # The privacy notice on the very first launch, which is its own dialog.
    assert firefox['"datareporting.policy.dataSubmissionPolicyBypassNotification"'] == "true"
    # Studies are code Mozilla ships to a subset of users; the coverage ping is a
    # separate report from telemetry with its own opt-out.
    assert firefox['"app.shield.optoutstudies.enabled"'] == "false"
    assert firefox['"toolkit.coverage.opt-out"'] == "true"
    assert firefox['"browser.newtabpage.activity-stream.showSponsored"'] == "false"


def test_the_firefox_preferences_send_nothing_home_either():
    # Firefox's half had no assertion at all, so its telemetry could have been left
    # on without anything noticing.
    written = bibbrowsers.firefox_preferences("/somewhere")
    lines = dict(
        line.removeprefix("user_pref(").removesuffix(");").split(", ", 1)
        for line in written.splitlines()
    )
    assert lines['"browser.download.dir"'] == '"/somewhere"'
    # 2 is "use the folder named above"; without it Firefox ignores the path.
    assert lines['"browser.download.folderList"'] == "2"
    for key in (
        "datareporting.healthreport.uploadEnabled",
        "datareporting.policy.dataSubmissionEnabled",
        "toolkit.telemetry.enabled",
        "toolkit.telemetry.unified",
        "browser.newtabpage.activity-stream.feeds.telemetry",
        "browser.ping-centre.telemetry",
        "browser.search.suggest.enabled",
        "network.prefetch-next",
    ):
        assert lines[f'"{key}"'] == "false", key
    assert lines['"network.dns.disablePrefetch"'] == "true"


@pytest.mark.parametrize(
    "tool", ["python3", "python", "git", "make", "gcc", "clang", "cc", "svn", "jq"]
)
def test_the_guest_script_needs_nothing_the_guest_does_not_have(tool):
    # A fresh macOS has no Command Line Tools. Any of these is a stub that opens
    # "The <tool> command requires the command line developer tools" — a dialog on
    # the guest's screen, waiting for a click, from a command that is supposed to
    # need none. Seen for real, from a diagnostic that used python3 in the guest.
    script = bib.guest_install_script("pw", "Europe/Rome")
    body = "\n".join(ln for ln in script.split("\n") if not ln.lstrip().startswith("#"))
    assert not re.search(rf"(^|[\s|;&(]){re.escape(tool)}([\s;&)]|$)", body), (
        f"{tool} is not on a bare macOS; using it turns 'bib vm setup' into a dialog"
    )


def test_the_guest_never_locks_its_screen(tmp_path):
    # A lock screen asks for the generated 24-character password, and a VM has no
    # Touch ID to shortcut it — so the one thing the password exists to avoid.
    (tmp_path / "Downloads").mkdir()
    # Not "fakebin": that is the harness's own directory, and writing into it
    # means the two overwrite each other.
    bin_dir = tmp_path / "extra"
    bin_dir.mkdir(parents=True)
    for tool in ("defaults", "pmset", "sysadminctl"):
        (bin_dir / tool).write_text(f'#!/bin/sh\necho "{tool} $@" >> {tmp_path}/lock.log\n')
        (bin_dir / tool).chmod(0o755)
    result = _run_guest_script(
        bib.guest_install_script("pw"), tmp_path, share_exists=True, extra_bin=bin_dir
    )
    assert result.returncode == 0, result.stderr
    log = (tmp_path / "lock.log").read_text()
    # -currentHost spelled out: idleTime is a ByHost preference, so without it the
    # write lands in a domain nothing reads and the screensaver still starts. An
    # assertion on the tail alone matches either spelling and would not notice.
    assert "defaults -currentHost write com.apple.screensaver idleTime -int 0" in log, (
        "the screensaver would still start"
    )
    assert "defaults -currentHost write com.apple.screensaver askForPassword -int 0" in log, (
        "it would still ask"
    )
    assert "pmset -a displaysleep 0 sleep 0" in log, "the display would still sleep"
    # The one that actually does it on every guest this project can build: macOS 14
    # moved the lock behind sysadminctl, and bib requires 15+ for the Apple Account.
    assert "sysadminctl -screenLock off" in log, "macOS 14+ would still lock the screen"


def test_a_guest_without_sysadminctl_still_finishes(tmp_path):
    # The flag is macOS 14 and later; on an older guest the command is absent, and
    # under `sh -e` an unguarded failure would abort the whole install.
    (tmp_path / "Downloads").mkdir()
    # Not "fakebin": that is the harness's own directory, and writing into it
    # means the two overwrite each other.
    bin_dir = tmp_path / "extra"
    bin_dir.mkdir(parents=True)
    for tool in ("defaults", "pmset"):
        (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / tool).chmod(0o755)
    (bin_dir / "sysadminctl").write_text("#!/bin/sh\nexit 127\n")
    (bin_dir / "sysadminctl").chmod(0o755)
    result = _run_guest_script(
        bib.guest_install_script("pw"), tmp_path, share_exists=True, extra_bin=bin_dir
    )
    assert result.returncode == 0, result.stderr


def test_the_vnc_viewer_is_how_the_window_goes_full_screen(monkeypatch):
    # tart's own window has neither full screen nor scaling; Screen Sharing has
    # both, and the offline patch already turns on the Remote Login it needs.
    monkeypatch.setenv("BIB_VM_VIEWER", "vnc")
    assert "--vnc" in bib.vm_run_args(bib.VmConfig())


def test_the_built_in_window_stays_the_default(monkeypatch):
    assert "--vnc" not in bib.vm_run_args(bib.VmConfig())


def test_an_unknown_viewer_is_refused(monkeypatch):
    monkeypatch.setenv("BIB_VM_VIEWER", "kiosk")
    with pytest.raises(bib.Failure, match="BIB_VM_VIEWER"):
        bib.vm_run_args(bib.VmConfig())


# --- one command instead of three ----------------------------------------------


def test_create_boots_the_guest_and_installs_chrome_itself(
    calls, credentials, monkeypatch, tmp_path
):
    # It used to stop after patching and print three more commands to run. Each of
    # them is a place a build can fail, and each failure meant starting over.
    order: list[str] = []
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(bib.subprocess, "Popen", lambda *a, **k: _FakeBoot())
    monkeypatch.setattr(
        bib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0)
    )
    monkeypatch.setattr(bib, "start_detached", lambda t, vm: order.append("start") or _FakeBoot())
    monkeypatch.setattr(bib, "wait_for_guest", lambda t, vm, b: order.append("wait") or "10.0.0.9")
    monkeypatch.setattr(
        bib, "guest_ssh", lambda vm, ip, script=None: order.append(f"ssh:{ip}") or 0
    )
    bib.cmd_vm_create("tart", bib.VmConfig())
    assert order == ["start", "wait", "ssh:10.0.0.9"]


def test_create_says_what_is_left_when_the_install_fails(calls, credentials, monkeypatch, tmp_path):
    # The guest is built and running by then; telling the user to start over would
    # throw away half an hour for a step that retries on its own.
    monkeypatch.setattr(bib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(bib.subprocess, "Popen", lambda *a, **k: _FakeBoot())
    monkeypatch.setattr(
        bib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0)
    )
    monkeypatch.setattr(bib, "guest_ssh", lambda vm, ip, script=None: 1)
    with pytest.raises(bib.Failure, match="bib vm setup"):
        bib.cmd_vm_create("tart", bib.VmConfig())


def test_a_guest_that_dies_while_starting_is_not_waited_out(isolate_secrets, monkeypatch):
    # Five minutes of polling for an address that will never come.
    class _Died(_FakeBoot):
        returncode = 3

        def poll(self):
            return 3

    # Through the log, not a pipe. tart's stderr is redirected into the boot log
    # now, because a pipe nobody drains blocks tart at 64 KiB and then SIGPIPEs it
    # dead the moment bib exits — which killed the guest this test is about.
    bib.BOOT_LOG.parent.mkdir(parents=True, exist_ok=True)
    bib.BOOT_LOG.write_text("Downloading...\nbridged networking failed\n")
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    with pytest.raises(bib.Failure, match="bridged networking failed"):
        isolate_secrets.wait_for_guest("tart", bib.VmConfig(), _Died())


def test_a_guest_that_never_answers_points_at_setup(isolate_secrets, monkeypatch):
    monkeypatch.setattr(bib.time, "sleep", lambda s: None)
    monkeypatch.setattr(bib, "GUEST_WAIT_SECS", 0)
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    with pytest.raises(bib.Failure, match="bib vm setup"):
        isolate_secrets.wait_for_guest("tart", bib.VmConfig(), _FakeBoot())


def test_no_test_can_start_a_real_vm():
    # start_detached uses subprocess.Popen directly, so a test that replaces only
    # `bib.run` spawned tart for real and then sat in wait_for_guest for five
    # minutes. The autouse fixture is what stops that.
    assert bib.start_detached("tart", bib.VmConfig()).poll() is None
    assert bib.wait_for_guest("tart", bib.VmConfig(), _FakeBoot()) == "192.168.1.50"


def test_the_address_the_guest_last_answered_on_is_remembered(credentials, monkeypatch):
    # The host's arp table forgets a guest that has been quiet, and `tart ip --wait`
    # only re-reads that table — it sends nothing that would repopulate it. Hit
    # twice on a real guest that was pingable the whole time.
    monkeypatch.setattr(
        bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="10.0.0.9\n")
    )
    assert bib.vm_ip("tart", bib.VmConfig()) == "10.0.0.9"
    assert bib.read_state()["last_ip"] == "10.0.0.9"

    monkeypatch.setattr(bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=""))
    monkeypatch.setattr(bib, "guest_answers", lambda ip: ip == "10.0.0.9")
    assert bib.vm_ip("tart", bib.VmConfig()) == "10.0.0.9"


def test_a_remembered_address_that_answers_nothing_is_not_used(credentials, monkeypatch):
    # A guest that has really gone needs the error, not an address that will time
    # out on every command after it.
    bib.write_state(last_ip="10.0.0.9")
    monkeypatch.setattr(bib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=""))
    monkeypatch.setattr(bib, "guest_answers", lambda ip: False)
    with pytest.raises(bib.Failure, match="bib vm status"):
        bib.vm_ip("tart", bib.VmConfig())


def test_the_probe_asks_for_ssh_and_gives_up_quickly(monkeypatch):
    seen = {}

    class _Probe:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def settimeout(self, t):
            seen["timeout"] = t

        def connect_ex(self, addr):
            seen["addr"] = addr
            return 0

    monkeypatch.setattr(bib.socket, "socket", lambda *a, **k: _Probe())
    assert bib.guest_answers("10.0.0.9") is True
    assert seen["addr"] == ("10.0.0.9", 22)
    assert seen["timeout"] <= 5, "a dead guest must not hold the command up"


def test_a_chosen_guest_password_is_used_instead_of_a_generated_one(credentials, monkeypatch):
    monkeypatch.setenv("BIB_VM_PASSWORD", "admin")
    assert bib.guest_password(create=True) == "admin"
    # Saved like any other, so 'bib vm login' and the guest agree after the variable
    # is gone from the shell that built it.
    monkeypatch.delenv("BIB_VM_PASSWORD")
    assert bib.guest_password() == "admin"


def test_a_chosen_password_with_a_shifting_key_is_refused(credentials, monkeypatch):
    # y and z swap between the US and Swiss German layouts, and the packer path types
    # this password in as keystrokes — so it would build a guest nobody can log into.
    monkeypatch.setenv("BIB_VM_PASSWORD", "crazy")
    with pytest.raises(bib.Failure, match="Swiss German"):
        bib.guest_password(create=True)


def test_the_settings_file_fills_in_what_the_environment_does_not(tmp_path, monkeypatch):
    config = tmp_path / "bib.yaml"
    config.write_text(
        "box:\n  port: 7000\n  resolution: 1280x800\nvm:\n  name: work-vm\n  display: 1440x900\n"
    )
    monkeypatch.setenv("BIB_CONFIG", str(config))
    monkeypatch.setattr(bib, "CONFIG", bib.load_config())
    # Sections map onto the two prefixes, so one file configures both variants.
    assert bib.VmConfig().name == "work-vm"
    assert bib.VmConfig().display == "1440x900"
    assert bib.Config().port == 7000


def test_the_environment_wins_over_the_settings_file(tmp_path, monkeypatch):
    """A variable exported for one command has to beat a file you edited once."""
    config = tmp_path / "bib.yaml"
    config.write_text("vm:\n  name: from-file\n")
    monkeypatch.setenv("BIB_CONFIG", str(config))
    monkeypatch.setattr(bib, "CONFIG", bib.load_config())
    monkeypatch.setenv("BIB_VM_NAME", "from-env")
    assert bib.VmConfig().name == "from-env"


def test_a_settings_file_that_is_not_a_mapping_is_refused(tmp_path, monkeypatch):
    config = tmp_path / "bib.yaml"
    config.write_text("- one\n- two\n")
    monkeypatch.setenv("BIB_CONFIG", str(config))
    with pytest.raises(bib.Failure, match="mapping of sections"):
        bib.load_config()


def test_an_unknown_section_is_named_rather_than_ignored(tmp_path, monkeypatch):
    # Silently ignoring it looks exactly like the setting not working, which is the
    # one failure mode a settings file must not have.
    config = tmp_path / "bib.yaml"
    config.write_text("vm:\n  name: ok\ncontainer:\n  port: 1\n")
    monkeypatch.setenv("BIB_CONFIG", str(config))
    with pytest.raises(bib.Failure, match="container"):
        bib.load_config()


def test_a_yaml_boolean_survives_as_the_string_the_rest_of_bib_reads(tmp_path, monkeypatch):
    # yaml turns "yes" into True, and everything downstream compares strings.
    config = tmp_path / "bib.yaml"
    config.write_text("box:\n  force: yes\n")
    monkeypatch.setenv("BIB_CONFIG", str(config))
    settings = bib.load_config()
    assert settings["BIB_FORCE"] == "true"
    # Stopping at the load is what let this pass for a key that did nothing:
    # env_flag read os.environ directly, so the file's value was parsed, stored and
    # never looked at again.
    monkeypatch.setattr(bib, "CONFIG", settings)
    assert bib.env_flag("BIB_FORCE") is True


def test_every_yes_no_setting_can_be_written_in_the_settings_file(tmp_path, monkeypatch):
    # BIB_FORCE and BIB_VM_PACKER are the two, and neither reached its reader.
    config = tmp_path / "bib.yaml"
    config.write_text("box:\n  force: 1\nvm:\n  packer: true\n")
    monkeypatch.setenv("BIB_CONFIG", str(config))
    monkeypatch.setattr(bib, "CONFIG", bib.load_config())
    assert bib.env_flag("BIB_FORCE") is True
    assert bib.env_flag("BIB_VM_PACKER") is True
    assert bib.env_flag("BIB_NOT_SET_ANYWHERE") is False
    # And the environment still wins, the same way it does for every other setting.
    monkeypatch.setenv("BIB_VM_PACKER", "0")
    assert bib.env_flag("BIB_VM_PACKER") is False


def test_the_engine_can_be_named_in_the_settings_file(tmp_path, monkeypatch):
    config = tmp_path / "bib.yaml"
    config.write_text("box:\n  engine: docker\n")
    monkeypatch.setenv("BIB_CONFIG", str(config))
    monkeypatch.setattr(bib, "CONFIG", bib.load_config())
    monkeypatch.setattr(
        bib.shutil, "which", lambda name: f"/usr/bin/{name}" if name == "docker" else None
    )
    assert bib.find_engine() == "/usr/bin/docker"


def test_no_settings_file_is_normal_and_silent(tmp_path, monkeypatch):
    monkeypatch.setenv("BIB_CONFIG", str(tmp_path / "absent.yaml"))
    assert bib.load_config() == {}


# The icon is built with osacompile, sips and iconutil, none of which exist off
# macOS — and CI runs the suite on Linux. Skipped rather than faked: what these
# assert is that macOS itself accepts what bib writes, and a fake would assert
# nothing.
macos_only = pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS tooling")


def test_the_launcher_names_the_interpreter_instead_of_trusting_the_path(tmp_path, monkeypatch):
    # A Dock launch gets almost none of a login shell's PATH, so `env python3`
    # resolves against /usr/bin — where an unconfigured Mac has a stub that opens
    # the "install command line tools" dialog instead of running anything.
    script = tmp_path / "bib.py"
    script.touch()
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.setattr(sys, "executable", "/opt/python/bin/python3")
    monkeypatch.setattr(sys, "prefix", "/opt/python")
    monkeypatch.setattr(sys, "base_prefix", "/opt/python")
    assert bib.launcher_command() == f"/opt/python/bin/python3 {shlex.quote(str(script.resolve()))}"


def test_the_launcher_does_not_bake_in_a_virtualenv(tmp_path, monkeypatch):
    # Never exercised before: under pytest sys.argv[0] is the runner, which has no
    # .py suffix, so launcher_command returned at the frozen-build branch and the
    # icon test happily baked pytest's own path into the bundle it asserted on.
    base = tmp_path / "base"
    (base / "bin").mkdir(parents=True)
    (base / "bin" / "python3").touch()
    script = tmp_path / "bib.py"
    script.touch()
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.setattr(sys, "executable", str(tmp_path / "throwaway" / "bin" / "python3"))
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "throwaway"))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    command = bib.launcher_command()
    # The icon has to outlive the shell that wrote it, and bib imports nothing
    # outside the standard library, so the base interpreter runs it just as well.
    assert command.startswith(shlex.quote(str(base / "bin" / "python3")))
    assert "throwaway" not in command


def test_a_virtualenv_that_names_a_base_which_is_gone_falls_back(tmp_path, monkeypatch):
    script = tmp_path / "bib.py"
    script.touch()
    monkeypatch.setattr(sys, "argv", [str(script)])
    monkeypatch.setattr(sys, "executable", "/venv/bin/python3")
    monkeypatch.setattr(sys, "prefix", "/venv")
    monkeypatch.setattr(sys, "base_prefix", str(tmp_path / "no-such-base"))
    assert bib.launcher_command().startswith("/venv/bin/python3 ")


def test_a_frozen_build_is_its_own_interpreter(tmp_path, monkeypatch):
    binary = tmp_path / "bib"
    binary.touch()
    monkeypatch.setattr(sys, "argv", [str(binary)])
    assert bib.launcher_command() == shlex.quote(str(binary.resolve()))


@macos_only
def test_the_icon_is_a_real_app_bundle_rather_than_a_script(tmp_path, monkeypatch):
    """A shell script named in CFBundleExecutable is launched and then does not run.

    LaunchServices reports success, nothing executes, and nothing reaches the log
    — so the failure is invisible. osacompile builds a real signed bundle.
    """
    monkeypatch.setattr(bib, "APPS_DIR", tmp_path / "Applications")
    # bib.py, not pytest: sys.argv[0] under pytest is the runner, and a bundle
    # pointing at that is not the launcher anyone would ever get.
    entry = Path(bib.__file__).resolve()
    monkeypatch.setattr(sys, "argv", [str(entry)])
    bib.cmd_vm_icon("tart", bib.VmConfig())
    bundle = tmp_path / "Applications" / "Google Chrome in a Box.app"
    assert (bundle / "Contents" / "MacOS" / "applet").exists()
    compiled = bib.run(
        "/usr/bin/osadecompile",
        str(bundle / "Contents" / "Resources" / "Scripts" / "main.scpt"),
        capture=True,
    ).stdout
    # The settings are baked in: a Dock click inherits almost none of a login
    # shell's environment, so a BIB_VM_NAME set in a profile would open another VM.
    assert "BIB_VM_NAME=browser-vm" in compiled
    assert "vm open" in compiled
    assert str(entry) in compiled, "the bundle has to invoke bib, not whatever ran it"


@macos_only
def test_a_second_vm_gets_its_own_icon(tmp_path, monkeypatch):
    monkeypatch.setattr(bib, "APPS_DIR", tmp_path / "Applications")
    monkeypatch.setenv("BIB_VM_NAME", "work-vm")
    bib.cmd_vm_icon("tart", bib.VmConfig())
    assert (tmp_path / "Applications" / "Google Chrome in a Box (work-vm).app").exists()


def test_an_applescript_string_escapes_its_two_special_characters():
    # A share path with a quote in it would otherwise end the string early and
    # compile into something else entirely.
    assert bib.applescript_string(r'a"b\c') == r'"a\"b\\c"'


@macos_only
def test_the_icon_is_a_valid_pdf_that_sips_can_rasterise(tmp_path):
    """Hand-written PDF, so a malformed one would fail silently as a missing icon.

    The check is that macOS itself reads it, not that the bytes look plausible.
    """
    source = tmp_path / "icon.pdf"
    source.write_bytes(bibicon.pdf())
    assert source.read_bytes().startswith(b"%PDF-")
    out = tmp_path / "icon.png"
    bib.run(
        "/usr/bin/sips",
        "-s",
        "format",
        "png",
        "-z",
        "128",
        "128",
        str(source),
        "--out",
        str(out),
        capture=True,
    )
    assert out.exists()
    # Transparent outside the artwork, or the icon wears a white square.
    alpha = bib.run("/usr/bin/sips", "-g", "hasAlpha", str(out), capture=True).stdout
    assert "yes" in alpha


@macos_only
def test_the_icon_holds_every_size_macos_asks_for(tmp_path):
    """Chrome's own icns stops at 256, which is what made the Dock look soft."""
    target = tmp_path / "applet.icns"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    bib._build_icns(target, scratch)
    unpacked = tmp_path / "out.iconset"
    bib.run("/usr/bin/iconutil", "-c", "iconset", "-o", str(unpacked), str(target), capture=True)
    present = {png.name for png in unpacked.glob("*.png")}
    assert "icon_512x512@2x.png" in present, "no 1024, so the largest sizes are upscaled"
    assert "icon_16x16.png" in present


def test_the_drawing_uses_both_the_browser_and_the_box():
    """The name is the whole point: a second Chrome, in a box.

    Asserting on the colours rather than the shapes — it is the four Google ones
    plus cardboard that carry the meaning, and geometry has no stable assertion.
    """
    drawing = "\n".join(bibicon._artwork())
    for colour in (bibicon.RED, bibicon.YELLOW, bibicon.GREEN, bibicon.BLUE):
        assert bibicon._fill(colour) in drawing
    assert bibicon._fill(bibicon.CARTON_FACE) in drawing
    assert bibicon._fill(bibicon.CARTON_FLAP) in drawing


def test_opening_a_running_guest_brings_its_window_forward(tmp_path, monkeypatch, calls):
    """Otherwise a click on the launcher does nothing whenever the VM is already up.

    Which is most of the time, and it reads as the launcher being broken.
    """
    bundle = tmp_path / "tart.app"
    binary = bundle / "Contents" / "MacOS" / "tart"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    monkeypatch.setattr(bib, "vm_running", lambda tart, vm: True)
    bib.cmd_vm_open(str(binary), bib.VmConfig())
    assert ["/usr/bin/open", "-a", str(bundle)] in calls


def test_a_tart_outside_an_app_bundle_is_left_alone(tmp_path, monkeypatch, calls):
    # A bare binary has no bundle, so there is nothing for `open -a` to activate
    # and passing it one would be an error rather than a no-op.
    binary = tmp_path / "bin" / "tart"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    monkeypatch.setattr(bib, "vm_running", lambda tart, vm: True)
    bib.cmd_vm_open(str(binary), bib.VmConfig())
    assert not any(call[:2] == ["/usr/bin/open", "-a"] for call in calls)


def test_the_state_file_says_what_each_value_is(tmp_path):
    """It replaced a directory of bare values — vm-last-ip held an address and
    nothing else, so the only way to know what it was for was to read the source."""
    bib.write_state(last_ip="10.0.0.5")
    written = bib.STATE.read_text()
    assert "last_ip: 10.0.0.5" in written
    assert written.startswith("#"), "no line saying where the file came from"
    assert bib.read_state()["last_ip"] == "10.0.0.5"


def test_updating_one_value_keeps_the_others(tmp_path):
    bib.write_state(last_ip="10.0.0.5", something_else="kept")
    bib.write_state(last_ip="10.0.0.6")
    assert bib.read_state() == {"last_ip": "10.0.0.6", "something_else": "kept"}


def test_the_old_remembered_address_is_carried_into_the_state_file(tmp_path):
    """Losing it costs a guest that arp has forgotten, which is the case it is for."""
    bib.SECRETS.mkdir(parents=True, exist_ok=True)
    (bib.SECRETS / "vm-last-ip").write_text("10.0.0.9\n")
    bib.migrate_flat_secrets()
    assert bib.read_state()["last_ip"] == "10.0.0.9"
    assert not (bib.SECRETS / "vm-last-ip").exists()


@pytest.mark.parametrize("key", sorted(bibbrowsers.BROWSERS.keys() - {bibbrowsers.ALL}))
def test_every_browser_installs_from_a_script_that_actually_runs(key, tmp_path):
    """Rendering is not running. Firefox and Chromium take different shapes —
    a different archive, a different profile, a different way of being made the
    default — and a template can look right and still be unrunnable shell."""
    (tmp_path / "Downloads").mkdir()
    browser = bibbrowsers.BROWSERS[key]
    result = _run_guest_script(
        bib.guest_install_script("pw", browser=browser),
        tmp_path,
        share_exists=True,
        browser=browser,
        # Chromium and Vivaldi look a version up first, and each reads a different
        # kind of answer: a bare build number, and a Sparkle feed. The fake gives
        # whichever the URL asks for, so the real filter has something to parse.
        override_bin={
            "curl": '#!/bin/sh\nfor a in "$@"; do prev="$last"; last="$a"; done\n'
            'case "$last" in\n'
            "  *appcast.xml) printf '%s\\n' "
            "'<item><sparkle:version>9.9.9.9</sparkle:version></item>' ;;\n"
            "  *) echo 1234567 ;;\n"
            "esac\n",
            # Real enough that the move after it is a real move: a ditto that only
            # exits 0 would let a broken unpack-and-install sequence pass.
            "ditto": '#!/bin/sh\nfor a in "$@"; do dest="$a"; done\n'
            f'mkdir -p "$dest/{browser.inside}"\n',
        },
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("key", sorted(bibbrowsers.BROWSERS.keys() - {bibbrowsers.ALL}))
def test_every_browser_is_pointed_at_the_shared_downloads_folder(key):
    """The whole point of the share is that what you download lands on the host."""
    script = bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS[key])
    assert bib.GUEST_SHARE in script


def test_the_install_script_refuses_the_all_sentinel():
    # `all` is a mode, not a browser: its row carries no app name, so browser.app is
    # "/Applications" itself — and this script does `rm -rf` on it before installing.
    # install_browsers expands the choice first, so nothing reaches this today; the
    # guard is what keeps the next call site from finding out the hard way.
    with pytest.raises(bib.Failure) as failure:
        bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS[bibbrowsers.ALL])
    assert "expand" in str(failure.value)


def test_every_pass_writes_the_whole_dock_row(monkeypatch):
    # Clear-on-the-first-pass, append-on-the-rest assumed the clear survived, and on
    # a fresh guest it does not: the Dock writes its own default layout the first
    # time it starts for a new account, and that landed on top of pass one. The
    # result was Apple's eighteen apps plus the last two browsers, with the first
    # one missing entirely — seen on a real build. Writing the complete row every
    # pass is self-healing: whatever clobbers it, the next pass puts it right.
    scripts: list[str] = []
    monkeypatch.setattr(bib, "host_time_zone", lambda: ("Europe/Zurich", "Zurich"))
    monkeypatch.setattr(bib, "guest_ssh", lambda vm, ip, script: scripts.append(script) or 0)
    assert bib.install_browsers(bib.VmConfig(browser=bibbrowsers.ALL), "10.0.0.9", "pw") == 0

    assert len(scripts) == len(bibbrowsers.expand(bibbrowsers.ALL))
    assert not any("-array-add" in s for s in scripts), "append cannot repair a clobber"
    for script in scripts:
        assert "persistent-apps -array \\" in script
        # Split on the command, not the word: the comment above it says it too.
        row = script.split("persistent-apps -array")[1].split("persistent-others")[0]
        for browser in bibbrowsers.expand(bibbrowsers.ALL):
            assert browser.app in row, browser.key


def test_a_single_browser_run_puts_only_that_one_in_the_dock():
    script = bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS["firefox"])
    row = script.split("persistent-apps -array")[1].split("persistent-others")[0]
    assert bibbrowsers.BROWSERS["firefox"].app in row
    assert bibbrowsers.BROWSERS["chrome"].app not in row


def test_firefox_gets_a_profiles_ini_and_chromium_does_not():
    # Firefox ignores a profile directory it has not been told about, so the files
    # alone would be written and never read.
    firefox = bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS["firefox"])
    chrome = bib.guest_install_script("pw", browser=bibbrowsers.BROWSERS["chrome"])
    assert "profiles.ini" in firefox
    assert "user_pref" in firefox
    assert "profiles.ini" not in chrome
    assert "Local State" in chrome


def test_each_browser_draws_a_different_icon():
    """A launcher per browser is useless if they all look the same in the Dock."""
    drawn = {
        key: bibicon.pdf(browser.palette, browser.mark)
        for key, browser in bibbrowsers.BROWSERS.items()
    }
    assert len(set(drawn.values())) == len(drawn), "two browsers render identically"


@pytest.mark.parametrize("key", sorted(bibbrowsers.BROWSERS.keys() - {bibbrowsers.ALL}))
def test_every_browser_has_a_full_palette(key):
    # _artwork unpacks exactly four: top, lower left, lower right, centre. A short
    # one would raise at draw time, which is after the bundle has been written.
    assert len(bibbrowsers.BROWSERS[key].palette) == 4


def test_every_module_is_in_the_wheel():
    """Twice now a new module shipped in a wheel that did not contain it.

    The failure is invisible until someone installs it: the tests import from the
    source tree and pass, and only the packaged console script raises
    ModuleNotFoundError. Read with a regex rather than tomllib, which needs 3.11
    while bib supports 3.10.
    """
    root = Path(__file__).resolve().parent.parent
    manifest = (root / "pyproject.toml").read_text()
    listed = re.search(r"only-include = \[([^\]]*)\]", manifest)
    assert listed, "the wheel manifest has moved"
    shipped = set(re.findall(r'"([^"]+)"', listed.group(1)))
    present = {module.name for module in root.glob("bib*.py")}
    assert present <= shipped, f"not in the wheel: {sorted(present - shipped)}"


def test_firefox_is_a_different_shape_and_not_a_recoloured_chrome():
    """Colouring Chrome's wheel orange looks like a broken Chrome, not a Firefox.

    The shapes are compared with one palette held constant, so what differs can
    only be the geometry.
    """
    palette = bibicon.DEFAULT_PALETTE
    wheel = bibicon.pdf(palette, "wheel")
    flame = bibicon.pdf(palette, "flame")
    assert wheel != flame
    assert bibbrowsers.BROWSERS["firefox"].mark == "flame"
    # Chromium's real mark is Chrome's, in blue and grey. Same shape is correct.
    assert bibbrowsers.BROWSERS["chromium"].mark == bibbrowsers.BROWSERS["chrome"].mark


def test_every_browser_names_a_mark_that_exists():
    # An unknown one raises inside the drawing, which is after the bundle has been
    # written and the launcher already looks installed.
    for browser in bibbrowsers.BROWSERS.values():
        assert browser.mark in bibicon.MARKS, browser.key


def test_the_all_mode_installs_every_browser_and_the_container_refuses_it():
    """One image serves one browser, so 'all' can only mean the VM."""
    assert [b.key for b in bibbrowsers.expand(bibbrowsers.ALL)] == [
        "chrome",
        "firefox",
        "vivaldi",
        "chromium",
    ]
    assert bibbrowsers.BROWSERS[bibbrowsers.ALL].mark == "globe"


def test_the_container_says_why_it_cannot_hold_every_browser(monkeypatch):
    monkeypatch.setenv("BIB_BROWSER", "all")
    with pytest.raises(bib.Failure, match="VM mode"):
        bib.Config()


def test_the_vm_installs_one_browser_per_pass(monkeypatch, tmp_path):
    """The script stays single-browser; the loop is outside it.

    A script that installed three would need a second dimension of conditionals
    inside shell that is already the hardest thing here to read.
    """
    monkeypatch.setenv("BIB_BROWSER", "all")
    seen = []
    monkeypatch.setattr(bib, "guest_ssh", lambda vm, ip, script=None: seen.append(script) or 0)
    monkeypatch.setattr(bib, "host_time_zone", lambda: ("Europe/Zurich", "Zurich"))
    assert bib.install_browsers(bib.VmConfig(), "10.0.0.5", "pw") == 0
    # One per browser, counted from the table rather than written down, so adding a
    # fourth is a row and not an edit here.
    assert len(seen) == len(bibbrowsers.expand(bibbrowsers.ALL))
    for browser in bibbrowsers.expand(bibbrowsers.ALL):
        assert any(browser.app in script for script in seen), browser.key


def test_a_failing_browser_stops_the_rest(monkeypatch):
    # Otherwise the run reports the last browser's exit code and the earlier
    # failure disappears, which is the one thing a loop must not do.
    monkeypatch.setenv("BIB_BROWSER", "all")
    monkeypatch.setattr(bib, "guest_ssh", lambda vm, ip, script=None: 1)
    monkeypatch.setattr(bib, "host_time_zone", lambda: ("Europe/Zurich", "Zurich"))
    assert bib.install_browsers(bib.VmConfig(), "10.0.0.5", "pw") == 1


@macos_only
def test_the_built_icns_differs_per_browser_not_just_the_drawing(tmp_path):
    """The drawing took a palette and the thing that packs it threw it away.

    Every launcher wore Chrome's wheel. The earlier test called bibicon.pdf
    directly, so it passed while the path that actually writes the icon did not
    use either argument — which is why this one goes through _build_icns.
    """
    built = {}
    for key in ("chrome", "firefox", bibbrowsers.ALL):
        browser = bibbrowsers.BROWSERS[key]
        scratch = tmp_path / key
        scratch.mkdir()
        target = scratch / "applet.icns"
        bib._build_icns(target, scratch, browser.palette, browser.mark)
        built[key] = target.read_bytes()
    assert len(set(built.values())) == len(built), "the launchers all look the same"


def test_the_tart_bundle_is_found_through_homebrews_shim(tmp_path):
    """The layout on every machine that followed the README.

    `brew install cirruslabs/cli/tart` puts a bash shim at bin/tart, so resolving
    the binary ends somewhere no parent is a bundle at all — the real one is a
    sibling of that bin, under libexec. The earlier test built .app/Contents/MacOS
    by hand, a shape Homebrew's tart does not have, and passed while the guard it
    covers did nothing on real installs.
    """
    prefix = tmp_path / "Cellar" / "tart" / "2.32.1"
    shim = prefix / "bin" / "tart"
    shim.parent.mkdir(parents=True)
    shim.write_text('#!/bin/bash\nexec .../tart.app/Contents/MacOS/tart "$@"\n')
    bundle = prefix / "libexec" / "tart.app"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    assert bib.tart_bundle(str(shim)) == bundle


def test_a_bare_tart_binary_has_no_bundle_to_activate(tmp_path):
    binary = tmp_path / "bin" / "tart"
    binary.parent.mkdir(parents=True)
    binary.write_text("")
    assert bib.tart_bundle(str(binary)) is None


def test_a_detached_guest_survives_writing_to_stderr_after_bib_exits(tmp_path, isolate_secrets):
    """A pipe nobody drains is not a detach.

    tart blocks once 64 KiB of diagnostics fill it, and when bib exits the read
    end closes so the next write is a SIGPIPE. Either one kills the guest that
    start_new_session was added to keep alive.
    """
    noisy = tmp_path / "faketart"
    # More than a pipe buffer holds, so a pipe would block rather than finish.
    noisy.write_text("#!/bin/sh\nawk 'BEGIN{for(i=0;i<3000;i++)print \"diagnostic line\"}' >&2\n")
    noisy.chmod(0o755)
    boot = isolate_secrets.start_detached(str(noisy), bib.VmConfig())
    assert boot.wait(timeout=30) == 0, "tart blocked writing to a pipe nobody reads"
    assert "diagnostic line" in bib.BOOT_LOG.read_text()
