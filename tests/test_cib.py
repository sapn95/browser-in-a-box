"""Tests for cib.py.

Nothing here touches a real container engine: `run` is replaced with a recorder,
so the actual command construction is asserted instead of being described.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cib


@pytest.fixture
def calls(monkeypatch):
    """Record every engine invocation and return success by default."""
    recorded: list[list[str]] = []

    def fake_run(engine, *args, check=True, capture=False):
        recorded.append([engine, *args])
        return subprocess.CompletedProcess([engine, *args], 0, stdout="", stderr="")

    monkeypatch.setattr(cib, "run", fake_run)
    monkeypatch.setattr(cib, "find_engine", lambda: "podman")
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
    assert ZERO not in out
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
    assert cib.vm_run_args(cib.VmConfig()) == ["run", "--net-bridged=en0", "chrome-vm"]


def test_the_vm_network_mode_and_interface_are_overridable(monkeypatch):
    monkeypatch.setenv("CIB_VM_INTERFACE", "en1")
    assert "--net-bridged=en1" in cib.vm_run_args(cib.VmConfig())
    monkeypatch.setenv("CIB_VM_NET", "shared")
    assert cib.vm_run_args(cib.VmConfig()) == ["run", "chrome-vm"]
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

    def fake_run(engine, *args, check=True, capture=False):
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
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    with pytest.raises(cib.Failure, match="Setup Assistant"):
        cib.vm_ip("tart", cib.VmConfig())


def test_the_ssh_command_does_not_pin_a_host_key():
    cmd = cib.ssh_command(cib.VmConfig(), "192.168.1.50")
    assert cmd[0] == "ssh"
    assert "StrictHostKeyChecking=no" in cmd
    assert cmd[-1] == "admin@192.168.1.50"


def test_the_ssh_user_is_overridable(monkeypatch):
    monkeypatch.setenv("CIB_VM_USER", "sapn")
    assert cib.ssh_command(cib.VmConfig(), "10.0.0.1")[-1] == "sapn@10.0.0.1"


def test_setup_installs_chrome_and_is_idempotent():
    assert "googlechrome.dmg" in cib.GUEST_INSTALL_CHROME
    assert "already installed" in cib.GUEST_INSTALL_CHROME
    assert cib.GUEST_INSTALL_CHROME.startswith("set -eu")


def test_setup_names_the_one_switch_the_guest_needs(monkeypatch, capsys):
    monkeypatch.setattr(cib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(cib, "guest_ssh", lambda *a, **k: 255)
    with pytest.raises(cib.Failure, match="Remote Login"):
        cib.cmd_vm_setup("tart", cib.VmConfig())


def test_setup_passes_the_install_script_to_the_guest(monkeypatch):
    seen = {}
    monkeypatch.setattr(cib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(
        cib, "guest_ssh", lambda vm, ip, script=None: seen.update(script=script) or 0
    )
    cib.cmd_vm_setup("tart", cib.VmConfig())
    assert seen["script"] == cib.GUEST_INSTALL_CHROME


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
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "find_packer", lambda: "packer")
    cib.cmd_vm_create("tart", cib.VmConfig())
    out = flat(calls)
    assert "packer build" in out
    assert cib.PACKER_TEMPLATE in out
    assert f"password={credentials.read_text().strip()}" in out
    assert "memory_gb=8" in out  # 8192 MB, passed to packer in GB


def test_a_missing_packer_points_at_the_install_command(monkeypatch):
    monkeypatch.setattr(cib.shutil, "which", lambda name: None)
    with pytest.raises(cib.Failure, match="brew install packer"):
        cib.find_packer()


def test_the_template_drives_setup_assistant_and_keeps_gatekeeper():
    template = (Path(__file__).resolve().parents[1] / cib.PACKER_TEMPLATE).read_text()
    assert "boot_command" in template
    assert "switzerland" in template
    assert "SwissGerman" in template
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
