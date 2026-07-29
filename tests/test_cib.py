"""Tests for cib.py.

Nothing here touches a real container engine: `run` is replaced with a recorder,
so the actual command construction is asserted instead of being described.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cib


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """cib is configured by CIB_* variables, so a developer who actually uses the
    tool would otherwise fail its tests."""
    for name in [k for k in os.environ if k.startswith("CIB_")]:
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

    monkeypatch.setattr(cib, "run", fake_run)
    monkeypatch.setattr(cib, "find_engine", lambda: "podman")
    recorded.env = recorded_env
    return recorded


def flat(calls: list[list[str]]) -> str:
    return "\n".join(" ".join(call) for call in calls)


# --- configuration ------------------------------------------------------------


def test_defaults_are_the_values_the_container_needs():
    cfg = cib.Config()
    assert len(cfg.password) >= cib.MIN_PASSWORD_LEN
    assert cfg.resolution == ""  # dynamic: the desktop follows the browser window
    assert cfg.port == 6901
    cfg.check()


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("password", "abc", "at least 6 characters"),
        ("resolution", "2560x1600", "exceeds the modes KasmVNC ships"),
        ("resolution", "1920x1201", "exceeds the modes KasmVNC ships"),
        ("resolution", "huge", "must look like"),
        ("resolution", "1920", "must look like"),
    ],
)
def test_settings_that_kill_the_container_are_rejected(field, value, expected):
    with pytest.raises(cib.Failure, match=expected):
        cib.Config(**{field: value}).check()


def test_the_image_is_pinned_to_a_version():
    assert ":latest" not in cib.DEFAULT_IMAGE
    assert cib.DEFAULT_IMAGE.startswith("docker.io/kasmweb/chrome:")


def test_the_jpeg_quality_stays_in_the_range_kasmvnc_accepts():
    # DynamicQualityMax=10 makes Xvnc exit with a fatal error.
    for key in ("DynamicQualityMin", "DynamicQualityMax"):
        value = int(cib.VNC_OPTIONS.split(f"{key}=")[1].split()[0])
        assert 0 <= value <= 9


def test_the_login_prompt_stays_disabled():
    assert "-DisableBasicAuth=1" in cib.VNC_OPTIONS


# --- engine resolution --------------------------------------------------------


def test_engine_prefers_podman(monkeypatch):
    monkeypatch.delenv("CIB_ENGINE", raising=False)
    monkeypatch.setattr(cib.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert cib.find_engine() == "/usr/bin/podman"


def test_engine_falls_back_to_docker(monkeypatch):
    monkeypatch.delenv("CIB_ENGINE", raising=False)
    monkeypatch.setattr(
        cib.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None
    )
    assert cib.find_engine() == "/usr/bin/docker"


def test_an_unusable_engine_override_fails_loudly(monkeypatch):
    monkeypatch.setenv("CIB_ENGINE", "nope")
    monkeypatch.setattr(cib.shutil, "which", lambda name: None)
    with pytest.raises(cib.Failure, match="not on PATH"):
        cib.find_engine()


def test_no_engine_at_all_fails_loudly(monkeypatch):
    monkeypatch.delenv("CIB_ENGINE", raising=False)
    monkeypatch.setattr(cib.shutil, "which", lambda name: None)
    with pytest.raises(cib.Failure, match="need podman or docker"):
        cib.find_engine()


# --- up -----------------------------------------------------------------------


def test_up_binds_the_ui_to_localhost_only(calls, monkeypatch):
    monkeypatch.setattr(cib, "container_running", lambda *a: False)
    monkeypatch.setattr(cib, "wait_for_ui", lambda *a: None)
    monkeypatch.setattr(cib, "ensure_desktop", lambda *a: True)
    cib.cmd_up("podman", cib.Config())
    assert "-p 127.0.0.1:6901:6901" in flat(calls)


def test_up_asks_for_a_bridge_network(calls, monkeypatch):
    # kasm's startup script waits forever for a veth; rootless podman's default
    # network namespace has none, so the desktop never comes up without this.
    monkeypatch.setattr(cib, "container_running", lambda *a: False)
    monkeypatch.setattr(cib, "wait_for_ui", lambda *a: None)
    monkeypatch.setattr(cib, "ensure_desktop", lambda *a: True)
    cib.cmd_up("podman", cib.Config())
    assert "--network bridge" in flat(calls)


def test_up_reuses_a_healthy_container(calls, monkeypatch):
    monkeypatch.delenv("CIB_FORCE", raising=False)
    monkeypatch.setattr(cib, "container_running", lambda *a: True)
    monkeypatch.setattr(cib, "ui_is_up", lambda *a: True)
    monkeypatch.setattr(cib, "ensure_desktop", lambda *a: True)
    cib.cmd_up("podman", cib.Config())
    assert "run -d" not in flat(calls)
    assert "rm -f" not in flat(calls)


def test_cib_force_recreates_a_healthy_container(calls, monkeypatch):
    monkeypatch.setenv("CIB_FORCE", "1")
    monkeypatch.setattr(cib, "container_running", lambda *a: True)
    monkeypatch.setattr(cib, "ui_is_up", lambda *a: True)
    monkeypatch.setattr(cib, "wait_for_ui", lambda *a: None)
    monkeypatch.setattr(cib, "ensure_desktop", lambda *a: True)
    cib.cmd_up("podman", cib.Config())
    assert "run -d" in flat(calls)


def test_up_rejects_a_bad_setting_before_touching_the_engine(calls, monkeypatch):
    monkeypatch.setenv("CIB_PASSWORD", "abc")
    with pytest.raises(cib.Failure):
        cib.cmd_up("podman", cib.Config())
    assert calls == []


# --- readiness ----------------------------------------------------------------


def test_wait_for_ui_returns_once_the_ui_answers(monkeypatch):
    monkeypatch.setattr(cib, "ui_status", lambda cfg: 200)
    cib.wait_for_ui("podman", cib.Config())


def test_wait_for_ui_reports_a_returning_login_prompt(monkeypatch):
    monkeypatch.setattr(cib, "ui_status", lambda cfg: 401)
    with pytest.raises(cib.Failure, match="asking for a login"):
        cib.wait_for_ui("podman", cib.Config())


def test_wait_for_ui_reports_a_container_that_died_at_boot(calls, monkeypatch):
    monkeypatch.setattr(cib, "ui_status", lambda cfg: None)
    monkeypatch.setattr(cib, "container_running", lambda *a: False)
    with pytest.raises(cib.Failure, match="exited during boot"):
        cib.wait_for_ui("podman", cib.Config())


def test_wait_for_ui_gives_up_after_the_deadline(monkeypatch):
    monkeypatch.setattr(cib, "ui_status", lambda cfg: None)
    monkeypatch.setattr(cib, "container_running", lambda *a: True)
    monkeypatch.setattr(cib.time, "sleep", lambda seconds: None)
    with pytest.raises(cib.Failure, match="did not come up within 0s"):
        cib.wait_for_ui("podman", cib.Config(wait_secs=0))


# --- the remaining commands ---------------------------------------------------


def test_logs_does_not_follow_by_default(calls):
    cib.cmd_logs("podman", cib.Config())
    assert "logs --tail 200 chrome-in-a-box" in flat(calls)
    assert "-f" not in flat(calls)


def test_logs_follows_when_asked(calls):
    cib.cmd_logs("podman", cib.Config(), follow=True)
    assert "logs -f chrome-in-a-box" in flat(calls)


def test_reset_needs_confirmation(calls, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    cib.cmd_reset("podman", cib.Config())
    assert "volume rm" not in flat(calls)
    assert "Cancelled." in capsys.readouterr().out


def test_reset_deletes_the_volume_when_confirmed(calls, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    cib.cmd_reset("podman", cib.Config())
    assert "volume rm chrome-in-a-box-profile" in flat(calls)


def test_reset_treats_a_closed_stdin_as_no(calls, monkeypatch):
    def raise_eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    cib.cmd_reset("podman", cib.Config())
    assert "volume rm" not in flat(calls)


def test_down_removes_the_container(calls):
    cib.cmd_down("podman", cib.Config())
    assert "rm -f chrome-in-a-box" in flat(calls)


def test_ensure_desktop_clears_a_stale_profile_lock_and_sets_the_mode():
    assert "Singleton*" in cib.DESKTOP_SCRIPT
    assert 'xrandr -s "$RES"' in cib.DESKTOP_SCRIPT


def test_ensure_desktop_warns_instead_of_failing(monkeypatch, capsys):
    def failing_run(engine, *args, check=True, capture=False):
        return subprocess.CompletedProcess(
            [engine, *args], 1, stdout="", stderr="no such container"
        )

    monkeypatch.setattr(cib, "run", failing_run)
    assert cib.ensure_desktop("podman", cib.Config()) is False
    assert "warning" in capsys.readouterr().err


# --- the macOS VM variant -----------------------------------------------------


def test_the_vm_variant_refuses_on_a_non_mac(monkeypatch):
    monkeypatch.setattr(cib.platform, "system", lambda: "Linux")
    with pytest.raises(cib.Failure, match="needs macOS"):
        cib.find_tart()


def test_the_vm_variant_refuses_on_intel(monkeypatch):
    monkeypatch.setattr(cib.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cib.platform, "machine", lambda: "x86_64")
    with pytest.raises(cib.Failure, match="Apple silicon"):
        cib.find_tart()


def test_a_missing_tart_points_at_the_install_command(monkeypatch):
    monkeypatch.setattr(cib.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(cib.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(cib.shutil, "which", lambda name: None)
    with pytest.raises(cib.Failure, match="brew install"):
        cib.find_tart()


def test_vm_up_refuses_before_create(calls, monkeypatch):
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    with pytest.raises(cib.Failure, match="vm create"):
        cib.cmd_vm_up("tart", cib.VmConfig())


def test_vm_delete_needs_confirmation(calls, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    cib.cmd_vm_delete("tart", cib.VmConfig())
    assert "delete" not in flat(calls)


def test_vm_delete_removes_the_vm_when_confirmed(calls, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    cib.cmd_vm_delete("tart", cib.VmConfig())
    assert "delete chrome-vm" in flat(calls)


def test_every_vm_action_is_reachable_from_the_cli(monkeypatch):
    parser = cib.build_parser()
    action = next(a for a in parser._actions if isinstance(a, cib.argparse._SubParsersAction))
    vm_parser = action.choices["vm"]
    choices = next(a.choices for a in vm_parser._actions if a.dest == "action")
    assert set(choices) == set(cib.VM_ACTIONS)


# --- cli ----------------------------------------------------------------------


def test_unknown_command_is_rejected():
    with pytest.raises(SystemExit) as excinfo:
        cib.main(["frobnicate"])
    assert excinfo.value.code != 0


def test_bare_invocation_prints_help_and_fails(capsys):
    assert cib.main([]) == 2
    assert "box" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["frobnicate"], ["box"], ["box", "frobnicate"], ["vm", "nope"]])
def test_unusable_invocations_are_rejected(argv):
    with pytest.raises(SystemExit) as excinfo:
        cib.main(argv)
    assert excinfo.value.code != 0


@pytest.mark.parametrize("action", sorted(cib.BOX_ACTIONS))
def test_every_box_action_dispatches(action, monkeypatch):
    monkeypatch.setattr(cib, "find_engine", lambda: "podman")
    called = []
    monkeypatch.setitem(cib.BOX_ACTIONS, action, lambda *a, **k: called.append(action))
    if action == "logs":
        monkeypatch.setattr(cib, "cmd_logs", lambda *a, **k: called.append(action))
    assert cib.main(["box", action]) == 0
    assert called == [action]


@pytest.mark.parametrize("action", sorted(cib.VM_ACTIONS))
def test_every_vm_action_dispatches(action, monkeypatch):
    monkeypatch.setattr(cib, "find_tart", lambda: "tart")
    called = []
    monkeypatch.setitem(cib.VM_ACTIONS, action, lambda *a, **k: called.append(action))
    assert cib.main(["vm", action]) == 0
    assert called == [action]


def test_the_help_names_both_variants_and_their_trade_off(capsys):
    with pytest.raises(SystemExit):
        cib.main(["--help"])
    out = capsys.readouterr().out
    assert "cib box" in out and "cib vm" in out
    assert "iCloud Keychain" in out
    assert "Touch ID" in out


def test_follow_reaches_the_logs_command(monkeypatch):
    monkeypatch.setattr(cib, "find_engine", lambda: "podman")
    seen = {}
    monkeypatch.setattr(cib, "cmd_logs", lambda e, c, follow=False: seen.update(follow=follow))
    cib.main(["box", "logs", "-f"])
    assert seen == {"follow": True}


def test_failures_are_reported_without_a_traceback(monkeypatch, capsys):
    monkeypatch.setattr(cib, "find_engine", lambda: (_ for _ in ()).throw(cib.Failure("boom")))
    assert cib.main(["box", "status"]) == 1
    assert "error: boom" in capsys.readouterr().err


# --- the Homebrew formula updater ---------------------------------------------


def _formula() -> str:
    return (Path(__file__).resolve().parents[1] / "Formula" / "cib.rb").read_text()


def _updater():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import update_formula

    return update_formula


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
    formula = tmp_path / "cib.rb"
    formula.write_text(_formula())
    with pytest.raises(SystemExit, match="no valid sha256"):
        _updater().main(["2.3.4", str(formula), "macos-arm64=nope"])


# --- VM networking ------------------------------------------------------------


def test_the_vm_uses_bridged_networking_by_default():
    # Shared networking hands out a vmnet gateway that does not always answer DNS,
    # which leaves the guest with an address but no name resolution.
    args = cib.vm_run_args(cib.VmConfig())
    assert "--net-bridged=en0" in args
    assert args[-1] == "chrome-vm"


def test_the_vm_network_mode_and_interface_are_overridable(monkeypatch):
    monkeypatch.setenv("CIB_VM_INTERFACE", "en1")
    assert "--net-bridged=en1" in cib.vm_run_args(cib.VmConfig())
    monkeypatch.setenv("CIB_VM_NET", "shared")
    args = cib.vm_run_args(cib.VmConfig())
    assert not any(a.startswith("--net-") for a in args)
    monkeypatch.setenv("CIB_VM_NET", "host")
    assert "--net-host" in cib.vm_run_args(cib.VmConfig())


def test_a_failed_bridged_start_explains_the_alternatives(monkeypatch):
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: True)
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    with pytest.raises(cib.Failure, match="CIB_VM_NET=shared"):
        cib.cmd_vm_up("tart", cib.VmConfig())


# --- taking the guest over from here ------------------------------------------


@pytest.fixture
def resolving(monkeypatch):
    """Record engine calls and answer the ip lookup with an address."""
    recorded: list[list[str]] = []

    def fake_run(engine, *args, check=True, capture=False, env=None):
        recorded.append([engine, *args])
        return subprocess.CompletedProcess([engine, *args], 0, stdout="192.168.1.50\n", stderr="")

    monkeypatch.setattr(cib, "run", fake_run)
    return recorded


def test_a_bridged_guest_is_resolved_by_arp(resolving):
    # Bridged guests get their address from the real network, so tart's default
    # DHCP-lease resolver has nothing to read.
    assert cib.vm_ip("tart", cib.VmConfig()) == "192.168.1.50"
    assert "ip --resolver arp --wait 60 chrome-vm" in flat(resolving)


def test_a_shared_guest_is_resolved_by_dhcp(resolving, monkeypatch):
    monkeypatch.setenv("CIB_VM_NET", "shared")
    cib.vm_ip("tart", cib.VmConfig())
    assert "--resolver dhcp" in flat(resolving)


def test_an_unresolvable_guest_is_reported_clearly(monkeypatch):
    # Not "past Setup Assistant": the default path never shows one, so naming it
    # sent people looking for a screen that does not exist.
    monkeypatch.setattr(
        cib,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="no such VM"),
    )
    with pytest.raises(cib.Failure, match="cib vm status") as caught:
        cib.vm_ip("tart", cib.VmConfig())
    assert "Setup Assistant" not in str(caught.value)
    assert "no such VM" in str(caught.value), "tart's own reason must reach the user"


def test_the_ssh_command_does_not_pin_a_host_key():
    cmd = cib.ssh_command(cib.VmConfig(), "192.168.1.50")
    assert cmd[0].endswith("ssh")
    assert "StrictHostKeyChecking=no" in cmd
    assert cmd[-1] == "admin@192.168.1.50"


def test_the_ssh_user_is_overridable(monkeypatch):
    monkeypatch.setenv("CIB_VM_USER", "sapn")
    assert cib.ssh_command(cib.VmConfig(), "10.0.0.1")[-1] == "sapn@10.0.0.1"


def test_setup_installs_chrome_and_is_idempotent():
    assert "googlechrome.dmg" in cib.guest_install_script("pw")
    assert "already installed" in cib.guest_install_script("pw")
    assert cib.guest_install_script("pw").startswith("set -eu")


def test_setup_names_the_one_switch_the_guest_needs(monkeypatch, capsys):
    monkeypatch.setattr(cib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(cib, "guest_ssh", lambda *a, **k: 255)
    with pytest.raises(cib.Failure, match="Remote Login"):
        cib.cmd_vm_setup("tart", cib.VmConfig())


def test_setup_passes_the_install_script_to_the_guest(credentials, monkeypatch):
    seen = {}
    password = cib.guest_password(create=True)
    monkeypatch.setattr(cib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(
        cib, "guest_ssh", lambda vm, ip, script=None: seen.update(script=script) or 0
    )
    cib.cmd_vm_setup("tart", cib.VmConfig())
    # The generated password has to reach the guest: sshd runs this with no tty and
    # no cached credential, so sudo there can only be fed one.
    assert seen["script"] == cib.guest_install_script(password)
    assert password in seen["script"]


def test_the_guest_password_never_reaches_a_process_list(credentials):
    # `ssh host "<script>"` would put the whole script, password included, in this
    # host's argv. It is read on stdin instead.
    password = cib.guest_password(create=True)
    script = cib.guest_install_script(password)
    argv = cib.ssh_command(cib.VmConfig(), "192.168.1.50", script)
    assert password not in " ".join(argv)
    assert argv[-2:] == ["/bin/sh", "-s"]


def test_an_interactive_shell_keeps_this_terminals_stdin(credentials, monkeypatch):
    # 'cib vm ssh' has no script; feeding it one would close stdin immediately.
    seen = {}
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: seen.update(cmd=cmd, kw=kw) or subprocess.CompletedProcess(cmd, 0),
    )
    cib.guest_ssh(cib.VmConfig(), "192.168.1.50")
    assert seen["kw"]["input"] is None
    assert "-s" not in seen["cmd"]


# --- the unattended build -----------------------------------------------------


@pytest.fixture
def credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(cib, "CREDENTIALS", tmp_path / "vm-credentials")
    return cib.CREDENTIALS


def test_the_guest_password_is_generated_once_and_remembered(credentials):
    first = cib.guest_password(create=True)
    assert len(first) >= 20
    assert cib.guest_password() == first
    assert credentials.stat().st_mode & 0o777 == 0o600


def test_asking_for_a_password_before_the_build_says_so(credentials):
    with pytest.raises(cib.Failure, match="build the VM first"):
        cib.guest_password()


def test_create_drives_packer_with_the_generated_password(calls, credentials, monkeypatch):
    monkeypatch.setenv("CIB_VM_PACKER", "1")  # this covers the fallback path
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "find_packer", lambda: "packer")
    cib.cmd_vm_create("tart", cib.VmConfig())
    out = flat(calls)
    assert "packer build" in out
    assert str(cib.PACKER_TEMPLATE) in out
    # The password travels in the environment, never in argv.
    assert "password=" not in out
    assert any(
        e and e.get("PKR_VAR_password") == credentials.read_text().strip() for e in calls.env
    )
    assert "memory_gb=8" in out  # 8192 MB, passed to packer in GB


def test_a_missing_packer_points_at_the_install_command(monkeypatch):
    monkeypatch.setattr(cib.shutil, "which", lambda name: None)
    with pytest.raises(cib.Failure, match="brew install hashicorp/tap/packer"):
        cib.find_packer()


def test_the_template_drives_setup_assistant_and_keeps_gatekeeper():
    template = (Path(__file__).resolve().parents[1] / cib.PACKER_TEMPLATE).read_text()
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
    assert "resize=remote" in cib.Config().url
    assert 'if [ -n "$RES" ]' in cib.DESKTOP_SCRIPT


def test_an_empty_resolution_passes_preflight():
    cib.Config(resolution="").check()


# --- what the review found ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("CIB_PORT", "abc", "whole number"),
        ("CIB_VM_MEMORY", "8g", "whole number"),
        ("CIB_VM_DISK", "abc", "whole number"),
        ("CIB_VM_CPUS", "0", "at least 1"),
        ("CIB_VM_MEMORY", "512", "at least 1024"),
    ],
)
def test_a_bad_numeric_setting_is_an_error_not_a_traceback(name, value, expected, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(cib.Failure, match=expected):
        cib.Config() if name == "CIB_PORT" else cib.VmConfig()


def test_memory_is_rounded_up_not_truncated(calls, credentials, monkeypatch):
    monkeypatch.setenv("CIB_VM_PACKER", "1")  # this covers the fallback path
    # 8000 MB is 8 GB worth of intent; truncating gives the guest 7.
    monkeypatch.setenv("CIB_VM_MEMORY", "8000")
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "find_packer", lambda: "packer")
    cib.cmd_vm_create("tart", cib.VmConfig())
    assert "memory_gb=8" in flat(calls)


def test_the_generated_password_avoids_layout_dependent_characters(credentials):
    # It is typed into the guest as keystrokes, and -, _, y, z move between the US
    # and Swiss German layouts, so such a password would never match what was saved.
    # Generate real ones rather than restating the alphabet.
    forbidden = set("yzYZ-_/")
    for _ in range(200):
        credentials.unlink(missing_ok=True)
        password = cib.guest_password(create=True)
        assert len(password) >= 20
        assert not (set(password) & forbidden), password


def test_an_empty_credentials_file_is_not_treated_as_a_password(credentials):
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("  \n")
    with pytest.raises(cib.Failure, match="build the VM first"):
        cib.guest_password()


def test_the_password_file_is_never_briefly_world_readable(credentials, monkeypatch):
    seen = {}
    real_open = os.open

    def spy(path, flags, mode=0o777, **kwargs):
        if str(path) == str(credentials):
            seen["mode"] = mode
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", spy)
    cib.guest_password(create=True)
    assert seen["mode"] == 0o600


def test_an_unknown_network_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("CIB_VM_NET", "bridge")  # a typo for "bridged"
    with pytest.raises(cib.Failure, match="CIB_VM_NET must be"):
        cib.vm_run_args(cib.VmConfig())


def test_a_user_name_cannot_become_an_ssh_option(monkeypatch):
    monkeypatch.setenv("CIB_VM_USER", "-oProxyCommand=touch /tmp/pwn")
    with pytest.raises(cib.Failure, match="not a usable account name"):
        cib.ssh_command(cib.VmConfig(), "192.168.1.50")


def test_follow_is_rejected_where_it_means_nothing(monkeypatch):
    monkeypatch.setattr(cib, "find_engine", lambda: "podman")
    assert cib.main(["box", "status", "-f"]) == 1


def test_create_runs_packer_init_first(calls, credentials, monkeypatch):
    monkeypatch.setenv("CIB_VM_PACKER", "1")  # this covers the fallback path
    # Without it, every first-time user hits "Did you run packer init".
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "find_packer", lambda: "packer")
    cib.cmd_vm_create("tart", cib.VmConfig())
    out = flat(calls)
    assert out.index("packer init") < out.index("packer build")


def test_the_template_is_resolved_next_to_the_module_not_the_cwd():
    # Otherwise `cib vm create` only works from a checkout, in the right directory.
    assert cib.PACKER_TEMPLATE.is_absolute()
    assert cib.PACKER_TEMPLATE.parent.parent == Path(cib.__file__).resolve().parent


def test_the_guest_shares_a_host_folder_for_downloads(tmp_path, monkeypatch):
    # Downloads should land on the host, not inside the VM's disk image.
    monkeypatch.setenv("CIB_VM_SHARE", str(tmp_path / "dl"))
    args = cib.vm_run_args(cib.VmConfig())
    assert f"--dir=downloads:{tmp_path / 'dl'}" in args
    assert (tmp_path / "dl").is_dir()  # created if missing, so tart does not fail


def test_setup_points_the_guest_downloads_at_the_share():
    assert cib.GUEST_SHARE in cib.guest_install_script("pw")
    assert "ln -sfn" in cib.guest_install_script("pw")
    # An existing real folder must not be destroyed.
    assert "Downloads.local" in cib.guest_install_script("pw")


# sudo that actually runs what it is given, so the script's own sudo path is
# exercised rather than stubbed out. A stub returning 0 hid the bug where the
# clipboard agent was installed by a `sudo -n` that could never succeed.
_FAKE_SUDO = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -S|-n) shift ;;
    -p) shift 2 ;;
    *) break ;;
  esac
done
exec "$@"
"""

# install that creates its destination, so `test -x` at the end of the script
# observes whether the agent was really installed.
_FAKE_INSTALL = """#!/bin/sh
for dst in "$@"; do :; done
mkdir -p "$(dirname "$dst")"
printf '#!/bin/sh\\nexit 0\\n' > "$dst"
chmod 0755 "$dst"
"""


def _run_guest_script(script: str, home, share_exists: bool):
    """Execute the guest script the way ssh would: /bin/sh -e, with fakes."""
    bin_dir = home / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    for name in ("curl", "hdiutil", "tar"):
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
    for name, body_text in (("sudo", _FAKE_SUDO), ("install", _FAKE_INSTALL)):
        (bin_dir / name).write_text(body_text)
        (bin_dir / name).chmod(0o755)
    share = Path(cib.GUEST_SHARE)
    body = script.replace(str(share), str(home / "share"))
    body = body.replace("'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'", "true")
    body = body.replace("'/Applications/Google Chrome.app'", f"'{home / 'chrome.app'}'")
    body = body.replace("/usr/local/bin/tart-guest-agent", str(home / "agent"))
    if share_exists:
        (home / "share").mkdir(exist_ok=True)
    return subprocess.run(  # noqa: S603
        ["/bin/sh", "-e", "-c", body],
        env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )


def test_the_guest_script_fails_when_the_share_is_missing(tmp_path):
    # It used to warn, install Chrome, exit 0 — and cib then printed "Done".
    (tmp_path / "Downloads").mkdir()
    result = _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=False)
    assert result.returncode != 0
    assert "not mounted" in result.stderr


def test_the_guest_script_links_downloads_to_the_share(tmp_path):
    (tmp_path / "Downloads").mkdir()
    result = _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "Downloads").is_symlink()
    assert (tmp_path / "Downloads").resolve() == (tmp_path / "share").resolve()


def test_the_guest_script_keeps_a_non_empty_downloads_folder(tmp_path):
    (tmp_path / "Downloads").mkdir()
    (tmp_path / "Downloads" / "keep.txt").write_text("mine")
    result = _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "Downloads.local" / "keep.txt").read_text() == "mine"


def test_a_share_path_with_a_colon_is_rejected(monkeypatch, tmp_path):
    # tart parses --dir as name:path:options, so a colon would silently mis-parse.
    monkeypatch.setenv("CIB_VM_SHARE", str(tmp_path / "a:b"))
    with pytest.raises(cib.Failure, match="colon"):
        cib.vm_run_args(cib.VmConfig())


def test_an_unusable_share_path_is_reported_not_raised(monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    monkeypatch.setenv("CIB_VM_SHARE", str(blocker / "share"))
    with pytest.raises(cib.Failure, match="cannot use"):
        cib.vm_run_args(cib.VmConfig())


def test_a_zero_resolution_is_rejected():
    with pytest.raises(cib.Failure, match="must be positive"):
        cib.Config(resolution="0x0").check()


def test_the_password_never_reaches_the_argument_list(calls, credentials, monkeypatch):
    monkeypatch.setenv("CIB_VM_PACKER", "1")  # this covers the fallback path
    # argv is world-readable while the build runs, and is printed on failure.
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "find_packer", lambda: "packer")
    cib.cmd_vm_create("tart", cib.VmConfig())
    assert credentials.read_text().strip() not in flat(calls)


def test_the_template_installs_the_clipboard_agent():
    # Copy and paste between host and guest is not a tart flag: it needs
    # tart-guest-agent running inside the guest, and the password is meant to be
    # pasted rather than typed.
    template = (Path(__file__).resolve().parents[1] / cib.PACKER_TEMPLATE).read_text()
    assert "tart-guest-agent" in template
    assert "--install-daemon=launchd" in template
    # Pinned, not "latest": a build should produce the same guest twice, and
    # renovate moves the pin.
    assert "releases/latest/download" not in template
    assert "guest_agent_version" in template
    assert "test -x /usr/local/bin/tart-guest-agent" in template


def test_an_already_running_vm_is_not_a_networking_failure(calls, monkeypatch):
    # tart exits non-zero for a VM that is already up; blaming the bridge for that
    # sent the user off to degrade their DNS for no reason.
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: True)
    monkeypatch.setattr(cib, "vm_running", lambda *a, **k: True)
    cib.cmd_vm_up("tart", cib.VmConfig())
    assert "run" not in flat(calls)


def test_down_says_nothing_was_running_when_there_was_nothing(monkeypatch, capsys):
    # podman's `rm -f` exits 0 for a container that never existed.
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    cib.cmd_down("podman", cib.Config())
    assert "Not running." in capsys.readouterr().out


def test_the_resolution_is_normalised_before_it_reaches_xrandr(calls, monkeypatch):
    # xrandr reads anything but lowercase <int>x<int> as a mode index.
    monkeypatch.setenv("CIB_RESOLUTION", "1280 X 800")
    cib.ensure_desktop("podman", cib.Config())
    assert "RES=1280x800" in flat(calls)


def test_a_non_zero_remote_shell_is_not_a_connection_failure(monkeypatch):
    # ssh passes the remote shell's exit status through; only 255 is ssh's own.
    monkeypatch.setattr(cib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(cib, "guest_ssh", lambda *a, **k: 1)
    cib.cmd_vm_ssh("tart", cib.VmConfig())  # must not raise
    monkeypatch.setattr(cib, "guest_ssh", lambda *a, **k: 255)
    with pytest.raises(cib.Failure, match="Remote Login"):
        cib.cmd_vm_ssh("tart", cib.VmConfig())


def test_error_messages_name_commands_that_exist(monkeypatch):
    # The CLI is variant-scoped now, so "cib logs" would be rejected by argparse.
    source = Path(cib.__file__).read_text()
    for stale in ("'cib logs'", "'cib up'", "'cib down'", "'cib status'"):
        assert stale not in source, stale


def test_the_release_archive_has_a_top_level_directory():
    # Otherwise `tar -xzf` scatters ~44 files into the current directory.
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/release.yml").read_text()
    assert "tar -czf dist/cib-macos-arm64.tar.gz -C dist cib-macos-arm64" in workflow
    assert '-C dist "cib-linux-${{ matrix.arch }}"' in workflow


# --- the offline guest patcher ------------------------------------------------

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cibpatch  # noqa: E402


def test_the_password_verifier_matches_what_macos_expects():
    import plistlib

    blob = plistlib.loads(cibpatch.shadow_hash_data("hunter2"))
    entry = blob["SALTED-SHA512-PBKDF2"]
    # Wrong parameters do not fail loudly; the account just refuses every password.
    assert entry["iterations"] == 50_000
    assert len(entry["salt"]) == 32
    assert len(entry["entropy"]) == 128
    # The verifier has to reach the record, under the name macOS reads: dropping it
    # leaves an account that refuses every password, and no other test noticed.
    record = cibpatch.user_record(cibpatch.Account("admin", "hunter2"), "GUID")
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
        plistlib.loads(cibpatch.shadow_hash_data("same"))["SALTED-SHA512-PBKDF2"]["salt"]
        for _ in range(5)
    }
    assert len(salts) == 5


def test_kcpassword_is_padded_to_the_key_length():
    # Without the padding macOS reads past the end of the password.
    for password in ("a", "elevenchars", "exactly-eleven"):
        assert len(cibpatch.kcpassword(password)) % len(cibpatch.KCPASSWORD_KEY) == 0


def test_kcpassword_round_trips():
    key = cibpatch.KCPASSWORD_KEY
    encoded = cibpatch.kcpassword("s3cret")
    decoded = bytes(b ^ key[i % len(key)] for i, b in enumerate(encoded))
    assert decoded.startswith(b"s3cret\x00")


def test_the_user_record_stores_every_value_as_a_list():
    # DirectoryService silently ignores a plain string here.
    record = cibpatch.user_record(cibpatch.Account("admin", "pw"))
    assert all(isinstance(v, list) for v in record.values())
    assert record["name"] == ["admin"]
    assert record["uid"] == ["501"]


def test_patching_a_directory_that_is_not_a_guest_volume_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    with pytest.raises(cibpatch.PatchError, match="Data volume"):
        cibpatch.patch(tmp_path, cibpatch.Account("admin", "pw"))


def test_patching_without_a_first_boot_state_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    (tmp_path / "private/var/db").mkdir(parents=True)
    with pytest.raises(cibpatch.PatchError, match="booted once"):
        cibpatch.patch(tmp_path, cibpatch.Account("admin", "pw"))


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
    disk = tmp_path / ".tart" / "vms" / "chrome-vm" / "disk.img"
    disk.parent.mkdir(parents=True, exist_ok=True)
    disk.touch()
    monkeypatch.setattr(cib.Path, "home", classmethod(lambda cls: tmp_path))
    return disk


def test_create_prepares_the_guest_offline_by_default(calls, credentials, monkeypatch, tmp_path):
    # Typing into Setup Assistant is the fallback now, not the default.
    seen = {}
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cib.subprocess,
        "Popen",
        lambda *a, **k: _FakeBoot(),
    )
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: (
            seen.update(cmd=cmd, stdin=kw.get("input")) or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    cib.cmd_vm_create("tart", cib.VmConfig())
    out = flat(calls)
    assert "create --from-ipsw=latest --disk-size=100 chrome-vm" in out
    assert "packer" not in out
    # Only the patch runs as root, and the password goes in on stdin so it never
    # appears in the process list.
    assert seen["cmd"][0] == "/usr/bin/sudo"
    assert "cibpatch.py" in " ".join(seen["cmd"])
    assert not any("password" in str(a) for a in seen["cmd"])
    assert seen["stdin"].strip() == credentials.read_text().strip()


def test_the_packer_path_is_still_reachable(calls, credentials, monkeypatch):
    monkeypatch.setenv("CIB_VM_PACKER", "1")
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "find_packer", lambda: "packer")
    cib.cmd_vm_create("tart", cib.VmConfig())
    assert "packer build" in flat(calls)


def test_a_failed_patch_names_the_fallback(calls, credentials, monkeypatch, tmp_path):
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cib.subprocess,
        "Popen",
        lambda *a, **k: _FakeBoot(),
    )

    # The sudo probe must succeed so we reach the patch itself.
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0 if "-n" in cmd else 1),
    )
    with pytest.raises(cib.Failure, match="CIB_VM_PACKER=1"):
        cib.cmd_vm_create("tart", cib.VmConfig())


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
        cibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    assert cibpatch.data_volume("/dev/disk5") == "/dev/disk5s5"


def test_a_disk_without_a_data_volume_is_reported(monkeypatch):
    import plistlib

    empty = plistlib.dumps({"Containers": []})
    monkeypatch.setattr(
        cibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=empty, stderr=b""),
    )
    with pytest.raises(cibpatch.PatchError, match="physical store"):
        cibpatch.data_volume("/dev/disk9")


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
    account = cibpatch.Account("admin", "pw")
    cibpatch.add_to_group(root, "admin", account, "USER-GUID-1234")
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
    monkeypatch.setattr(cibpatch.os, "chown", lambda *a: None)
    account = cibpatch.Account("admin", "pw")
    cibpatch.create_account(root, account)
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
        cibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    assert cibpatch.data_volume("/dev/disk10") == "/dev/disk11s2"


def test_patching_without_root_says_so_instead_of_a_traceback(tmp_path, monkeypatch):
    # Writing dslocal and setting root ownership needs root; PermissionError is an
    # OSError, so it would have escaped the PatchError handler entirely.
    (tmp_path / "private/var/db").mkdir(parents=True)
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 501)
    with pytest.raises(cibpatch.PatchError, match="needs root"):
        cibpatch.patch(tmp_path, cibpatch.Account("admin", "pw"))


def test_the_home_tree_is_owned_after_every_file_exists(tmp_path, monkeypatch):
    # suppress_setup_assistant creates ~/Library/Preferences. Chowning the home
    # before that leaves those directories root-owned, and cfprefsd then silently
    # fails to write any preference the guest sets.
    chowned: list[str] = []
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: chowned.append(str(p)))
    root = tmp_path
    for sub in ("users", "groups"):
        (root / f"private/var/db/dslocal/nodes/Default/{sub}").mkdir(parents=True)
    import plistlib

    for name in ("admin", "staff"):
        path = root / f"private/var/db/dslocal/nodes/Default/groups/{name}.plist"
        with path.open("wb") as fh:
            plistlib.dump({"users": [], "groupmembers": []}, fh, fmt=plistlib.FMT_BINARY)
    cibpatch.patch(root, cibpatch.Account("admin", "pw"))
    prefs = root / "Users/admin/Library/Preferences"
    assert prefs.is_dir()
    assert str(prefs) in chowned, "the per-user preferences directory was never owned"


def test_the_data_volume_is_matched_on_its_real_name(monkeypatch):
    # The APFS volume is called "Data", not "Macintosh HD - Data" — that is a Finder
    # display name.
    listing = _apfs_listing("disk9s2", [{"DeviceIdentifier": "disk10s2", "Name": "Data"}])
    monkeypatch.setattr(
        cibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    assert cibpatch.data_volume("/dev/disk9") == "/dev/disk10s2"


def test_the_patcher_is_run_with_a_real_interpreter(calls, credentials, monkeypatch, tmp_path):
    # Under Nuitka sys.executable is the compiled binary, which cannot run a script.
    monkeypatch.setenv("CIB_VM_PACKER", "0")
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cib.subprocess,
        "Popen",
        lambda *a, **k: _FakeBoot(),
    )
    monkeypatch.setattr(cib.sys, "executable", "/opt/homebrew/bin/cib")  # a binary
    monkeypatch.setattr(cib.shutil, "which", lambda n: "/usr/bin/python3")
    seen = {}
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: seen.update(cmd=cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    cib.cmd_vm_create("tart", cib.VmConfig())
    assert seen["cmd"][1] == "/usr/bin/python3"
    assert not seen["cmd"][1].endswith("/cib")


def test_a_missing_sudo_credential_is_named_before_anything_is_tried(monkeypatch, tmp_path):
    # sudo prompts on its own tty and cannot ask for anything when cib runs
    # detached; that used to surface as a bare "preparing the guest failed".
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )
    with pytest.raises(cib.Failure, match="sudo -v"):
        cib._prepare_guest(cib.VmConfig(), "pw")


def test_prepare_can_be_retried_without_rebuilding(calls, credentials, monkeypatch, tmp_path):
    # Building takes half an hour; a failed patch must not cost that again.
    cib.guest_password(create=True)  # as a real build would have left it
    _fake_guest_disk(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: True)
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: (
            (None if "-n" in cmd else seen.update(cmd=cmd)) or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    cib.cmd_vm_prepare("tart", cib.VmConfig())
    assert "cibpatch.py" in " ".join(seen["cmd"])
    assert "create" not in flat(calls)


def test_a_first_boot_that_never_happened_is_not_called_built(
    calls, credentials, monkeypatch, tmp_path
):
    # Otherwise cib patches a guest that never booted and prints "Built."
    import io

    class Died(_FakeBoot):
        returncode = 2
        stderr = io.StringIO("VM is already running!")

        def poll(self):
            return 2

    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "sudo_is_cached", lambda: True)
    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    monkeypatch.setattr(cib.subprocess, "Popen", lambda *a, **k: Died())
    _fake_guest_disk(monkeypatch, tmp_path)
    with pytest.raises(cib.Failure, match="exited immediately"):
        cib.cmd_vm_create("tart", cib.VmConfig())


@pytest.mark.parametrize("name", ["../../etc/pam.d/x", "-oProxyCommand=x", "My User", "/abs"])
def test_a_dangerous_account_name_is_refused_before_root_runs(name, monkeypatch):
    # This value reaches a root-privileged patcher that builds paths from it.
    monkeypatch.setenv("CIB_VM_USER", name)
    with pytest.raises(cib.Failure, match="not a usable account name"):
        cib.validate_vm_user(cib.VmConfig().user)


def test_the_patcher_refuses_a_dangerous_name_itself(tmp_path, monkeypatch):
    # The privileged half does not trust its caller either.
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    with pytest.raises(cibpatch.PatchError, match="refusing to use"):
        cibpatch.patch(tmp_path, cibpatch.Account("../../etc/x", "pw"))


def test_the_disk_is_looked_for_where_tart_actually_puts_it(monkeypatch, tmp_path):
    # tart honours TART_HOME; looking under ~/.tart would miss the disk entirely.
    monkeypatch.setenv("TART_HOME", str(tmp_path / "elsewhere"))
    disk = tmp_path / "elsewhere" / "vms" / "chrome-vm" / "disk.img"
    disk.parent.mkdir(parents=True)
    disk.touch()
    monkeypatch.setattr(
        cib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1)
    )
    with pytest.raises(cib.Failure, match="sudo"):  # got past the disk lookup
        cib._prepare_guest(cib.VmConfig(), "pw")


def test_mount_failures_report_the_reason(monkeypatch):
    # diskutil writes its diagnosis to stderr; reporting stdout gave a bare colon.
    monkeypatch.setattr(
        cibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="Failed to find disk"),
    )
    with pytest.raises(cibpatch.PatchError, match="Failed to find disk"):
        cibpatch.mount("/dev/disk99s9")


def test_the_guest_script_survives_a_home_with_no_downloads(tmp_path):
    # The offline path creates the home itself, so Downloads may not exist — under
    # `set -e` the old two-state handling killed the script before Chrome.
    result = _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "Downloads").is_symlink()


def test_setup_installs_the_clipboard_agent_too():
    # It used to be installed only by the packer path, while cib told the user that
    # `vm setup` had done it.
    assert "tart-guest-agent" in cib.guest_install_script("pw")
    assert "--install-daemon=launchd" in cib.guest_install_script("pw")
    assert cib.GUEST_AGENT_VERSION in cib.guest_install_script("pw")


def test_the_image_volume_is_attached_with_ownership(monkeypatch):
    # macOS mounts an image volume noowners, where chown returns success and writes
    # nothing — so every file the patcher creates would stay root-owned.
    #
    # Asserted on the argv hdiutil is actually handed. Grepping the source for the
    # flag name passed even when the value beside it was wrong.
    seen = {}
    monkeypatch.setattr(
        cibpatch.subprocess,
        "run",
        lambda cmd, **kw: (
            seen.update(cmd=cmd)
            or subprocess.CompletedProcess(cmd, 0, stdout="/dev/disk9\n", stderr="")
        ),
    )
    assert cibpatch.attach(Path("/x/disk.img")) == "/dev/disk9"
    cmd = seen["cmd"]
    assert cmd[cmd.index("-owners") + 1] == "on"
    assert "-nomount" in cmd


def test_owning_the_home_never_follows_a_link(tmp_path, monkeypatch):
    # A symlink stored in the guest resolves against the HOST filesystem, so
    # following one here would let the guest steer a root chown at any host path.
    calls_seen: list[tuple] = []
    monkeypatch.setattr(
        cibpatch.os,
        "chown",
        lambda p, u, g, **kw: calls_seen.append((str(p), kw.get("follow_symlinks"))),
    )
    home = tmp_path / "Users/admin"
    home.mkdir(parents=True)
    (home / "real").write_text("x")
    (home / "escape").symlink_to("/etc")
    cibpatch.own_home(tmp_path, cibpatch.Account("admin", "pw"))
    assert calls_seen, "nothing was owned"
    assert all(kw is False for _, kw in calls_seen), "a chown followed links"
    assert not any(path == "/etc" for path, _ in calls_seen)


def test_a_symlinked_home_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(cibpatch.os, "chown", lambda *a, **k: None)
    (tmp_path / "Users").mkdir()
    (tmp_path / "Users/admin").symlink_to("/")
    with pytest.raises(cibpatch.PatchError, match="symlink"):
        cibpatch.own_home(tmp_path, cibpatch.Account("admin", "pw"))


def test_the_disk_is_sized_when_it_is_created(calls, credentials, monkeypatch, tmp_path):
    # tart create installs macOS onto its default 50 GB disk; growing the image
    # afterwards leaves the partitions where the installer put them.
    monkeypatch.setenv("CIB_VM_DISK", "120")
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    monkeypatch.setattr(cib.subprocess, "Popen", lambda *a, **k: _FakeBoot())
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(
        cib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0)
    )
    cib.cmd_vm_create("tart", cib.VmConfig())
    out = flat(calls)
    assert "--disk-size=120" in out
    assert "set chrome-vm --cpu" in out and "--disk-size" not in out.split("set chrome-vm")[1]


def test_prepare_refuses_a_running_guest(calls, credentials, monkeypatch):
    # Patching a mounted disk that the guest is also writing means two writers.
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: True)
    monkeypatch.setattr(cib, "vm_running", lambda *a, **k: True)
    with pytest.raises(cib.Failure, match="cib vm down"):
        cib.cmd_vm_prepare("tart", cib.VmConfig())


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
        cib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=_hitoolbox([_SWISS])),
    )
    assert cib.host_keyboard_layout() == (19, "Swiss German")


def test_an_input_method_is_not_mistaken_for_a_keyboard_layout(monkeypatch):
    # Press-And-Hold sits in the same list and carries no layout at all.
    sources = [
        {"InputSourceKind": "Non Keyboard Input Method", "Bundle ID": "com.apple.PressAndHold"},
        _SWISS,
    ]
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=_hitoolbox(sources)),
    )
    assert cib.host_keyboard_layout() == (19, "Swiss German")


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
        cib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, returncode, stdout=stdout),
    )
    assert cib.host_keyboard_layout() == cib.DEFAULT_KEYBOARD


def test_the_layout_is_written_where_the_guest_reads_it(tmp_path):
    # Selecting a layout that is not also enabled leaves the guest on U.S.
    import plistlib

    cibpatch.set_keyboard_layout(
        tmp_path, cibpatch.Account("admin", "pw"), cibpatch.Keyboard(19, "Swiss German")
    )
    path = tmp_path / "Users/admin/Library/Preferences/com.apple.HIToolbox.plist"
    record = plistlib.loads(path.read_bytes())
    assert record["AppleEnabledInputSources"] == [_SWISS]
    assert record["AppleSelectedInputSources"] == [_SWISS]


def test_the_hosts_layout_reaches_the_patcher(credentials, monkeypatch, tmp_path):
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(cib, "host_keyboard_layout", lambda: (19, "Swiss German"))
    seen = {}
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: (
            (None if "-n" in cmd else seen.update(cmd=cmd)) or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    cib._prepare_guest(cib.VmConfig(), "pw")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--keyboard-id") + 1] == "19"
    assert cmd[cmd.index("--keyboard-name") + 1] == "Swiss German"


def test_patching_leaves_the_guest_on_the_layout_it_was_told(tmp_path, monkeypatch):
    import plistlib

    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    for sub in ("users", "groups"):
        (tmp_path / f"private/var/db/dslocal/nodes/Default/{sub}").mkdir(parents=True)
    for name in ("admin", "staff"):
        path = tmp_path / f"private/var/db/dslocal/nodes/Default/groups/{name}.plist"
        with path.open("wb") as fh:
            plistlib.dump({"users": [], "groupmembers": []}, fh, fmt=plistlib.FMT_BINARY)
    cibpatch.patch(tmp_path, cibpatch.Account("admin", "pw"), cibpatch.Keyboard(19, "Swiss German"))
    record = plistlib.loads(
        (tmp_path / "Users/admin/Library/Preferences/com.apple.HIToolbox.plist").read_bytes()
    )
    assert record["AppleSelectedInputSources"] == [_SWISS]


# --- pins, managers, and messages that pointed the wrong way -------------------


def test_both_tart_guest_agent_pins_move_together():
    # Each pin is updated by its own Renovate manager. If one stops matching, the
    # two install paths quietly end up on different agents.
    root = Path(cib.__file__).resolve().parent
    template = (root / "packer" / "chrome-vm.pkr.hcl").read_text()
    pinned = re.search(r'variable "guest_agent_version".*?default\s*=\s*"([^"]+)"', template, re.S)
    assert pinned, "the packer template no longer pins a guest agent version"
    assert pinned.group(1) == cib.GUEST_AGENT_VERSION


def test_every_renovate_marker_anywhere_has_a_manager_that_matches_it():
    # Generalised from cib.py alone: the packer template carries markers too, and a
    # marker with no manager reads as configured while updating nothing.
    import json

    root = Path(cib.__file__).resolve().parent
    config = json.loads((root / "renovate.json").read_text())
    by_file: dict[str, list[str]] = {}
    for manager in config["customManagers"]:
        target = manager["managerFilePatterns"][0].strip("/^$").replace("\\", "")
        by_file.setdefault(target, []).extend(manager["matchStrings"])
    marked = {
        str(path.relative_to(root))
        for path in (root / "cib.py", root / "packer" / "chrome-vm.pkr.hcl")
        if "# renovate:" in path.read_text()
    }
    assert marked, "no renovate markers found at all — has the layout changed?"
    for name in marked:
        source = (root / name).read_text()
        covered = {
            match.group(0).splitlines()[0].strip()
            for pattern in by_file.get(name, [])
            for match in re.finditer(re.sub(r"\(\?<(?![=!])", "(?P<", pattern), source)
        }
        markers = {ln.strip() for ln in re.findall(r"^\s*# renovate:.*$", source, flags=re.M)}
        assert markers == covered, f"{name}: {markers - covered} matched by no manager"


def test_every_renovate_marker_in_cib_has_a_manager_that_matches_it():
    # A marker with no manager reads as configured and updates nothing: the
    # tart-guest-agent pin sat unmanaged behind one for seven review rounds.
    import json

    root = Path(cib.__file__).resolve().parent
    config = json.loads((root / "renovate.json").read_text())
    source = (root / "cib.py").read_text()
    patterns = [
        pattern
        for manager in config["customManagers"]
        if manager["managerFilePatterns"] == ["/^cib\\.py$/"]
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
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: True)
    cib.cmd_vm_create("tart", cib.VmConfig())
    assert "cib vm prepare" in capsys.readouterr().out


def test_a_guest_that_will_not_stop_is_pointed_at_prepare(calls, credentials, monkeypatch):
    # It has already been killed here, so telling the user to stop it is advice
    # they cannot act on.
    class Stuck(_FakeBoot):
        def wait(self, timeout=None):
            if timeout:
                raise subprocess.TimeoutExpired("tart", timeout)
            return 0

    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "sudo_is_cached", lambda: True)
    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    monkeypatch.setattr(cib.subprocess, "Popen", lambda *a, **k: Stuck())
    with pytest.raises(cib.Failure, match="cib vm prepare"):
        cib.cmd_vm_create("tart", cib.VmConfig())


def test_the_packer_fallback_is_offered_with_the_delete_that_makes_it_work(monkeypatch, tmp_path):
    # 'vm create' on an existing VM only reports that it exists, so suggesting the
    # fallback without the delete suggests a no-op.
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(cib.Path, "exists", lambda self: "cibpatch" not in str(self))
    with pytest.raises(cib.Failure, match="cib vm delete"):
        cib._prepare_guest(cib.VmConfig(), "pw")


# --- the disk layer, where every real failure so far has happened --------------


def test_the_disk_is_put_back_even_when_patching_fails(monkeypatch):
    # A half-finished run must never leave the guest's disk attached to the host.
    undone: list[tuple[str, str]] = []
    monkeypatch.setattr(cibpatch, "attach", lambda disk: "/dev/disk9")
    monkeypatch.setattr(cibpatch, "data_volume", lambda device: "/dev/disk9s1")
    monkeypatch.setattr(cibpatch, "mount", lambda volume: Path("/Volumes/Data"))
    monkeypatch.setattr(cibpatch, "unmount", lambda volume: undone.append(("unmount", volume)))
    monkeypatch.setattr(cibpatch, "detach", lambda device: undone.append(("detach", device)))
    monkeypatch.setattr(
        cibpatch, "patch", lambda *a, **k: (_ for _ in ()).throw(cibpatch.PatchError("no"))
    )
    with pytest.raises(cibpatch.PatchError):
        cibpatch.prepare(Path("/x/disk.img"), cibpatch.Account("admin", "pw"))
    assert undone == [("unmount", "/dev/disk9s1"), ("detach", "/dev/disk9")]


def test_a_disk_that_will_not_attach_is_reported_with_its_reason(monkeypatch):
    monkeypatch.setattr(
        cibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="no such file"),
    )
    with pytest.raises(cibpatch.PatchError, match="no such file"):
        cibpatch.attach(Path("/x/disk.img"))


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

    monkeypatch.setattr(cibpatch.subprocess, "run", fake_run)
    with pytest.raises(cibpatch.PatchError, match="without ownership"):
        cibpatch.mount("/dev/disk9s1")


def test_the_patcher_passes_the_layout_it_was_given(monkeypatch):
    import io

    seen = {}
    typed = "on-stdin-not-argv"
    monkeypatch.setattr(cibpatch, "prepare", lambda d, a, k: seen.update(disk=d, account=a, kb=k))
    monkeypatch.setattr(cibpatch.sys, "stdin", io.StringIO(typed + "\n"))
    cibpatch.main(
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
    assert seen["kb"] == cibpatch.Keyboard(19, "Swiss German")
    # Never in argv, so it is never in the process list.
    assert seen["account"].password == typed


def test_the_patcher_defaults_to_us_when_it_is_told_no_layout(monkeypatch):
    import io

    seen = {}
    monkeypatch.setattr(cibpatch, "prepare", lambda d, a, k: seen.update(kb=k))
    monkeypatch.setattr(cibpatch.sys, "stdin", io.StringIO("pw\n"))
    cibpatch.main(["--disk", "/x/disk.img", "--user", "admin"])
    assert seen["kb"] == cibpatch.Keyboard()


def test_the_patcher_refuses_to_run_without_a_password(monkeypatch):
    # An empty password would produce an account nothing can log in to.
    import io

    monkeypatch.setattr(cibpatch.sys, "stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit, match="no password"):
        cibpatch.main(["--disk", "/x/disk.img", "--user", "admin"])


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

    with pytest.raises(cibpatch.PatchError, match="symlink"):
        cibpatch.guest_path(root, f"{planted}/precious")
    assert witness.read_text() == "host file", "a host file was written through the link"


def test_the_whole_patch_refuses_a_guest_that_planted_a_link(tmp_path, monkeypatch):
    # End to end: patch() must stop, not write half the guest and then notice.
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    target = tmp_path / "host"
    target.mkdir()
    (target / "AppleSetupDone-decoy").write_text("host file")
    (root / "private/etc").mkdir(parents=True, exist_ok=True)
    (root / "private/etc").rmdir()
    (root / "private/etc").symlink_to(target)
    with pytest.raises(cibpatch.PatchError, match="symlink"):
        cibpatch.patch(root, cibpatch.Account("admin", "pw"))
    assert sorted(p.name for p in target.iterdir()) == ["AppleSetupDone-decoy"]


def test_a_guest_volume_with_no_links_still_patches(tmp_path, monkeypatch):
    # The guard must not refuse the ordinary case it was added to protect.
    import plistlib

    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    cibpatch.patch(root, cibpatch.Account("admin", "pw"), cibpatch.Keyboard(19, "Swiss German"))
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
        cibpatch.KCPASSWORD_KEY
    )
    assert cibpatch.kcpassword("test") == bytes.fromhex("09ec2157d2bcddeaa3b91f")


def test_the_padding_is_observable_for_a_password_the_key_length_divides():
    # The documented failure ("macOS reads past the password") only happens when the
    # length is a multiple of 11, which is the case the old assertion could not see.
    encoded = cibpatch.kcpassword("elevenchars")
    assert len(encoded) == 22, "an 11-character password must still get its terminator"
    assert encoded[11] == cibpatch.KCPASSWORD_KEY[0], "byte 11 is the NUL, XOR-ed"


def test_autologin_writes_the_keys_loginwindow_actually_reads(tmp_path):
    import plistlib

    account = cibpatch.Account("admin", "pw")
    cibpatch.enable_autologin(tmp_path, account)
    record = plistlib.loads(
        (tmp_path / "Library/Preferences/com.apple.loginwindow.plist").read_bytes()
    )
    # Misspell either and loginwindow ignores the setting: the guest stops at a login
    # window and the generated 24-character password has to be typed by hand.
    assert record["autoLoginUser"] == "admin"
    assert record["autoLoginUserUID"] == 501
    kc = tmp_path / "private/etc/kcpassword"
    assert kc.read_bytes() == cibpatch.kcpassword("pw")
    assert kc.stat().st_mode & 0o777 == 0o600, "the guest's password must not be world-readable"


def test_remote_login_is_recorded_where_launchd_looks(tmp_path):
    import plistlib

    cibpatch.enable_remote_login(tmp_path)
    record = plistlib.loads(
        (tmp_path / "private/var/db/com.apple.xpc.launchd/disabled.plist").read_bytes()
    )
    # False means "not disabled". True, or a missing key, and 'cib vm setup' can
    # never reach the guest.
    assert record["com.openssh.sshd"] is False


def test_the_two_keyboard_defaults_are_the_same_fact_twice():
    # cib decides the fallback and cibpatch carries its own; a test that compares
    # each against itself would not notice them drifting apart.
    assert cib.DEFAULT_KEYBOARD == (0, "U.S.")
    assert (cibpatch.Keyboard().layout_id, cibpatch.Keyboard().name) == cib.DEFAULT_KEYBOARD


def _run_desktop_script(home, resolution: str, chrome_running: bool):
    """Execute DESKTOP_SCRIPT the way the engine does, with recording fakes."""
    bin_dir = home / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "xrandr").write_text(f'#!/bin/sh\necho "$@" >> {home}/xrandr.log\n')
    (bin_dir / "pgrep").write_text(f"#!/bin/sh\nexit {0 if chrome_running else 1}\n")
    (bin_dir / "nohup").write_text(f'#!/bin/sh\necho "$@" >> {home}/launch.log\n')
    for name in ("xrandr", "pgrep", "nohup"):
        (bin_dir / name).chmod(0o755)
    subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", cib.DESKTOP_SCRIPT],
        env={"HOME": str(home), "RES": resolution, "PATH": f"{bin_dir}:/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    # Chrome is launched with `&`, so the shell returns before the child has run.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not (home / "launch.log").exists():
        if chrome_running:
            break  # nothing will ever be launched, so do not wait the full timeout
        time.sleep(0.02)
    if chrome_running:
        time.sleep(0.2)  # long enough for a launch that should not happen to appear

    def read(name: str) -> str:
        path = home / name
        return path.read_text() if path.exists() else ""

    return read("xrandr.log"), read("launch.log")


def test_the_desktop_script_launches_chrome_on_its_own_profile():
    # Only ever string-grepped before, so a wrong profile directory (Chrome starts
    # on a throwaway profile) or a missing display would have passed.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        mode, launch = _run_desktop_script(Path(tmp), "1920x1200", chrome_running=False)
    assert "-s 1920x1200" in mode
    assert cib.CHROME_BIN in launch
    assert f"--user-data-dir={cib.PROFILE_DIR}" in launch
    assert "--no-sandbox" in launch


def test_the_desktop_script_does_not_start_a_second_chrome():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        _, launch = _run_desktop_script(Path(tmp), "1920x1200", chrome_running=True)
    assert launch == "", "a second Chrome over a live one loses the first one's tabs"


def test_the_desktop_script_skips_xrandr_when_no_mode_was_asked_for():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        mode, launch = _run_desktop_script(Path(tmp), "", chrome_running=False)
    assert mode == "", "an empty CIB_RESOLUTION means follow the browser window"
    assert cib.CHROME_BIN in launch


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
        cib,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=""),
    )
    assert cib.container_running("podman", cib.Config()) is expected


def test_ui_status_reports_the_code_and_none_when_nothing_answers(monkeypatch):
    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(cib.urllib.request, "urlopen", lambda *a, **k: _Response())
    assert cib.ui_status(cib.Config()) == 200
    assert cib.ui_is_up(cib.Config()) is True

    def refused(*a, **k):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(cib.urllib.request, "urlopen", refused)
    assert cib.ui_status(cib.Config()) is None
    assert cib.ui_is_up(cib.Config()) is False
