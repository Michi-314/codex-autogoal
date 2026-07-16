from __future__ import annotations

import os
import stat
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codex_autogoal import paths
from codex_autogoal.config import Config
from codex_autogoal.runner import run_codex_session
from codex_autogoal import cli
from codex_autogoal.watcher import _read_job_status
from codex_autogoal.process import sanitized_environment


@pytest.mark.parametrize("value", ["../escape", "a/b", "", ".", "x" * 129])
def test_session_and_job_paths_reject_invalid_identifiers(tmp_path, value):
    config = Config(home=tmp_path / "home")
    with pytest.raises(ValueError):
        paths.session_dir(config, value)
    with pytest.raises(ValueError):
        paths.job_dir(config, value)
    assert paths.resolve_job_dir_safe(config, value) is None


def test_harden_runtime_permissions(tmp_path):
    config = Config(home=tmp_path / "home")
    nested = config.home / "state" / "session"
    nested.mkdir(parents=True, mode=0o755)
    log = nested / "codex.jsonl"
    log.write_text("sensitive")
    log.chmod(0o644)

    paths.harden_runtime_permissions(config)

    assert stat.S_IMODE(config.home.stat().st_mode) == 0o700
    assert stat.S_IMODE(nested.stat().st_mode) == 0o700
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


@pytest.mark.parametrize("linked_component", ["state", "jobs"])
def test_harden_quarantines_home_when_control_root_is_symlink(
    tmp_path, linked_component
):
    config = Config(home=tmp_path / "home")
    outside = tmp_path / "outside"
    outside.mkdir()
    config.home.mkdir()
    (config.home / linked_component).symlink_to(outside, target_is_directory=True)

    quarantine = paths.harden_runtime_permissions(config)

    assert quarantine is not None
    assert quarantine.parent == config.home.parent
    assert (quarantine / linked_component).is_symlink()
    assert config.home.is_dir()
    assert not config.home.is_symlink()
    assert list(config.home.iterdir()) == []


def test_harden_quarantines_symlinked_runtime_home(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    config = Config(home=tmp_path / "home")
    config.home.symlink_to(outside, target_is_directory=True)

    quarantine = paths.harden_runtime_permissions(config)

    assert quarantine is not None and quarantine.is_symlink()
    assert config.home.is_dir() and not config.home.is_symlink()
    assert list(outside.iterdir()) == []


def test_harden_quarantines_special_control_node(tmp_path):
    config = Config(home=tmp_path / "home")
    session = config.home / "state" / "session"
    session.mkdir(parents=True)
    os.mkfifo(session / "codex.jsonl")

    quarantine = paths.harden_runtime_permissions(config)

    assert quarantine is not None
    assert stat.S_ISFIFO((quarantine / "state/session/codex.jsonl").lstat().st_mode)
    assert config.home.is_dir()


def test_harden_quarantines_hardlinked_control_file(tmp_path):
    config = Config(home=tmp_path / "home")
    session = config.home / "state" / "session"
    session.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("must remain unchanged")
    os.link(outside, session / "codex.jsonl")

    quarantine = paths.harden_runtime_permissions(config)

    assert quarantine is not None
    assert outside.read_text() == "must remain unchanged"
    assert list(config.home.iterdir()) == []
    assert (quarantine / "state/session/codex.jsonl").stat().st_nlink == 2


def test_sanitized_environment_drops_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("GH_TOKEN", "secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    env = sanitized_environment()

    assert env["PATH"] == "/usr/bin"
    assert env["LANG"] == "C.UTF-8"
    assert "GH_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_sanitized_environment_can_explicitly_inherit(monkeypatch):
    monkeypatch.setenv("CUSTOM_SECRET", "allowed-only-by-opt-in")
    assert sanitized_environment(inherit=True)["CUSTOM_SECRET"] == "allowed-only-by-opt-in"


def test_existing_identifier_symlink_cannot_escape_root(tmp_path):
    config = Config(home=tmp_path / "home")
    outside = tmp_path / "outside"
    outside.mkdir()
    paths.state_dir(config).mkdir(parents=True)
    (paths.state_dir(config) / "valid-id").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError):
        paths.session_dir(config, "valid-id")


def test_private_write_refuses_final_component_symlink(tmp_path):
    target = tmp_path / "outside"
    target.write_text("unchanged")
    link = tmp_path / "control"
    link.symlink_to(target)

    with pytest.raises(OSError):
        paths.write_private_text(link, "attacker controlled")

    assert target.read_text() == "unchanged"


@pytest.mark.parametrize("writer", [paths.write_private_text, paths.open_private_write])
def test_private_write_refuses_hardlinked_file_without_truncating(tmp_path, writer):
    outside = tmp_path / "outside"
    outside.write_text("must remain unchanged")
    control = tmp_path / "control"
    os.link(outside, control)

    with pytest.raises(ValueError, match="multiple hard links"):
        result = writer(control, "attacker controlled") if writer is paths.write_private_text else writer(control)
        if result is not None:
            result.close()

    assert outside.read_text() == "must remain unchanged"


def test_private_read_refuses_hardlinked_file(tmp_path):
    outside = tmp_path / "outside"
    outside.write_text("sensitive")
    outside.chmod(0o600)
    control = tmp_path / "control"
    os.link(outside, control)

    with pytest.raises(ValueError, match="multiple hard links"):
        paths.read_private_text(control)


def test_codex_command_never_adds_control_home(tmp_path):
    config = Config(home=tmp_path / "control")
    proc = MagicMock()
    proc.stdout = []
    proc.wait.return_value = 0
    with patch("codex_autogoal.runner.subprocess.Popen", return_value=proc) as popen:
        assert run_codex_session(config, "prompt", cwd=str(tmp_path)) == 0

    command = popen.call_args.args[0]
    assert "--add-dir" not in command
    assert str(config.home) not in command


def test_runtime_protocol_poisoning_is_ignored(tmp_path):
    config = Config(home=tmp_path / "control")
    config.home.mkdir()
    paths.protocol_file(config).write_text("PERSISTENT MALICIOUS PROTOCOL")
    args = SimpleNamespace(
        prompt="safe task",
        prompt_file=None,
        cwd=str(tmp_path),
        sandbox="workspace-write",
        model=None,
        bypass_hook_trust=False,
    )
    with patch("codex_autogoal.cli.run_codex_session", return_value=0) as run:
        with pytest.raises(SystemExit) as exited:
            cli._cmd_start(config, args)

    assert exited.value.code == 0
    prompt = run.call_args.args[1]
    assert "PERSISTENT MALICIOUS PROTOCOL" not in prompt
    assert "AutoGoalモード" in prompt


def test_job_status_control_characters_are_rejected(tmp_path):
    config = Config(home=tmp_path / "control")
    job_id = "job-safe"
    status_path = paths.job_status_json(config, job_id)
    paths.write_private_text(
        status_path,
        '{"status":"SUCCEEDED\\ntouch /tmp/pwned","exit_code":0}',
    )

    assert _read_job_status(config, job_id) == {"status": "UNKNOWN", "exit_code": -1}
