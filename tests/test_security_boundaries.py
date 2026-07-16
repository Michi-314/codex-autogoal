from __future__ import annotations

import stat
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codex_autogoal import paths
from codex_autogoal.config import Config
from codex_autogoal.runner import run_codex_session
from codex_autogoal import cli
from codex_autogoal.watcher import _read_job_status


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
