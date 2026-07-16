"""既存threadへのjob接続とvisible resumeのテスト。"""

from __future__ import annotations

from unittest.mock import patch

from codex_autogoal import paths
from codex_autogoal.attachment import attach_job
from codex_autogoal.config import Config
from codex_autogoal.state import SessionState, SessionStatus, StateManager, now_iso


def test_attach_job_forces_headless_resume(tmp_path):
    config = Config(home=tmp_path / "autogoal")
    session_id = "thread-123"
    job_id = "job-123"
    paths.job_dir(config, job_id).mkdir(parents=True)
    mgr = StateManager(paths.session_dir(config, session_id))
    mgr.ensure_dir()
    mgr.write(SessionState(
        session_id=session_id,
        cwd="/tmp",
        status=SessionStatus.RUNNING,
        created_at=now_iso(),
    ))

    with patch("codex_autogoal.attachment.launch_watcher", return_value=321) as launch:
        pid = attach_job(
            config,
            session_id=session_id,
            job_id=job_id,
            cwd="/tmp/project",
            pane_id="7",
        )

    state = mgr.read()
    assert pid == 321
    assert state.status == SessionStatus.WAITING
    assert state.current_job_id == job_id
    assert state.resume_mode == "headless"
    assert state.terminal_pane_id is None
    launch.assert_called_once()
