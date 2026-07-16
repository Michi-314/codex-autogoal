from __future__ import annotations

import stat

import pytest

from codex_autogoal import paths
from codex_autogoal.config import Config


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
