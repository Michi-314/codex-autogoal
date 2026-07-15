"""Process-level checks against the fake Codex executable."""

from __future__ import annotations

import logging
from pathlib import Path

from codex_autogoal import paths
from codex_autogoal.config import Config
from codex_autogoal.resume import resume_session
from codex_autogoal.runner import run_codex_session
from codex_autogoal.state import SessionState, SessionStatus, StateManager, now_iso


FAKE_CODEX = Path(__file__).with_name("fake_codex.py")


def test_exec_uses_codex_thread_id(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = Config(home=home, codex_bin=str(FAKE_CODEX))
    monkeypatch.setenv("FAKE_CODEX_THREAD_ID", "fake-thread-process")
    monkeypatch.setenv("FAKE_CODEX_SCENARIO", "simple_done")

    assert run_codex_session(config, "test", cwd=str(tmp_path), model="fake-model") == 0
    state = StateManager(paths.session_dir(config, "fake-thread-process")).read()
    assert state is not None
    assert state.session_id == "fake-thread-process"
    assert paths.codex_jsonl(config, state.session_id).exists()


def test_resume_same_session_once(tmp_path, monkeypatch):
    home = tmp_path / "home"
    config = Config(home=home, codex_bin=str(FAKE_CODEX), max_resume_attempts=1)
    session_id = "fake-thread-resume"
    manager = StateManager(paths.session_dir(config, session_id))
    manager.ensure_dir()
    manager.write(SessionState(
        session_id=session_id,
        cwd=str(tmp_path),
        status=SessionStatus.RESUMING,
        created_at=now_iso(),
    ))
    paths.codex_jsonl(config, session_id).touch()
    monkeypatch.setenv("FAKE_CODEX_RESUME_SCENARIO", "done_after_resume")

    assert resume_session(
        config=config,
        session_id=session_id,
        resume_message="job complete",
        cwd=str(tmp_path),
        state_manager=manager,
        logger=logging.getLogger("fake-resume"),
    )
    state = manager.read()
    assert state is not None
    assert state.status == SessionStatus.RUNNING
    assert state.resume_count == 1
    assert state.token_usage.input_tokens == 200
    assert '"thread_id": "fake-thread-resume"' in paths.codex_jsonl(
        config, session_id
    ).read_text()
