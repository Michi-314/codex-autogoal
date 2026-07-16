"""Stop Hook の単体テスト"""

import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_autogoal.config import Config
from codex_autogoal import paths
from codex_autogoal.hooks.stop import main as stop_hook_main
from codex_autogoal.state import (
    SessionState,
    SessionStatus,
    StateManager,
    now_iso,
)


@pytest.fixture
def config(tmp_path):
    home = tmp_path / "autogoal"
    home.mkdir()
    return Config(home=home, enabled=True, max_turns=5)


@pytest.fixture
def session_dir(config):
    sid = "test-session-001"
    sdir = paths.session_dir(config, sid)
    sdir.mkdir(parents=True)
    return sdir, sid


def _run_hook(config, hook_input: dict, capsys) -> dict:
    """Stop Hookを実行し、出力JSONを返す"""
    input_json = json.dumps(hook_input)

    env = {
        "CODEX_AUTOGOAL_ENABLED": "1",
        "CODEX_AUTOGOAL_HOME": str(config.home),
    }

    with patch.dict(os.environ, env), \
         patch("sys.stdin", io.StringIO(input_json)), \
         patch("codex_autogoal.hooks.stop.load_config", return_value=config):
        stop_hook_main()

    captured = capsys.readouterr()
    return json.loads(captured.out.strip())


class TestDisabled:
    def test_passthrough_when_disabled(self, tmp_path, capsys):
        """CODEX_AUTOGOAL_ENABLED!=1の場合はパススルー"""
        config = Config(home=tmp_path / "autogoal", enabled=False)

        input_json = json.dumps({"session_id": "x", "last_assistant_message": "test"})

        with patch.dict(os.environ, {"CODEX_AUTOGOAL_ENABLED": "0"}), \
             patch("sys.stdin", io.StringIO(input_json)), \
             patch("codex_autogoal.hooks.stop.load_config", return_value=config):
            stop_hook_main()

        captured = capsys.readouterr()
        result = json.loads(captured.out.strip())
        assert result.get("continue") is True


class TestContinue:
    def test_continue_returns_block(self, config, session_dir, capsys):
        """continueシグナルでblock応答を返す"""
        sdir, sid = session_dir

        # RUNNING状態を作成
        mgr = StateManager(sdir)
        state = SessionState(session_id=sid, status=SessionStatus.RUNNING,
                             cwd="/tmp", created_at=now_iso())
        mgr.write(state)

        result = _run_hook(config, {
            "session_id": sid,
            "last_assistant_message": 'テスト結果OK\nAUTOGOAL_SIGNAL: {"state":"continue","reason":"次のテスト"}',
            "cwd": "/tmp",
            "stop_hook_active": False,
        }, capsys)

        assert result.get("decision") == "block"

    def test_max_turns(self, config, session_dir, capsys):
        """最大ターン数到達で停止"""
        sdir, sid = session_dir

        mgr = StateManager(sdir)
        state = SessionState(
            session_id=sid,
            status=SessionStatus.RUNNING,
            cwd="/tmp",
            created_at=now_iso(),
            continuation_count=4,  # max_turns=5なので、次で到達
        )
        mgr.write(state)

        result = _run_hook(config, {
            "session_id": sid,
            "last_assistant_message": 'AUTOGOAL_SIGNAL: {"state":"continue","reason":"次"}',
            "cwd": "/tmp",
            "stop_hook_active": False,
        }, capsys)

        # 停止する
        assert result.get("continue") is True  # Codexを停止

        read = mgr.read()
        assert read.status == SessionStatus.BLOCKED_LIMIT


class TestDone:
    def test_done_stops(self, config, session_dir, capsys):
        """doneシグナルで停止"""
        sdir, sid = session_dir

        mgr = StateManager(sdir)
        state = SessionState(session_id=sid, status=SessionStatus.RUNNING,
                             cwd="/tmp", created_at=now_iso())
        mgr.write(state)

        result = _run_hook(config, {
            "session_id": sid,
            "last_assistant_message": 'AUTOGOAL_SIGNAL: {"state":"done","reason":"完了"}',
            "cwd": "/tmp",
            "stop_hook_active": False,
        }, capsys)

        read = mgr.read()
        assert read.status == SessionStatus.DONE

    def test_last_message_symlink_does_not_overwrite_external_file(
        self, config, session_dir, tmp_path, capsys
    ):
        """The unsandboxed Stop hook must never follow a model-created symlink."""
        sdir, sid = session_dir
        mgr = StateManager(sdir)
        mgr.write(SessionState(
            session_id=sid,
            status=SessionStatus.RUNNING,
            cwd="/tmp",
            created_at=now_iso(),
        ))
        external = tmp_path / "shell-rc"
        external.write_text("safe\n")
        paths.last_message_txt(config, sid).symlink_to(external)

        _run_hook(config, {
            "session_id": sid,
            "last_assistant_message": (
                'attacker controlled\n'
                'AUTOGOAL_SIGNAL: {"state":"done","reason":"complete"}'
            ),
            "cwd": "/tmp",
            "stop_hook_active": False,
        }, capsys)

        assert external.read_text() == "safe\n"
        assert paths.last_message_txt(config, sid).is_symlink()


class TestBlocked:
    def test_blocked_stops(self, config, session_dir, capsys):
        """blockedシグナルで停止"""
        sdir, sid = session_dir

        mgr = StateManager(sdir)
        state = SessionState(session_id=sid, status=SessionStatus.RUNNING,
                             cwd="/tmp", created_at=now_iso())
        mgr.write(state)

        result = _run_hook(config, {
            "session_id": sid,
            "last_assistant_message": 'AUTOGOAL_SIGNAL: {"state":"blocked","reason":"APIキー必要"}',
            "cwd": "/tmp",
            "stop_hook_active": False,
        }, capsys)

        read = mgr.read()
        assert read.status == SessionStatus.BLOCKED


class TestMalformedSignal:
    def test_no_signal(self, config, session_dir, capsys):
        """シグナルなしで安全停止"""
        sdir, sid = session_dir

        mgr = StateManager(sdir)
        state = SessionState(session_id=sid, status=SessionStatus.RUNNING,
                             cwd="/tmp", created_at=now_iso())
        mgr.write(state)

        result = _run_hook(config, {
            "session_id": sid,
            "last_assistant_message": "普通のメッセージ",
            "cwd": "/tmp",
            "stop_hook_active": False,
        }, capsys)

        read = mgr.read()
        assert read.status == SessionStatus.BLOCKED_PROTOCOL_ERROR

    def test_broken_json(self, config, session_dir, capsys):
        sdir, sid = session_dir

        mgr = StateManager(sdir)
        state = SessionState(session_id=sid, status=SessionStatus.RUNNING,
                             cwd="/tmp", created_at=now_iso())
        mgr.write(state)

        _run_hook(config, {
            "session_id": sid,
            "last_assistant_message": 'AUTOGOAL_SIGNAL: {broken',
            "cwd": "/tmp",
            "stop_hook_active": False,
        }, capsys)

        read = mgr.read()
        assert read.status == SessionStatus.BLOCKED_PROTOCOL_ERROR


class TestLoopDetection:
    def test_loop_detected(self, config, session_dir, capsys):
        """同一メッセージ3回連続で停止"""
        sdir, sid = session_dir

        mgr = StateManager(sdir)

        from codex_autogoal.protocol import normalize_message_hash
        msg = 'テスト\nAUTOGOAL_SIGNAL: {"state":"continue","reason":"同じ"}'
        msg_hash = normalize_message_hash(msg)

        state = SessionState(
            session_id=sid,
            status=SessionStatus.RUNNING,
            cwd="/tmp",
            created_at=now_iso(),
            recent_message_hashes=[msg_hash, msg_hash],
        )
        mgr.write(state)

        result = _run_hook(config, {
            "session_id": sid,
            "last_assistant_message": msg,
            "cwd": "/tmp",
            "stop_hook_active": False,
        }, capsys)

        read = mgr.read()
        assert read.status == SessionStatus.BLOCKED_LOOP_DETECTED
