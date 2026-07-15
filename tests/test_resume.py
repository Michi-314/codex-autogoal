"""resume.py の単体テスト"""

import json
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codex_autogoal.config import Config
from codex_autogoal import paths
from codex_autogoal.state import (
    SessionState,
    SessionStatus,
    StateManager,
    now_iso,
)
from codex_autogoal.resume import resume_session, _RETRY_DELAYS


@pytest.fixture
def config(tmp_path):
    return Config(home=tmp_path / "autogoal", max_resume_attempts=2)


@pytest.fixture
def setup_session(config):
    sid = "test-resume-session"
    sdir = paths.session_dir(config, sid)
    sdir.mkdir(parents=True)

    mgr = StateManager(sdir)
    state = SessionState(
        session_id=sid,
        status=SessionStatus.RESUMING,
        cwd="/tmp",
        created_at=now_iso(),
    )
    mgr.write(state)

    # codex.jsonl、resume.logの親ディレクトリ
    paths.codex_jsonl(config, sid).touch()
    paths.resume_log(config, sid).touch()

    return sid, mgr


@pytest.fixture
def logger():
    return logging.getLogger("test_resume")


class TestResumeSession:
    def test_cancelled_during_resume(self, config, setup_session, logger):
        """resume中にキャンセルされた場合"""
        sid, mgr = setup_session

        # キャンセルマーカー作成
        paths.cancelled_marker(config, sid).touch()

        result = resume_session(
            config=config,
            session_id=sid,
            resume_message="test",
            cwd="/tmp",
            state_manager=mgr,
            logger=logger,
        )

        assert not result
        state = mgr.read()
        assert state.status == SessionStatus.CANCELLED

    def test_wrong_state_skips(self, config, setup_session, logger):
        """状態がRESUMINGでない場合はスキップ"""
        sid, mgr = setup_session

        state = mgr.read()
        state.status = SessionStatus.DONE
        mgr.write(state)

        result = resume_session(
            config=config,
            session_id=sid,
            resume_message="test",
            cwd="/tmp",
            state_manager=mgr,
            logger=logger,
        )

        assert not result
