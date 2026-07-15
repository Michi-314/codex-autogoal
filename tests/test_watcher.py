"""watcher.py の単体テスト"""

import json
import time
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
from codex_autogoal.watcher import _watch_loop, _build_resume_message
from codex_autogoal.job_runner import _finalize_job


@pytest.fixture
def config(tmp_path):
    return Config(home=tmp_path / "autogoal")


@pytest.fixture
def setup_session(config):
    """WAITINGセッションとジョブをセットアップ"""
    sid = "test-session-watcher"
    job_id = "test-job-001"

    sdir = paths.session_dir(config, sid)
    sdir.mkdir(parents=True)

    mgr = StateManager(sdir)
    state = SessionState(
        session_id=sid,
        status=SessionStatus.WAITING,
        cwd="/tmp",
        current_job_id=job_id,
        created_at=now_iso(),
    )
    mgr.write(state)

    # ジョブディレクトリ作成
    jdir = paths.job_dir(config, job_id)
    jdir.mkdir(parents=True)

    return sid, job_id, mgr


class TestBuildResumeMessage:
    def test_message_contains_job_info(self, config):
        job_id = "test-job-msg"
        jdir = paths.job_dir(config, job_id)
        jdir.mkdir(parents=True)

        # ジョブステータスを作成
        status_data = {
            "job_id": job_id,
            "status": "SUCCEEDED",
            "exit_code": 0,
        }
        paths.job_status_json(config, job_id).write_text(json.dumps(status_data))

        msg = _build_resume_message(config, job_id, status_data)
        assert job_id in msg
        assert "SUCCEEDED" in msg
        assert "exit_code: 0" in msg

    def test_failed_job_message(self, config):
        job_id = "test-job-fail-msg"
        jdir = paths.job_dir(config, job_id)
        jdir.mkdir(parents=True)

        status_data = {
            "job_id": job_id,
            "status": "FAILED",
            "exit_code": 7,
        }
        paths.job_status_json(config, job_id).write_text(json.dumps(status_data))

        msg = _build_resume_message(config, job_id, status_data)
        assert "FAILED" in msg
        assert "exit_code: 7" in msg


class TestWatchLoop:
    def test_detects_done(self, config, setup_session):
        """doneファイル検出でループ終了"""
        sid, job_id, mgr = setup_session
        import logging
        logger = logging.getLogger("test_watcher")

        # 別スレッドでdoneを作成
        import threading

        def create_done():
            time.sleep(0.5)
            _finalize_job(config, job_id, 0, ["echo"])

        t = threading.Thread(target=create_done)
        t.start()

        with patch("codex_autogoal.watcher.resume_session", return_value=True), \
             patch("codex_autogoal.watcher.POLL_INTERVAL", 0.1):
            _watch_loop(config, sid, job_id, logger)

        t.join()

    def test_cancelled_exits(self, config, setup_session):
        """キャンセル時にループ終了"""
        sid, job_id, mgr = setup_session
        import logging
        logger = logging.getLogger("test_watcher")

        # キャンセルマーカー作成
        paths.cancelled_marker(config, sid).touch()

        with patch("codex_autogoal.watcher.POLL_INTERVAL", 0.1):
            _watch_loop(config, sid, job_id, logger)

        # ジョブは完了していない
        assert not paths.job_done_marker(config, job_id).exists()
