"""Tests for cib.py.

Nothing here touches a real container engine: `run` is replaced with a recorder,
so the actual command construction is asserted instead of being described.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cib


@pytest.fixture(autouse=True)
def isolate_secrets(tmp_path, monkeypatch):
    """No test may reach the real ~/.config/chrome-in-a-box.

    Found the hard way, during a real build: running `pytest` deleted a live VM's
    password and both key pairs, because two delete tests called cmd_vm_delete
    against the module-level paths. Per-test fixtures were not enough — this has to
    hold for every test, including ones written later.
    """
    home = tmp_path / "isolated-home"
    secrets_dir = home / ".config" / "chrome-in-a-box" / "chrome-vm"
    secrets_dir.mkdir(parents=True)
    monkeypatch.setattr(cib.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(cib, "SECRETS", secrets_dir)
    monkeypatch.setattr(cib, "CREDENTIALS", secrets_dir / "vm-credentials")
    monkeypatch.setattr(cib, "VM_KEY", secrets_dir / "vm-key")
    monkeypatch.setattr(cib, "VM_HOST_KEY", secrets_dir / "vm-host-key")
    monkeypatch.setattr(cib, "KNOWN_HOSTS", secrets_dir / "vm-known-hosts")
    # And no test may start a real VM. start_detached uses subprocess.Popen
    # directly, so a test that replaces only `cib.run` would spawn tart for real and
    # then sit in wait_for_guest for five minutes. Tests that care replace these.
    real = SimpleNamespace(start_detached=cib.start_detached, wait_for_guest=cib.wait_for_guest)
    monkeypatch.setattr(cib, "start_detached", lambda tart, vm: _FakeBoot())
    monkeypatch.setattr(cib, "wait_for_guest", lambda tart, vm, boot: "192.168.1.50")
    # Handed back, so the two tests that exercise the real ones can ask for them
    # rather than reaching around the guard.
    return real


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
        ("resolution", "2560x1600", "not one of the modes KasmVNC"),
        ("resolution", "1920x1201", "not one of the modes KasmVNC"),
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


def test_the_ssh_command_verifies_the_host_key_it_planted(credentials):
    # It used to be StrictHostKeyChecking=no against /dev/null, which accepts any
    # peer answering on the guest's address — and the script sent over that
    # connection carries the guest's password for sudo.
    cmd = cib.ssh_command(cib.VmConfig(), "192.168.1.50")
    assert cmd[0].endswith("ssh")
    assert "StrictHostKeyChecking=yes" in cmd
    assert "StrictHostKeyChecking=no" not in cmd
    assert f"UserKnownHostsFile={cib.KNOWN_HOSTS}" in cmd
    assert "PasswordAuthentication=no" in cmd
    assert cmd[cmd.index("-i") + 1] == str(cib.VM_KEY)
    assert cmd[-1] == "admin@192.168.1.50"


def test_the_known_hosts_entry_pins_the_guests_key_at_any_address(credentials):
    # The guest's address changes with every lease, and this file is used for
    # nothing but connections to that one guest, so a wildcard is the pin.
    cib.ensure_vm_keys()
    entry = cib.KNOWN_HOSTS.read_text().strip()
    assert entry.startswith("* ssh-ed25519 ")
    assert entry.endswith(cib.VM_HOST_KEY.with_suffix(".pub").read_text().strip())


def test_the_keys_are_generated_once_and_kept(tmp_path, monkeypatch):
    # Regenerating them would lock cib out of the guest it already built.
    monkeypatch.setattr(cib, "VM_KEY", tmp_path / "vm-key")
    monkeypatch.setattr(cib, "VM_HOST_KEY", tmp_path / "vm-host-key")
    monkeypatch.setattr(cib, "KNOWN_HOSTS", tmp_path / "vm-known-hosts")
    cib.ensure_vm_keys()  # real ssh-keygen
    assert (tmp_path / "vm-key").exists()
    first = (tmp_path / "vm-key").read_bytes()
    assert (tmp_path / "vm-key.pub").read_text().startswith("ssh-ed25519 ")
    cib.ensure_vm_keys()
    assert (tmp_path / "vm-key").read_bytes() == first


def test_a_keygen_that_produced_nothing_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(cib, "VM_KEY", tmp_path / "vm-key")
    monkeypatch.setattr(cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0))
    with pytest.raises(cib.Failure, match="did not produce"):
        cib.ensure_vm_keys()


def test_the_ssh_user_is_overridable(monkeypatch):
    monkeypatch.setenv("CIB_VM_USER", "sapn")
    assert cib.ssh_command(cib.VmConfig(), "10.0.0.1")[-1] == "sapn@10.0.0.1"


def test_setup_installs_chrome_and_is_idempotent():
    assert "googlechrome.dmg" in cib.guest_install_script("pw")
    assert "already installed" in cib.guest_install_script("pw")
    assert cib.guest_install_script("pw").startswith("set -eu")


def test_setup_points_at_prepare_not_at_a_switch_it_already_turned_on(
    credentials, monkeypatch, capsys
):
    # The offline build enables Remote Login itself, so telling the user to go and
    # turn it on sent them looking for a setting that is already set.
    cib.guest_password(create=True)
    monkeypatch.setattr(cib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(cib, "guest_ssh", lambda *a, **k: 255)
    with pytest.raises(cib.Failure, match="cib vm prepare") as caught:
        cib.cmd_vm_setup("tart", cib.VmConfig())
    assert "turn on" not in str(caught.value)


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
    assert seen["script"] == cib.guest_install_script(password, cib.host_time_zone()[0])
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
    # All four live together under ~/.config/chrome-in-a-box and are module-level,
    # so redirecting Path.home() afterwards would not move them.
    monkeypatch.setattr(cib, "SECRETS", tmp_path)
    monkeypatch.setattr(cib, "CREDENTIALS", tmp_path / "vm-credentials")
    monkeypatch.setattr(cib, "VM_KEY", tmp_path / "vm-key")
    monkeypatch.setattr(cib, "VM_HOST_KEY", tmp_path / "vm-host-key")
    monkeypatch.setattr(cib, "KNOWN_HOSTS", tmp_path / "vm-known-hosts")
    # Stand-ins rather than real ssh-keygen output: these tests replace `run`, so a
    # real keygen would never happen. ensure_vm_keys() then only rewrites
    # known_hosts, which is the part they care about.
    for key in (cib.VM_KEY, cib.VM_HOST_KEY):
        key.write_text(f"PRIVATE {key.name}\n")
        key.with_suffix(".pub").write_text(f"ssh-ed25519 AAAAC3Nz-{key.name} cib\n")
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
        ("CIB_VM_CPUS", "0", "at least 2"),
        ("CIB_VM_MEMORY", "512", "at least 4096"),
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
for dst in "$@"; do :; done
mkdir -p "$(dirname "$dst")"
printf '#!/bin/sh\\nexit 0\\n' > "$dst"
chmod 0755 "$dst"
"""


def _run_guest_script(script: str, home, share_exists: bool, extra_bin=None):
    """Execute the guest script the way ssh would: /bin/sh -e, with fakes."""
    bin_dir = home / "fakebin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    if extra_bin:
        for tool in extra_bin.iterdir():
            (bin_dir / tool.name).write_text(tool.read_text())
            (bin_dir / tool.name).chmod(0o755)
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
    body = body.replace(cib.AGENT_BIN, str(home / "agent"))
    body = body.replace(cib.AGENT_PLIST_PATH, str(home / "agent.plist"))
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


def test_the_guest_script_fails_when_the_share_is_missing(tmp_path):
    # It used to warn, install Chrome, exit 0 — and cib then printed "Done".
    (tmp_path / "Downloads").mkdir()
    result = _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=False)
    assert result.returncode != 0
    assert "not mounted" in result.stderr


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


def test_the_template_no_longer_carries_a_second_copy_of_the_install():
    # It kept `--install-daemon=launchd` for three rounds after the guest script's
    # copy was fixed, because there were two copies. Chrome and the agent are now
    # installed in one place, by 'cib vm setup', for both build paths.
    template = (Path(cib.__file__).resolve().parent / "packer" / "chrome-vm.pkr.hcl").read_text()
    assert "--install-daemon" not in template
    assert "googlechrome.dmg" not in template
    assert "tart-guest-agent" not in template


def test_the_template_installs_the_key_cib_will_connect_with():
    template = (Path(cib.__file__).resolve().parent / "packer" / "chrome-vm.pkr.hcl").read_text()
    assert "authorized_keys" in template
    assert "ssh_host_ed25519_key" in template
    for name in ("authorized_key", "host_private_key", "host_public_key"):
        assert f'variable "{name}"' in template, f"{name} is used but never declared"


def test_the_packer_path_generates_and_passes_the_keys(calls, credentials, monkeypatch):
    # Without them the `cib vm setup` the build recommends could never connect.
    monkeypatch.setenv("CIB_VM_PACKER", "1")
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "find_packer", lambda: "/usr/bin/packer")
    seen = {}
    monkeypatch.setattr(
        cib,
        "run",
        lambda engine, *a, **k: (
            seen.update(env=k.get("env") or seen.get("env"))
            or subprocess.CompletedProcess([], 0, stdout="", stderr="")
        ),
    )
    cib.cmd_vm_create("tart", cib.VmConfig())
    env = seen["env"]
    assert env["PKR_VAR_authorized_key"].startswith("ssh-ed25519 ")
    assert env["PKR_VAR_host_private_key"]
    assert env["PKR_VAR_host_public_key"].startswith("ssh-ed25519 ")
    assert cib.KNOWN_HOSTS.exists(), "the host key has to be pinned for cib to use it"


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
        # Only the patcher: create now goes on to boot the guest and ssh into it, so
        # "the last call" is no longer the one under test.
        lambda cmd, **kw: (
            (
                seen.update(cmd=cmd, stdin=kw.get("input"))
                if any("cibpatch" in str(c) for c in cmd)
                else None
            )
            or subprocess.CompletedProcess(cmd, 0)
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
        lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1 if any("cibpatch" in str(c) for c in cmd) else 0
        ),
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
        # Only the patcher: create now goes on to boot the guest and ssh into it, so
        # "the last call" is no longer the one under test.
        lambda cmd, **kw: (
            (seen.update(cmd=cmd) if any("cibpatch" in str(c) for c in cmd) else None)
            or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    cib.cmd_vm_create("tart", cib.VmConfig())
    assert seen["cmd"][:2] == ["/usr/bin/sudo", "-n"]
    assert seen["cmd"][2] == "/usr/bin/python3"
    assert not seen["cmd"][2].endswith("/cib")


def test_a_missing_sudo_credential_is_named_before_anything_is_tried(
    credentials, monkeypatch, tmp_path
):
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
            (seen.update(cmd=cmd) if any("cibpatch" in str(c) for c in cmd) else None)
            or subprocess.CompletedProcess(cmd, 0)
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
    monkeypatch.setattr(cib, "find_guest_python", lambda: "/usr/bin/python3")
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


def test_the_disk_is_looked_for_where_tart_actually_puts_it(credentials, monkeypatch, tmp_path):
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


def test_setup_installs_the_clipboard_agent_too():
    # It used to be installed only by the packer path, while cib told the user that
    # `vm setup` had done it.
    assert "tart-guest-agent" in cib.guest_install_script("pw")
    assert cib.AGENT_PLIST_PATH in cib.guest_install_script("pw")
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
            (seen.update(cmd=cmd) if any("cibpatch" in str(c) for c in cmd) else None)
            or subprocess.CompletedProcess(cmd, 0)
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


def test_the_guest_agent_is_pinned_in_exactly_one_place():
    # It used to be pinned twice, in cib.py and in the packer template, with a test
    # asserting the two agreed. The template no longer installs the agent at all,
    # so the twin — and the way it could drift — is gone.
    root = Path(cib.__file__).resolve().parent
    template = (root / "packer" / "chrome-vm.pkr.hcl").read_text()
    assert "guest_agent_version" not in template
    assert re.search(r'^GUEST_AGENT_VERSION = "[\d.]+"$', (root / "cib.py").read_text(), re.M)


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
    monkeypatch.setattr(cib, "find_guest_python", lambda: "/usr/bin/python3")
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
    monkeypatch.setattr(
        cibpatch, "prepare", lambda d, a, k, ks: seen.update(disk=d, account=a, kb=k, keys=ks)
    )
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
    monkeypatch.setattr(cibpatch, "prepare", lambda d, a, k, ks: seen.update(kb=k))
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

    class _Opener:
        def __init__(self, answer):
            self.answer = answer

        def open(self, *a, **k):
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

    monkeypatch.setattr(cib.urllib.request, "build_opener", lambda *h: _Opener(_Response()))
    assert cib.ui_status(cib.Config()) == 200
    assert cib.ui_is_up(cib.Config()) is True

    monkeypatch.setattr(
        cib.urllib.request,
        "build_opener",
        lambda *h: _Opener(urllib.error.URLError("connection refused")),
    )
    assert cib.ui_status(cib.Config()) is None
    assert cib.ui_is_up(cib.Config()) is False


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
    monkeypatch.setattr(cib.urllib.request, "build_opener", fake_build_opener)
    cib.ui_status(cib.Config())
    proxy = next(h for h in handlers["given"] if isinstance(h, cib.urllib.request.ProxyHandler))
    assert proxy.proxies == {}, "an empty proxy map is what bypasses the environment"


# --- what round 11 confirmed and the last commit did not close -----------------


def test_a_missing_patcher_is_named_before_the_download_starts(credentials, monkeypatch, tmp_path):
    # It used to be found only in the patch step, so a build that could never
    # finish still downloaded ~15 GB and installed macOS first.
    calls: list[str] = []
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "sudo_is_cached", lambda: True)
    monkeypatch.setattr(cib, "find_guest_python", lambda: "/usr/bin/python3")
    monkeypatch.setattr(cib, "PATCHER", tmp_path / "not-shipped" / "cibpatch.py")
    monkeypatch.setattr(cib, "run", lambda *a, **k: calls.append(a) or None)
    with pytest.raises(cib.Failure, match="patcher is missing"):
        cib.cmd_vm_create("tart", cib.VmConfig())
    assert calls == [], "nothing may be downloaded before the check"


def test_a_missing_sudo_credential_is_named_before_the_download_starts(
    credentials, monkeypatch, tmp_path
):
    calls: list[str] = []
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "sudo_is_cached", lambda: False)
    monkeypatch.setattr(cib, "run", lambda *a, **k: calls.append(a) or None)
    with pytest.raises(cib.Failure, match="Nothing has been downloaded yet"):
        cib.cmd_vm_create("tart", cib.VmConfig())
    assert calls == []


def test_a_second_account_on_the_same_uid_is_refused(tmp_path, monkeypatch):
    # uid is fixed at 501, so 'CIB_VM_USER=bob cib vm prepare' over a guest built as
    # 'admin' would give bob full access to admin's home, Chrome profile and login
    # keychain — and nothing said so.
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    cibpatch.patch(root, cibpatch.Account("admin", "pw"))
    with pytest.raises(cibpatch.PatchError, match="already has an account 'admin'"):
        cibpatch.patch(root, cibpatch.Account("bob", "pw"))


def test_preparing_the_same_account_twice_is_still_allowed(tmp_path, monkeypatch):
    # 'cib vm prepare' after a failed patch is the documented retry, so the guard
    # must not turn it into an error.
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    cibpatch.patch(root, cibpatch.Account("admin", "pw"))
    cibpatch.patch(root, cibpatch.Account("admin", "pw"))  # must not raise


def test_logs_reports_an_engine_failure_as_a_failure(monkeypatch):
    # It used to exit 0 whatever the engine did, so `cib box logs > out.txt || handle`
    # never fired and out.txt was silently empty.
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    with pytest.raises(cib.Failure, match="cib box status"):
        cib.cmd_logs("podman", cib.Config())


def test_logs_stays_quiet_when_the_engine_is_happy(monkeypatch):
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    cib.cmd_logs("podman", cib.Config())  # must not raise


def test_the_guest_script_keeps_its_scratch_space_out_of_tmp():
    # /tmp in the guest is writable by every account there, so a staged Chrome.app
    # could be swapped between the copy and the move into /Applications. It also
    # made two concurrent test runs share host paths and delete each other's.
    script = cib.guest_install_script("pw")
    body = "\n".join(ln for ln in script.split("\n") if not ln.lstrip().startswith("#"))
    # Spelled this way so the assertion itself is not a hardcoded temp path.
    assert not re.search(r"/tm[p]/", body)
    assert 'CIB_WORK="$HOME/.cache/cib"' in body
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
    result = _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=False)
    assert result.returncode != 0, "a missing share is still a failure"
    assert not (tmp_path / ".cache" / "cib").exists()


# --- the clipboard agent, which was started by a flag that does not exist -------


def test_the_clipboard_agent_is_started_by_launchd_not_an_invented_flag():
    # `--install-daemon=launchd` was never a real flag; the agent's own README says
    # it is started from a launchd plist. So it was downloaded, installed, and never
    # ran — and copy-paste, which the generated password exists for, never worked.
    script = cib.guest_install_script("pw")
    assert "--install-daemon" not in script
    assert "--run-agent" in script, "the session agent is the one that sees the pasteboard"
    assert "--run-daemon" not in script, "a root daemon cannot reach the pasteboard"
    assert cib.AGENT_PLIST_PATH in script
    assert "launchctl bootstrap" in script


def test_the_agent_plist_is_a_plist_launchd_will_accept():
    import plistlib

    record = plistlib.loads(cib.AGENT_PLIST.encode())
    assert record["Label"] == cib.AGENT_LABEL
    assert record["ProgramArguments"] == [cib.AGENT_BIN, "--run-agent"]
    assert record["RunAtLoad"] is True
    assert record["KeepAlive"] is True


def test_the_agent_plist_survives_the_shell_that_writes_it(tmp_path):
    # It is written with printf from a single-quoted string; an unescaped quote or a
    # mangled newline would install a plist launchd silently ignores.
    import plistlib

    script = cib.guest_install_script("pw")
    end_marker = '> "$CIB_WORK/agent.plist"'
    end = script.index(end_marker) + len(end_marker)
    # The first printf in the script belongs to sudo_pw; this is the last one before
    # the plist is written.
    start = script.rindex("printf ", 0, end)
    out = tmp_path / "agent.plist"
    statement = script[start:end].replace('"$CIB_WORK/agent.plist"', str(out))
    subprocess.run(["/bin/sh", "-c", statement], check=True, capture_output=True)  # noqa: S603
    assert plistlib.loads(out.read_bytes())["Label"] == cib.AGENT_LABEL


# --- settings that used to fail late, or quietly do the wrong thing ------------


def test_an_empty_share_is_refused_rather_than_sharing_the_current_directory(monkeypatch):
    # "~/Downloads/chrome-vm" expands to the working directory when empty, and the
    # guest would get whatever happened to be there.
    monkeypatch.setenv("CIB_VM_SHARE", "  ")
    with pytest.raises(cib.Failure, match="CIB_VM_SHARE is empty"):
        cib.VmConfig().check()


@pytest.mark.parametrize("value", ["1920", "1920*1200", "big", "1920x"])
def test_a_malformed_display_is_refused_like_the_box_variant_does(monkeypatch, value):
    monkeypatch.setenv("CIB_VM_DISPLAY", value)
    with pytest.raises(cib.Failure, match="CIB_VM_DISPLAY"):
        cib.VmConfig().check()


def test_a_well_formed_display_and_share_pass(monkeypatch):
    monkeypatch.setenv("CIB_VM_DISPLAY", "1280x800")
    cib.VmConfig().check()  # must not raise


def test_deleting_the_vm_takes_its_password_and_keys_with_it(credentials, monkeypatch):
    # Left behind, the next build silently reuses them, and 'cib vm password' keeps
    # printing a password for a guest that no longer exists.
    cib.guest_password(create=True)
    cib.ensure_vm_keys()
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    cib.cmd_vm_delete("tart", cib.VmConfig())
    for gone in (cib.CREDENTIALS, cib.VM_KEY, cib.VM_HOST_KEY, cib.KNOWN_HOSTS):
        assert not gone.exists(), f"{gone.name} outlived the VM"


def test_a_cancelled_delete_keeps_everything(credentials, monkeypatch):
    cib.guest_password(create=True)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    cib.cmd_vm_delete("tart", cib.VmConfig())
    assert cib.CREDENTIALS.exists()


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
    monkeypatch.setattr(cib.os, "readlink", lambda p: link)
    result = cib.host_time_zone()
    assert result == (expected or ("America/Argentina/Buenos_Aires", "Buenos Aires"))


def test_the_guest_gets_the_key_and_the_host_key_it_will_be_checked_against(tmp_path, monkeypatch):
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    keys = cibpatch.Keys(
        authorized="ssh-ed25519 AAAAPUB cib",
        host_private="-----BEGIN OPENSSH PRIVATE KEY-----\nx\n",
        host_public="ssh-ed25519 AAAAHOST cib-guest",
    )
    cibpatch.patch(root, cibpatch.Account("admin", "pw"), None, keys)
    authorized = root / "Users/admin/.ssh/authorized_keys"
    assert authorized.read_text().strip() == "ssh-ed25519 AAAAPUB cib"
    assert authorized.stat().st_mode & 0o777 == 0o600
    assert (root / "Users/admin/.ssh").stat().st_mode & 0o777 == 0o700
    host = root / "private/etc/ssh/ssh_host_ed25519_key"
    assert host.read_text() == keys.host_private
    assert host.stat().st_mode & 0o777 == 0o600, "sshd refuses a world-readable host key"
    assert host.with_suffix(".pub").read_text().strip() == keys.host_public


def test_a_secret_is_never_briefly_world_readable(tmp_path):
    # Creating the file and chmod-ing it afterwards leaves a window in between.
    target = tmp_path / "secret"
    cibpatch.write_private(target, b"x")
    assert target.stat().st_mode & 0o777 == 0o600


def test_a_planted_fifo_does_not_hang_the_root_patcher(tmp_path):
    # guest_path only refused symlinks. Opening a FIFO blocks until something opens
    # the other end, which nothing ever does — so the patch hung for ever, as root.
    # Sockets and device nodes are refused by the same check.
    root = tmp_path / "volume"
    (root / "private/etc").mkdir(parents=True)
    os.mkfifo(root / "private/etc/kcpassword")
    with pytest.raises(cibpatch.PatchError, match="neither a directory nor a regular file"):
        cibpatch.guest_path(root, "private/etc/kcpassword")


def test_a_fifo_in_the_middle_of_the_path_is_refused_too(tmp_path):
    root = tmp_path / "volume"
    (root / "private").mkdir(parents=True)
    os.mkfifo(root / "private/etc")
    with pytest.raises(cibpatch.PatchError, match="neither a directory nor a regular file"):
        cibpatch.guest_path(root, "private/etc/kcpassword")


# --- round 13's remainder ------------------------------------------------------


def test_shell_refuses_instead_of_reporting_a_session_it_never_opened(monkeypatch):
    # `cib box shell && echo attached` printed the engine's refusal and then
    # "attached", because the exec's exit code was ignored.
    monkeypatch.setattr(cib, "container_running", lambda *a, **k: False)
    with pytest.raises(cib.Failure, match="cib box up"):
        cib.cmd_shell("podman", cib.Config())


def test_shell_opens_when_the_container_is_there(calls, monkeypatch):
    monkeypatch.setattr(cib, "container_running", lambda *a, **k: True)
    cib.cmd_shell("podman", cib.Config())
    assert "exec" in flat(calls)


def test_the_first_pull_is_visible_instead_of_several_silent_gigabytes(calls, monkeypatch):
    # `run -d` is captured, which also swallowed the whole first pull: one line of
    # output and then nothing at all for several GB.
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    seen = {}

    def fake(engine, *args, **kwargs):
        seen.setdefault("cmds", []).append((args, kwargs))
        return subprocess.CompletedProcess([], 0 if args[0] == "pull" else 1, stdout="", stderr="")

    monkeypatch.setattr(cib, "run", fake)
    cib.ensure_image("podman", cib.Config())
    pull = next(a for a, _ in seen["cmds"] if a[0] == "pull")
    kwargs = next(k for a, k in seen["cmds"] if a[0] == "pull")
    assert "--platform" in pull
    assert not kwargs.get("capture"), "capturing the pull is what hid it"


def test_an_image_already_present_in_the_right_architecture_is_not_pulled_again(monkeypatch):
    pulled = []
    monkeypatch.setattr(
        cib,
        "run",
        lambda e, *a, **k: (
            pulled.append(a[0]) or subprocess.CompletedProcess([], 0, stdout="amd64\n", stderr="")
        ),
    )
    cib.ensure_image("podman", cib.Config())
    assert "pull" not in pulled


def test_an_image_of_the_wrong_architecture_is_pulled_again_visibly(monkeypatch):
    # An arm64 copy of the same tag satisfies `image inspect`, and `run --platform
    # linux/amd64` then pulls the amd64 one anyway — captured, so silently.
    seen = []
    monkeypatch.setattr(
        cib,
        "run",
        lambda e, *a, **k: (
            seen.append((a[0], k.get("capture")))
            or subprocess.CompletedProcess([], 0, stdout="arm64\n", stderr="")
        ),
    )
    cib.ensure_image("podman", cib.Config())
    assert ("pull", None) in seen or ("pull", False) in seen


def test_a_failed_pull_is_reported(monkeypatch):
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    with pytest.raises(cib.Failure, match="could not pull"):
        cib.ensure_image("podman", cib.Config())


@pytest.mark.parametrize("value,expected", [["1920X1200", "1920x1200"], ["1280 x 800", "1280x800"]])
def test_the_vm_display_is_normalised_like_the_box_one(monkeypatch, value, expected):
    # tart takes 1920X1200 without complaint and then ignores it, leaving the guest
    # at 1024x768 with nothing said.
    monkeypatch.setenv("CIB_VM_DISPLAY", value)
    vm = cib.VmConfig()
    vm.check()
    assert vm.normalised_display == expected


def test_an_account_the_guest_already_has_is_not_overwritten(tmp_path, monkeypatch):
    # root, daemon and _spotlight all match the name rules. Replacing root's record
    # with a uid-501 one leaves the guest with no working sudo at all.
    import plistlib

    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    users = root / "private/var/db/dslocal/nodes/Default/users"
    with (users / "root.plist").open("wb") as fh:
        plistlib.dump({"name": ["root"], "uid": ["0"]}, fh, fmt=plistlib.FMT_BINARY)
    with pytest.raises(cibpatch.PatchError, match="already has an account called 'root'"):
        cibpatch.patch(root, cibpatch.Account("root", "pw"))
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
        cibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=mount_output, stderr=""),
    )
    assert cibpatch.ownership_is_honoured(Path("/Volumes/Data")) is expected


@pytest.mark.parametrize("content", [[1, 2, 3], "a string", 42])
def test_a_plist_that_is_not_a_dictionary_is_not_a_traceback(tmp_path, content):
    # A plist root can be any type, and this runs as root: an array where a dict was
    # expected used to come out as a traceback rather than a message.
    import plistlib

    target = tmp_path / "Library" / "Preferences" / "com.apple.loginwindow.plist"
    target.parent.mkdir(parents=True)
    with target.open("wb") as fh:
        plistlib.dump(content, fh, fmt=plistlib.FMT_BINARY)
    assert cibpatch.read_plist(tmp_path, "Library/Preferences/com.apple.loginwindow.plist") == {}


def test_a_diskutil_that_will_not_describe_the_volume_is_reported(monkeypatch):
    def fake(cmd, *a, **k):
        if "info" in cmd:
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"could not find disk")
        if cmd[0] == "/sbin/mount":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(cibpatch.subprocess, "run", fake)
    with pytest.raises(cibpatch.PatchError, match="could not find disk"):
        cibpatch.mount("/dev/disk9s1")


def test_the_agent_directory_is_created_before_the_agent_is_installed():
    # BSD install does not create its target directory, and a fresh guest can have
    # /usr/local with no bin in it.
    script = cib.guest_install_script("pw")
    assert f'sudo_pw install -d -m 0755 "$(dirname {cib.AGENT_BIN})"' in script
    assert script.index("install -d") < script.index(
        f'"$CIB_WORK/tart-guest-agent" {cib.AGENT_BIN}'
    )


def test_a_python3_that_cannot_run_is_not_treated_as_an_interpreter(monkeypatch):
    # /usr/bin/python3 exists on every Mac and is a stub without the Command Line
    # Tools: it is executable, and exits non-zero the moment it runs.
    monkeypatch.setattr(cib.shutil, "which", lambda name: None)
    monkeypatch.setattr(cib.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1))
    with pytest.raises(cib.Failure, match="xcode-select --install"):
        cib.find_guest_python()


def test_a_working_python3_is_used_as_it_is(monkeypatch):
    monkeypatch.setattr(cib.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 0))
    assert cib.find_guest_python() == "/usr/bin/python3"


# --- round 14 and 15 -----------------------------------------------------------


def test_a_delete_that_failed_keeps_the_password_and_keys(credentials, monkeypatch):
    # Wiping them on a failed delete locks cib out of a guest that is still there.
    cib.guest_password(create=True)
    cib.ensure_vm_keys()
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(
        cib,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="VM is running"),
    )
    with pytest.raises(cib.Failure, match="nothing is locked out"):
        cib.cmd_vm_delete("tart", cib.VmConfig())
    for kept in (cib.CREDENTIALS, cib.VM_KEY, cib.VM_HOST_KEY, cib.KNOWN_HOSTS):
        assert kept.exists(), f"{kept.name} was destroyed by a delete that did not happen"


def test_ssh_refuses_every_way_of_being_asked_for_a_password(credentials):
    # PasswordAuthentication=no alone leaves keyboard-interactive, which sshd offers
    # the same password through.
    cmd = cib.ssh_command(cib.VmConfig(), "192.168.1.50")
    assert "PasswordAuthentication=no" in cmd
    assert "KbdInteractiveAuthentication=no" in cmd
    assert "NumberOfPasswordPrompts=0" in cmd


def test_the_interpreter_is_checked_before_the_download_too(credentials, monkeypatch, tmp_path):
    # The README promises the preflight catches this; it used to run after the build.
    calls: list = []
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib, "sudo_is_cached", lambda: True)
    monkeypatch.setattr(cib, "find_patcher", lambda: tmp_path / "cibpatch.py")
    monkeypatch.setattr(cib.shutil, "which", lambda name: None)
    monkeypatch.setattr(cib.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1))
    monkeypatch.setattr(cib, "run", lambda *a, **k: calls.append(a) or None)
    with pytest.raises(cib.Failure, match="xcode-select --install"):
        cib.cmd_vm_create("tart", cib.VmConfig())
    assert calls == [], "nothing may be downloaded before the check"


def test_preparing_twice_keeps_the_accounts_generated_uid(tmp_path, monkeypatch):
    # Group membership records the GUID: a fresh one on every prepare leaves admin
    # and staff pointing at a user that no longer exists under that id.
    import plistlib

    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    users = root / "private/var/db/dslocal/nodes/Default/users"
    groups = root / "private/var/db/dslocal/nodes/Default/groups"
    cibpatch.patch(root, cibpatch.Account("admin", "pw"))
    first = plistlib.loads((users / "admin.plist").read_bytes())["generateduid"][0]
    cibpatch.patch(root, cibpatch.Account("admin", "pw"))
    assert plistlib.loads((users / "admin.plist").read_bytes())["generateduid"][0] == first
    members = plistlib.loads((groups / "admin.plist").read_bytes())["groupmembers"]
    assert members == [first], "the group must still name the account that exists"


def test_a_malformed_xml_plist_is_not_a_traceback(tmp_path):
    # Well-formed-looking XML that is not valid raises ExpatError, which is none of
    # the exceptions already caught — out of a step running as root.
    target = tmp_path / "Library" / "Preferences" / "com.apple.loginwindow.plist"
    target.parent.mkdir(parents=True)
    target.write_bytes(b'<?xml version="1.0"?>\n<plist version="1.0"><dict><key>x')
    assert cibpatch.read_plist(tmp_path, "Library/Preferences/com.apple.loginwindow.plist") == {}


def test_each_vm_name_keeps_its_own_password_and_keys(monkeypatch, tmp_path):
    # They used to sit flat in one directory, so a second CIB_VM_NAME reused the
    # first one's key — and deleting either took the other's away.
    monkeypatch.setattr(cib.Path, "home", classmethod(lambda c: tmp_path))
    first = cib.secrets_dir()
    assert first.name == "chrome-vm"
    monkeypatch.setenv("CIB_VM_NAME", "other")
    assert cib.secrets_dir() != first
    assert cib.secrets_dir().name == "other"
    assert cib.secrets_dir().parent == first.parent


def test_the_six_files_that_have_to_be_migrated_are_named_here_too():
    # All three migration tests seed and assert with SECRET_NAMES, so the tuple was
    # only ever compared with itself: dropping "vm-credentials" from it left the
    # password stranded in the flat directory with the suite green, and the first
    # command an upgrading user ran died on a guest that exists.
    assert set(cib.SECRET_NAMES) == {
        "vm-credentials",
        "vm-key",
        "vm-key.pub",
        "vm-host-key",
        "vm-host-key.pub",
        "vm-known-hosts",
    }
    # And they are the files the rest of cib actually uses.
    assert cib.CREDENTIALS.name in cib.SECRET_NAMES
    assert cib.VM_KEY.name in cib.SECRET_NAMES
    assert cib.VM_KEY.with_suffix(".pub").name in cib.SECRET_NAMES
    assert cib.VM_HOST_KEY.name in cib.SECRET_NAMES
    assert cib.VM_HOST_KEY.with_suffix(".pub").name in cib.SECRET_NAMES
    assert cib.KNOWN_HOSTS.name in cib.SECRET_NAMES


def test_secrets_an_older_cib_left_flat_go_to_the_default_vm(monkeypatch, tmp_path):
    # Nothing on disk says which guest they belong to. Moving them into whichever
    # name runs first takes them away from the guest actually using them — whose
    # disk was patched with that key pair, and which has no password fallback left.
    monkeypatch.setattr(cib.Path, "home", classmethod(lambda c: tmp_path))
    flat = tmp_path / ".config" / "chrome-in-a-box"
    flat.mkdir(parents=True)
    for name in cib.SECRET_NAMES:
        (flat / name).write_text(f"old {name}\n")
    monkeypatch.setenv("CIB_VM_NAME", "work")  # a second name runs first
    cib.migrate_flat_secrets()
    for name in cib.SECRET_NAMES:
        assert (flat / cib.DEFAULT_VM_NAME / name).read_text() == f"old {name}\n"
        assert not (flat / name).exists(), "moved, not copied"
        assert not (flat / "work" / name).exists(), "they are not the second VM's"


def test_the_migration_does_not_overwrite_what_is_already_there(monkeypatch, tmp_path):
    monkeypatch.setattr(cib.Path, "home", classmethod(lambda c: tmp_path))
    flat = tmp_path / ".config" / "chrome-in-a-box"
    (flat / cib.DEFAULT_VM_NAME).mkdir(parents=True)
    (flat / "vm-credentials").write_text("old\n")
    (flat / cib.DEFAULT_VM_NAME / "vm-credentials").write_text("current\n")
    cib.migrate_flat_secrets()
    assert (flat / cib.DEFAULT_VM_NAME / "vm-credentials").read_text() == "current\n"


def test_delete_removes_what_an_older_cib_left_flat_too(credentials, monkeypatch, tmp_path):
    # It used to unlink per-name paths that did not exist yet, print "Deleted.",
    # and the next command migrated the flat originals back in.
    monkeypatch.setattr(cib.Path, "home", classmethod(lambda c: tmp_path))
    flat = tmp_path / ".config" / "chrome-in-a-box"
    flat.mkdir(parents=True)
    for name in cib.SECRET_NAMES:
        (flat / name).write_text("old\n")
    monkeypatch.setattr(cib, "SECRETS", flat / cib.DEFAULT_VM_NAME)
    monkeypatch.setattr(cib, "CREDENTIALS", cib.SECRETS / "vm-credentials")
    monkeypatch.setattr(cib, "VM_KEY", cib.SECRETS / "vm-key")
    monkeypatch.setattr(cib, "VM_HOST_KEY", cib.SECRETS / "vm-host-key")
    monkeypatch.setattr(cib, "KNOWN_HOSTS", cib.SECRETS / "vm-known-hosts")
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    cib.cmd_vm_delete("tart", cib.VmConfig())
    for name in cib.SECRET_NAMES:
        assert not (flat / name).exists(), f"{name} survived a delete that said Deleted."
        assert not (flat / cib.DEFAULT_VM_NAME / name).exists()


def test_the_shell_reports_an_exec_the_engine_refused(monkeypatch):
    # Checking the container first was not enough: the exec itself can fail, and
    # its exit code was still thrown away.
    monkeypatch.setattr(cib, "container_running", lambda *a, **k: True)
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 125, stdout="", stderr="")
    )
    with pytest.raises(cib.Failure, match="could not start a shell"):
        cib.cmd_shell("podman", cib.Config())


def test_a_shell_that_exits_non_zero_is_not_a_cib_failure(monkeypatch):
    # The user's own shell exiting 1 is their business, not a cib error.
    monkeypatch.setattr(cib, "container_running", lambda *a, **k: True)
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="")
    )
    cib.cmd_shell("podman", cib.Config())  # must not raise


@pytest.mark.parametrize("tty,expected", [(True, "-it"), (False, "-i")])
def test_a_pty_is_only_asked_for_when_there_is_a_terminal(monkeypatch, tty, expected):
    # podman blocks for ever allocating a pty for a stdin that is a pipe, so
    # `cib box shell` from a script or CI hung instead of failing.
    monkeypatch.setattr(cib, "container_running", lambda *a, **k: True)
    monkeypatch.setattr(cib.sys.stdin, "isatty", lambda: tty, raising=False)
    seen = {}
    monkeypatch.setattr(
        cib,
        "run",
        lambda e, *a, **k: seen.update(args=a) or subprocess.CompletedProcess([], 0),
    )
    cib.cmd_shell("podman", cib.Config())
    assert seen["args"][1] == expected


@pytest.mark.parametrize("mode", ["1600x900", "800x600", "1919x1199"])
def test_a_resolution_kasmvnc_does_not_ship_is_refused_not_merely_bounded(monkeypatch, mode):
    # In range is not the same as available: 1600x900 is smaller than the largest
    # mode and still not there, and xrandr then leaves the desktop at 1024x768
    # while cib warned three times and reported "Ready."
    monkeypatch.setenv("CIB_RESOLUTION", mode)
    with pytest.raises(cib.Failure, match="not one of the modes KasmVNC"):
        cib.Config().check()


@pytest.mark.parametrize("mode", ["1920x1200", "1280x800", "1024x768"])
def test_the_modes_kasmvnc_does_ship_are_accepted(monkeypatch, mode):
    monkeypatch.setenv("CIB_RESOLUTION", mode)
    cib.Config().check()  # must not raise


def test_the_sdist_carries_everything_its_tests_read():
    # Shipping tests/ without the files they open means the tests are there and
    # cannot run, which is the only reason to ship them.
    # Read as text, not with tomllib: that is 3.11+, and this project supports 3.10.
    root = Path(cib.__file__).resolve().parent
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
    # green — producing exactly the "a VM cib could never connect to" the packer
    # fix was written for.
    _fake_guest_disk(monkeypatch, tmp_path)
    seen = {}
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: (
            (seen.update(cmd=cmd) if any("cibpatch" in str(c) for c in cmd) else None)
            or subprocess.CompletedProcess(cmd, 0)
        ),
    )
    cib._prepare_guest(cib.VmConfig(), "pw")
    cmd = seen["cmd"]
    assert cmd[cmd.index("--authorized-key") + 1] == str(cib.VM_KEY.with_suffix(".pub"))
    assert cmd[cmd.index("--host-key") + 1] == str(cib.VM_HOST_KEY)
    assert cib.KNOWN_HOSTS.exists(), "the host key has to be pinned for cib to use it"


def test_the_default_build_generates_the_keys_if_they_are_missing(
    credentials, monkeypatch, tmp_path
):
    for stale in (
        cib.VM_KEY,
        cib.VM_KEY.with_suffix(".pub"),
        cib.VM_HOST_KEY,
        cib.VM_HOST_KEY.with_suffix(".pub"),
    ):
        stale.unlink(missing_ok=True)
    _fake_guest_disk(monkeypatch, tmp_path)
    real = cib.subprocess.run
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: (
            real(cmd, **kw) if "ssh-keygen" in str(cmd[0]) else subprocess.CompletedProcess(cmd, 0)
        ),
    )
    cib._prepare_guest(cib.VmConfig(), "pw")
    assert cib.VM_KEY.with_suffix(".pub").read_text().startswith("ssh-ed25519 ")
    assert cib.VM_HOST_KEY.with_suffix(".pub").read_text().startswith("ssh-ed25519 ")


def test_the_share_the_guest_looks_for_is_the_one_tart_mounts(calls, credentials, monkeypatch):
    # The old assertion compared GUEST_SHARE with itself. Change either side alone
    # and `cib vm setup` aborts before installing anything, because the script's
    # `[ -d "$GUEST_SHARE" ]` fails.
    monkeypatch.setattr(cib, "vm_running", lambda *a, **k: False)
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: True)
    cib.cmd_vm_up("tart", cib.VmConfig())
    shared = next(a for a in flat(calls).split() if a.startswith("--dir="))
    name = shared.removeprefix("--dir=").split(":", 1)[0]
    assert f"/Volumes/My Shared Files/{name}" == cib.GUEST_SHARE, (
        "the guest looks for the share under the name tart was told to use"
    )


# --- what the mutation pass found the suite still did not watch ----------------


def test_a_complete_patch_writes_every_marker_it_documents(tmp_path, monkeypatch):
    # Each of these had a test calling the function directly, so deleting the CALL
    # from patch() left the suite green — and the guest boots into the very Setup
    # Assistant the offline path exists to avoid, or with sshd still disabled.
    import plistlib

    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    keys = cibpatch.Keys(
        authorized="ssh-ed25519 AAAAPUB cib",
        host_private="KEY\n",
        host_public="ssh-ed25519 AAAAHOST g",
    )
    cibpatch.patch(
        root, cibpatch.Account("admin", "pw"), cibpatch.Keyboard(19, "Swiss German"), keys
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
    assert launchd["com.openssh.sshd"] is False, "without this cib can never reach the guest"
    login = plistlib.loads((root / "Library/Preferences/com.apple.loginwindow.plist").read_bytes())
    assert login["autoLoginUser"] == "admin"
    assert (root / "private/etc/kcpassword").read_bytes() == cibpatch.kcpassword("pw")
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
    monkeypatch.setattr(cib, "container_running", lambda *a, **k: False)
    monkeypatch.setattr(cib, "ensure_image", lambda e, c: order.append("pull"))
    monkeypatch.setattr(cib, "wait_for_ui", lambda e, c: None)
    monkeypatch.setattr(cib, "ensure_desktop", lambda e, c: True)
    monkeypatch.setattr(
        cib,
        "run",
        lambda e, *a, **k: order.append(a[0]) or subprocess.CompletedProcess([], 0, stdout=""),
    )
    cib.cmd_up("podman", cib.Config())
    assert order.index("pull") < order.index("run"), "the pull has to happen outside `run -d`"


def test_the_password_verifier_matches_a_known_answer(monkeypatch):
    # Swapping the password and the salt, or reordering the PBKDF2 arguments, still
    # produces a plausible-looking verifier — one macOS will never match, so the
    # account exists and refuses its own password for ever.
    monkeypatch.setattr(cibpatch.secrets, "token_bytes", lambda n: bytes(range(n)))
    import plistlib

    entry = plistlib.loads(cibpatch.shadow_hash_data("hunter2"))["SALTED-SHA512-PBKDF2"]
    expected = hashlib.pbkdf2_hmac("sha512", b"hunter2", bytes(range(32)), 50_000, 128)
    assert entry["entropy"] == expected
    assert entry["salt"] == bytes(range(32))


def test_the_sudo_probe_can_never_block_on_a_prompt(monkeypatch):
    # Without -n the probe whose docstring promises an exit code instead of a hang
    # waits for a password cib says it will never ask for.
    seen = {}
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: seen.update(cmd=cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    assert cib.sudo_is_cached() is True
    assert "-n" in seen["cmd"]


def test_the_keepalive_refreshes_without_prompting(monkeypatch):
    # sudo forgets a credential in about five minutes and the build takes thirty to
    # sixty; without this the last step of every unattended build was refused.
    seen: list[list[str]] = []
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: seen.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    keepalive = cib.SudoKeepalive(interval=0.01)
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
    with pytest.raises(cibpatch.PatchError, match="file where a directory has to be"):
        cibpatch.guest_path(root, relative, make_parents=True)


def test_a_directory_where_a_file_belongs_is_a_message_too(tmp_path):
    root = tmp_path / "volume"
    (root / "private/etc/kcpassword").mkdir(parents=True)
    with pytest.raises(cibpatch.PatchError, match="directory in the guest where a file"):
        cibpatch.guest_path(root, "private/etc/kcpassword", make_parents=True)


# --- round 17 ------------------------------------------------------------------


def test_preparing_a_guest_that_already_has_the_key_still_works(tmp_path, monkeypatch):
    # `cib vm prepare` is the documented retry for a half-hour build, and every
    # failure message points at it. The directory guard added for a file where a
    # directory belongs fired on ~/.ssh, which is a directory and is supposed to be.
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    keys = cibpatch.Keys(
        authorized="ssh-ed25519 AAAAPUB cib",
        host_private="KEY\n",
        host_public="ssh-ed25519 AAAAHOST guest",
    )
    cibpatch.patch(root, cibpatch.Account("admin", "pw"), None, keys)
    cibpatch.patch(root, cibpatch.Account("admin", "pw"), None, keys)  # must not raise
    authorized = root / "Users/admin/.ssh/authorized_keys"
    assert authorized.read_text().strip() == keys.authorized
    assert authorized.stat().st_mode & 0o777 == 0o600


def test_a_file_where_the_ssh_directory_belongs_is_still_refused(tmp_path, monkeypatch):
    # The guard has to keep catching the case it was added for.
    monkeypatch.setattr(cibpatch.os, "geteuid", lambda: 0)
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: None)
    root = _guest_volume(tmp_path)
    (root / "Users/admin").mkdir(parents=True)
    (root / "Users/admin/.ssh").write_text("not a directory")
    with pytest.raises(cibpatch.PatchError, match="file in the guest where a directory"):
        cibpatch.authorise_key(root, cibpatch.Account("admin", "pw"), "ssh-ed25519 AAAA cib")


def test_every_vm_command_migrates_an_older_installs_secrets(monkeypatch, tmp_path):
    # 'cib vm ssh' reads the keys through ssh_options() without ever calling
    # guest_password(), so it was the one command that failed on a pre-1.4 install
    # while every other one repaired it on the way past.
    monkeypatch.setattr(cib.Path, "home", classmethod(lambda c: tmp_path))
    flat = tmp_path / ".config" / "chrome-in-a-box"
    flat.mkdir(parents=True)
    for name in cib.SECRET_NAMES:
        (flat / name).write_text(f"old {name}\n")
    monkeypatch.setattr(cib, "SECRETS", flat / cib.DEFAULT_VM_NAME)
    monkeypatch.setattr(cib, "find_tart", lambda: "/usr/bin/tart")
    monkeypatch.setattr(cib, "VM_ACTIONS", {"ssh": lambda tart, vm: None})
    cib.main(["vm", "ssh"])
    for name in cib.SECRET_NAMES:
        assert (flat / cib.DEFAULT_VM_NAME / name).exists(), f"{name} was not migrated"


def test_the_patcher_turns_the_key_paths_it_is_given_into_key_material(monkeypatch, tmp_path):
    # cib's side of this wire is asserted; the patcher's side was not, so main()
    # could throw both arguments away with the suite green.
    import io

    pub, priv = tmp_path / "k.pub", tmp_path / "h"
    pub.write_text("ssh-ed25519 AAAAPUB cib\n")
    priv.write_text("HOSTKEY\n")
    priv.with_suffix(".pub").write_text("ssh-ed25519 AAAAHOST guest\n")
    seen = {}
    monkeypatch.setattr(cibpatch, "prepare", lambda d, a, k, ks: seen.update(keys=ks))
    monkeypatch.setattr(cibpatch.sys, "stdin", io.StringIO("pw\n"))
    cibpatch.main(
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
    assert seen["keys"].authorized.strip() == "ssh-ed25519 AAAAPUB cib"
    assert seen["keys"].host_private == "HOSTKEY\n"
    assert seen["keys"].host_public.strip() == "ssh-ed25519 AAAAHOST guest"


def test_the_templates_key_provisioner_writes_what_ssh_will_look_for(tmp_path):
    # Grepping the template for "authorized_keys" passes even if the provisioner
    # writes it somewhere sshd never reads.
    import re as _re

    template = (Path(cib.__file__).resolve().parent / "packer" / "chrome-vm.pkr.hcl").read_text()
    lines = _re.findall(
        r'^\s*"(mkdir -p ~/\.ssh.*?|printf .*?authorized_keys)",\s*$', template, _re.M
    )
    assert lines, "the key provisioner is no longer where it was"
    home = tmp_path / "home"
    home.mkdir()
    # HCL escapes its strings, so what packer hands the shell is the unescaped
    # form: `printf '%s\\n'` in the template is `printf '%s\n'` in the guest.
    script = "\n".join(
        ln.replace("\\\\n", "\\n").replace("${var.authorized_key}", "ssh-ed25519 AAAAPUB cib")
        for ln in lines
    )
    subprocess.run(  # noqa: S603
        ["/bin/sh", "-e", "-c", script],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
    )
    assert (home / ".ssh/authorized_keys").read_text().strip() == "ssh-ed25519 AAAAPUB cib"


def test_up_mounts_the_profile_volume_where_the_image_keeps_it(calls, monkeypatch):
    # Drop the -v, or let the container path drift from /home/kasm-user, and the
    # profile stops persisting while `box down` still promises it is kept.
    monkeypatch.setattr(cib, "container_running", lambda *a, **k: False)
    monkeypatch.setattr(cib, "ensure_image", lambda e, c: None)
    monkeypatch.setattr(cib, "wait_for_ui", lambda e, c: None)
    monkeypatch.setattr(cib, "ensure_desktop", lambda e, c: True)
    cfg = cib.Config()
    cib.cmd_up("podman", cfg)
    # The exact pair, not a substring: "/home/kasm-user/Downloads" contains
    # "/home/kasm-user", so the substring form could only ever catch a dropped -v,
    # never the drift the comment above names.
    argv = [a for call in calls for a in call]
    assert argv[argv.index("-v") + 1] == f"{cfg.volume}:/home/kasm-user"
    assert cib.PROFILE_DIR.startswith("/home/kasm-user/"), (
        "the profile has to live under what is mounted, or it stops persisting"
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
        cib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=exported),
    )
    assert cib.host_keyboard_layout() == (19, "Swiss German")


def test_the_enabled_list_is_used_when_nothing_is_selected(monkeypatch):
    import plistlib

    exported = plistlib.dumps(
        {"AppleEnabledInputSources": [_SWISS], "AppleSelectedInputSources": []},
        fmt=plistlib.FMT_XML,
    ).decode()
    monkeypatch.setattr(
        cib.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=exported),
    )
    assert cib.host_keyboard_layout() == (19, "Swiss German")


def test_a_secret_written_over_a_loose_file_gets_the_tight_mode(tmp_path):
    # O_CREAT|O_TRUNC applies the mode only when it creates the file, so the unlink
    # is the only thing that makes the docstring true for a path that exists. sshd
    # refuses to start with a group-readable host key, so the guest would silently
    # become unreachable and the documented retry would not repair it.
    target = tmp_path / "ssh_host_ed25519_key"
    target.write_bytes(b"OLD\n")
    target.chmod(0o644)
    cibpatch.write_private(target, b"NEWKEY\n")
    assert target.read_bytes() == b"NEWKEY\n"
    assert target.stat().st_mode & 0o777 == 0o600


# --- round 19 ------------------------------------------------------------------


def test_owning_the_home_never_descends_through_a_link(tmp_path, monkeypatch):
    # The old assertion pinned the chown's follow_symlinks flag, which is only half
    # the guard: os.walk(followlinks=True) descends *through* a symlinked directory
    # and lchowns whatever is inside it — on the host. Both mutations have to fail.
    chowned: list[str] = []
    monkeypatch.setattr(cibpatch.os, "chown", lambda p, u, g, **kw: chowned.append(str(p)))
    outside = tmp_path / "host"
    (outside / "deeper").mkdir(parents=True)
    (outside / "deeper" / "sudoers").write_text("root ALL")
    home = tmp_path / "volume" / "Users" / "admin"
    home.mkdir(parents=True)
    (home / "real").write_text("guest file")
    (home / "escape").symlink_to(outside)
    cibpatch.own_home(tmp_path / "volume", cibpatch.Account("admin", "pw"))
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
    template = (Path(cib.__file__).resolve().parent / "packer" / "chrome-vm.pkr.hcl").read_text()
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
    assert f"/Library/LaunchAgents/{cib.AGENT_LABEL}.plist" == cib.AGENT_PLIST_PATH
    assert "/Library/LaunchDaemons" not in cib.guest_install_script("pw")


def test_the_keepalive_refreshes_faster_than_sudo_forgets():
    # sudo's timestamp_timeout is five minutes by default and the build takes thirty
    # to sixty; any interval above that makes the thread do nothing useful, and the
    # last step of every unattended build is refused.
    assert cib.SudoKeepalive().interval <= 120


def test_the_image_is_pulled_before_the_container_is_removed(monkeypatch):
    # A guard has to run before the thing it guards: the rm used to happen first, so
    # a pull that could not succeed destroyed a working container and then reported
    # only the pull.
    order: list[str] = []
    monkeypatch.setattr(cib, "container_running", lambda *a, **k: False)
    monkeypatch.setattr(
        cib,
        "ensure_image",
        lambda e, c: order.append("pull") or (_ for _ in ()).throw(cib.Failure("could not pull")),
    )
    monkeypatch.setattr(
        cib, "run", lambda e, *a, **k: order.append(a[0]) or subprocess.CompletedProcess([], 0)
    )
    with pytest.raises(cib.Failure, match="could not pull"):
        cib.cmd_up("podman", cib.Config())
    assert "rm" not in order, "a working container was removed for a pull that failed"


def test_the_guest_sets_its_own_time_zone_with_its_own_tool(credentials, monkeypatch):
    # Patching /etc/localtime from the host cannot work: on a real Data volume both
    # it and the zoneinfo directory are symlinks into paths that resolve against the
    # host, so the patcher refused them and aborted the whole patch.
    cib.guest_password(create=True)
    monkeypatch.setattr(cib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(cib, "host_time_zone", lambda: ("Europe/Rome", "Rome"))
    seen = {}
    monkeypatch.setattr(
        cib, "guest_ssh", lambda vm, ip, script=None: seen.update(script=script) or 0
    )
    cib.cmd_vm_setup("tart", cib.VmConfig())
    assert "sudo_pw systemsetup -settimezone Europe/Rome" in seen["script"]


def test_a_guest_script_without_a_time_zone_still_runs(tmp_path):
    # The step has to be a no-op, not an empty line that `sh -e` chokes on.
    result = _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=True)
    assert result.returncode == 0, result.stderr


def test_the_time_zone_step_runs_in_the_guest(tmp_path):
    script = cib.guest_install_script("pw", "Europe/Rome")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "systemsetup").write_text(f'#!/bin/sh\necho "$@" >> {tmp_path}/tz.log\n')
    (bin_dir / "systemsetup").chmod(0o755)
    result = _run_guest_script(script, tmp_path, share_exists=True, extra_bin=bin_dir)
    assert result.returncode == 0, result.stderr
    assert "-settimezone Europe/Rome" in (tmp_path / "tz.log").read_text()


def test_a_time_zone_the_guest_rejects_does_not_fail_the_install(tmp_path):
    # A wrong clock is an annoyance; a failed `vm setup` costs Chrome and the
    # clipboard agent. Under `sh -e` the step has to swallow its own failure.
    script = cib.guest_install_script("pw", "Mars/Olympus")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "systemsetup").write_text("#!/bin/sh\nexit 1\n")
    (bin_dir / "systemsetup").chmod(0o755)
    result = _run_guest_script(script, tmp_path, share_exists=True, extra_bin=bin_dir)
    assert result.returncode == 0, result.stderr
    assert "could not set the time zone" in result.stderr


@pytest.mark.parametrize("command", ["ssh", "setup"])
def test_the_repair_advice_names_the_step_that_makes_it_possible(credentials, monkeypatch, command):
    # Both messages can only be printed while the guest is up, and `cib vm prepare`
    # refuses while it is up. Naming prepare alone sent the user to a second error.
    cib.guest_password(create=True)
    monkeypatch.setattr(cib, "vm_ip", lambda *a, **k: "192.168.1.50")
    monkeypatch.setattr(cib, "guest_ssh", lambda *a, **k: 255)
    action = cib.cmd_vm_ssh if command == "ssh" else cib.cmd_vm_setup
    with pytest.raises(cib.Failure) as caught:
        action("tart", cib.VmConfig())
    message = str(caught.value)
    assert "cib vm prepare" in message
    assert "cib vm down" in message, "prepare refuses while the guest is running"
    assert message.index("cib vm down") < message.index("cib vm prepare")


def test_the_sudo_message_says_the_credential_is_per_terminal():
    # sudo remembers per tty. "Run 'sudo -v', then re-run" is only true from the
    # same window, and a process with no tty can never satisfy the check at all —
    # which is exactly how this was found, from a tool that has none.
    assert "SAME TERMINAL" in cib.SUDO_MESSAGE
    assert "per tty" in cib.SUDO_MESSAGE


def test_a_boot_blocked_by_the_installers_lock_is_retried(monkeypatch):
    # `tart create` returns before the Virtualization framework lets go of the VM's
    # auxiliary storage, so a boot started straight afterwards fails with EAGAIN.
    # Nothing holds it a moment later: it is a handover, not a conflict.
    import io

    attempts = []

    class _Locked(_FakeBoot):
        returncode = 1

        # A property, not a class attribute: one StringIO shared by every instance
        # is emptied by the first read, so the second attempt would see no detail
        # and be reported as a different failure.
        @property
        def stderr(self):
            return io.StringIO('VZErrorDomain Code=2 "Failed to lock auxiliary storage."')

        def poll(self):
            return 1

    def spawn(*a, **k):
        attempts.append(1)
        return _FakeBoot() if len(attempts) >= 3 else _Locked()

    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    monkeypatch.setattr(cib.subprocess, "Popen", spawn)
    assert cib.boot_once("tart", cib.VmConfig()).poll() is None
    assert len(attempts) == 3


def test_a_boot_that_failed_for_another_reason_is_not_retried(monkeypatch):
    # A guest that never booted has no first-boot state; patching it produces
    # something that reports "Built." and cannot be logged in to.
    import io

    attempts = []

    class _Broken(_FakeBoot):
        returncode = 2
        stderr = io.StringIO("no such vm")

        def poll(self):
            return 2

    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    monkeypatch.setattr(cib.subprocess, "Popen", lambda *a, **k: attempts.append(1) or _Broken())
    with pytest.raises(cib.Failure, match="no such vm"):
        cib.boot_once("tart", cib.VmConfig())
    assert len(attempts) == 1, "only the lock error is transient"


def test_a_lock_that_never_clears_is_reported(monkeypatch):
    import io

    class _Locked(_FakeBoot):
        returncode = 1

        @property
        def stderr(self):
            return io.StringIO("Failed to lock auxiliary storage.")

        def poll(self):
            return 1

    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    monkeypatch.setattr(cib.subprocess, "Popen", lambda *a, **k: _Locked())
    with pytest.raises(cib.Failure, match="still locked after"):
        cib.boot_once("tart", cib.VmConfig())


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
        cibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    assert cibpatch.data_volume("/dev/disk4") == "/dev/disk5s2"


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
        cibpatch.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=listing, stderr=b""),
    )
    with pytest.raises(cibpatch.PatchError, match="iSCPreboot, Recovery"):
        cibpatch.data_volume("/dev/disk4")


def test_the_suite_cannot_reach_the_real_secrets():
    # Running pytest once deleted a live VM's password and both key pairs. The
    # autouse fixture is what stops that; this is what stops the fixture being
    # dropped.
    real = Path(os.path.expanduser("~")) / ".config" / "chrome-in-a-box"
    for path in (cib.SECRETS, cib.CREDENTIALS, cib.VM_KEY, cib.VM_HOST_KEY, cib.KNOWN_HOSTS):
        assert real not in path.parents and path != real, f"{path} is the user's own"
    assert cib.Path.home() != Path(os.path.expanduser("~")), "Path.home() is not redirected"


def test_chrome_is_pointed_at_the_share_rather_than_moving_downloads(tmp_path):
    # macOS protects ~/Downloads against being renamed, and a process arriving over
    # ssh has no TCC grant for it, so `mv` there fails with EPERM no matter what the
    # permissions say. Proven on a real guest.
    import json as _json

    (tmp_path / "Downloads").mkdir()
    result = _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=True)
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
    _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=True)
    link = tmp_path / "Downloads" / "on-the-host"
    assert link.is_symlink()
    assert str(link.readlink()) == str(tmp_path / "share")


def test_an_existing_chrome_profile_is_not_overwritten(tmp_path):
    # Rewriting Preferences would throw away every setting the user has made.
    (tmp_path / "Downloads").mkdir()
    prefs = tmp_path / "Library/Application Support/Google/Chrome/Default/Preferences"
    prefs.parent.mkdir(parents=True)
    prefs.write_text('{"mine": true}')
    result = _run_guest_script(cib.guest_install_script("pw"), tmp_path, share_exists=True)
    assert result.returncode == 0, result.stderr
    assert prefs.read_text() == '{"mine": true}'
    assert "already has a profile" in result.stderr


def test_the_download_preferences_are_valid_json():
    import json as _json

    written = _json.loads(cib.DOWNLOAD_PREFS)
    assert written["download"]["default_directory"] == cib.GUEST_SHARE


@pytest.mark.parametrize(
    "tool", ["python3", "python", "git", "make", "gcc", "clang", "cc", "svn", "jq"]
)
def test_the_guest_script_needs_nothing_the_guest_does_not_have(tool):
    # A fresh macOS has no Command Line Tools. Any of these is a stub that opens
    # "The <tool> command requires the command line developer tools" — a dialog on
    # the guest's screen, waiting for a click, from a command that is supposed to
    # need none. Seen for real, from a diagnostic that used python3 in the guest.
    script = cib.guest_install_script("pw", "Europe/Rome")
    body = "\n".join(ln for ln in script.split("\n") if not ln.lstrip().startswith("#"))
    assert not re.search(rf"(^|[\s|;&(]){re.escape(tool)}([\s;&)]|$)", body), (
        f"{tool} is not on a bare macOS; using it turns 'cib vm setup' into a dialog"
    )


def test_the_guest_never_locks_its_screen(tmp_path):
    # A lock screen asks for the generated 24-character password, and a VM has no
    # Touch ID to shortcut it — so the one thing the password exists to avoid.
    (tmp_path / "Downloads").mkdir()
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True)
    for tool in ("defaults", "pmset", "sysadminctl"):
        (bin_dir / tool).write_text(f'#!/bin/sh\necho "{tool} $@" >> {tmp_path}/lock.log\n')
        (bin_dir / tool).chmod(0o755)
    result = _run_guest_script(
        cib.guest_install_script("pw"), tmp_path, share_exists=True, extra_bin=bin_dir
    )
    assert result.returncode == 0, result.stderr
    log = (tmp_path / "lock.log").read_text()
    assert "screensaver idleTime -int 0" in log, "the screensaver would still start"
    assert "screensaver askForPassword -int 0" in log, "it would still ask"
    assert "pmset -a displaysleep 0 sleep 0" in log, "the display would still sleep"


def test_a_guest_without_sysadminctl_still_finishes(tmp_path):
    # The flag is macOS 14 and later; on an older guest the command is absent, and
    # under `sh -e` an unguarded failure would abort the whole install.
    (tmp_path / "Downloads").mkdir()
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(parents=True)
    for tool in ("defaults", "pmset"):
        (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / tool).chmod(0o755)
    (bin_dir / "sysadminctl").write_text("#!/bin/sh\nexit 127\n")
    (bin_dir / "sysadminctl").chmod(0o755)
    result = _run_guest_script(
        cib.guest_install_script("pw"), tmp_path, share_exists=True, extra_bin=bin_dir
    )
    assert result.returncode == 0, result.stderr


def test_the_vnc_viewer_is_how_the_window_goes_full_screen(monkeypatch):
    # tart's own window has neither full screen nor scaling; Screen Sharing has
    # both, and the offline patch already turns on the Remote Login it needs.
    monkeypatch.setenv("CIB_VM_VIEWER", "vnc")
    assert "--vnc" in cib.vm_run_args(cib.VmConfig())


def test_the_built_in_window_stays_the_default(monkeypatch):
    assert "--vnc" not in cib.vm_run_args(cib.VmConfig())


def test_an_unknown_viewer_is_refused(monkeypatch):
    monkeypatch.setenv("CIB_VM_VIEWER", "kiosk")
    with pytest.raises(cib.Failure, match="CIB_VM_VIEWER"):
        cib.vm_run_args(cib.VmConfig())


# --- one command instead of three ----------------------------------------------


def test_create_boots_the_guest_and_installs_chrome_itself(
    calls, credentials, monkeypatch, tmp_path
):
    # It used to stop after patching and print three more commands to run. Each of
    # them is a place a build can fail, and each failure meant starting over.
    order: list[str] = []
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(cib.subprocess, "Popen", lambda *a, **k: _FakeBoot())
    monkeypatch.setattr(
        cib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0)
    )
    monkeypatch.setattr(cib, "start_detached", lambda t, vm: order.append("start") or _FakeBoot())
    monkeypatch.setattr(cib, "wait_for_guest", lambda t, vm, b: order.append("wait") or "10.0.0.9")
    monkeypatch.setattr(
        cib, "guest_ssh", lambda vm, ip, script=None: order.append(f"ssh:{ip}") or 0
    )
    cib.cmd_vm_create("tart", cib.VmConfig())
    assert order == ["start", "wait", "ssh:10.0.0.9"]


def test_create_says_what_is_left_when_the_install_fails(calls, credentials, monkeypatch, tmp_path):
    # The guest is built and running by then; telling the user to start over would
    # throw away half an hour for a step that retries on its own.
    monkeypatch.setattr(cib, "vm_exists", lambda *a, **k: False)
    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    _fake_guest_disk(monkeypatch, tmp_path)
    monkeypatch.setattr(cib.subprocess, "Popen", lambda *a, **k: _FakeBoot())
    monkeypatch.setattr(
        cib.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0)
    )
    monkeypatch.setattr(cib, "guest_ssh", lambda vm, ip, script=None: 1)
    with pytest.raises(cib.Failure, match="cib vm setup"):
        cib.cmd_vm_create("tart", cib.VmConfig())


def test_a_guest_that_dies_while_starting_is_not_waited_out(isolate_secrets, monkeypatch):
    # Five minutes of polling for an address that will never come.
    class _Died(_FakeBoot):
        returncode = 3

        @property
        def stderr(self):
            import io

            return io.StringIO("bridged networking failed")

        def poll(self):
            return 3

    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    with pytest.raises(cib.Failure, match="bridged networking failed"):
        isolate_secrets.wait_for_guest("tart", cib.VmConfig(), _Died())


def test_a_guest_that_never_answers_points_at_setup(isolate_secrets, monkeypatch):
    monkeypatch.setattr(cib.time, "sleep", lambda s: None)
    monkeypatch.setattr(cib, "GUEST_WAIT_SECS", 0)
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="", stderr="")
    )
    with pytest.raises(cib.Failure, match="cib vm setup"):
        isolate_secrets.wait_for_guest("tart", cib.VmConfig(), _FakeBoot())


def test_no_test_can_start_a_real_vm():
    # start_detached uses subprocess.Popen directly, so a test that replaces only
    # `cib.run` spawned tart for real and then sat in wait_for_guest for five
    # minutes. The autouse fixture is what stops that.
    assert cib.start_detached("tart", cib.VmConfig()).poll() is None
    assert cib.wait_for_guest("tart", cib.VmConfig(), _FakeBoot()) == "192.168.1.50"


def test_the_address_the_guest_last_answered_on_is_remembered(credentials, monkeypatch):
    # The host's arp table forgets a guest that has been quiet, and `tart ip --wait`
    # only re-reads that table — it sends nothing that would repopulate it. Hit
    # twice on a real guest that was pingable the whole time.
    monkeypatch.setattr(
        cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="10.0.0.9\n")
    )
    assert cib.vm_ip("tart", cib.VmConfig()) == "10.0.0.9"
    assert cib.LAST_IP.read_text().strip() == "10.0.0.9"

    monkeypatch.setattr(cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=""))
    monkeypatch.setattr(cib, "guest_answers", lambda ip: ip == "10.0.0.9")
    assert cib.vm_ip("tart", cib.VmConfig()) == "10.0.0.9"


def test_a_remembered_address_that_answers_nothing_is_not_used(credentials, monkeypatch):
    # A guest that has really gone needs the error, not an address that will time
    # out on every command after it.
    cib.LAST_IP.parent.mkdir(parents=True, exist_ok=True)
    cib.LAST_IP.write_text("10.0.0.9\n")
    monkeypatch.setattr(cib, "run", lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=""))
    monkeypatch.setattr(cib, "guest_answers", lambda ip: False)
    with pytest.raises(cib.Failure, match="cib vm status"):
        cib.vm_ip("tart", cib.VmConfig())


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

    monkeypatch.setattr(cib.socket, "socket", lambda *a, **k: _Probe())
    assert cib.guest_answers("10.0.0.9") is True
    assert seen["addr"] == ("10.0.0.9", 22)
    assert seen["timeout"] <= 5, "a dead guest must not hold the command up"
