"""既存threadへのjob接続とvisible resumeのテスト。"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from codex_autogoal import paths
from codex_autogoal.attachment import attach_job
from codex_autogoal.config import Config
from codex_autogoal.state import SessionState, SessionStatus, StateManager, now_iso
from codex_autogoal.watcher import _resume_visible


def test_attach_job_records_wait_and_visible_target(tmp_path):
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
    assert state.resume_mode == "wezterm"
    assert state.terminal_pane_id == "7"
    launch.assert_called_once()


def test_visible_resume_sends_message_then_enter_separately(tmp_path):
    config = Config(home=tmp_path / "autogoal")
    session_id = "thread-visible"
    mgr = StateManager(paths.session_dir(config, session_id))
    mgr.ensure_dir()
    state = SessionState(
        session_id=session_id,
        cwd="/tmp",
        status=SessionStatus.RESUMING,
        created_at=now_iso(),
        resume_mode="wezterm",
        terminal_pane_id="9",
    )
    mgr.write(state)
    completed = MagicMock(returncode=0, stderr="")

    with patch("codex_autogoal.watcher.shutil.which", return_value="/bin/wezterm"), \
         patch("codex_autogoal.watcher.subprocess.run", return_value=completed) as run:
        assert _resume_visible(state, "resume message", mgr, logging.getLogger("test"))

    assert [call.kwargs["input"] for call in run.call_args_list] == [
        "resume message", "\r", "\r",
    ]
    updated = mgr.read()
    assert updated.status == SessionStatus.RUNNING
    assert updated.resume_count == 1
