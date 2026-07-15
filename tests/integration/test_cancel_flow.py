"""統合テスト: キャンセル・malformed signal・通常セッション（シナリオE, F, G）"""

import io
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_autogoal.config import Config
from codex_autogoal import paths
from codex_autogoal.state import (
    SessionState,
    SessionStatus,
    StateManager,
    now_iso,
)
from codex_autogoal.hooks.stop import main as stop_hook_main


@pytest.fixture
def config(tmp_path):
    home = tmp_path / "autogoal"
    home.mkdir()
    (home / "state").mkdir()
    (home / "jobs").mkdir()
    return Config(home=home, enabled=True, max_turns=50)


def _run_stop_hook(config, session_id, last_message, capsys) -> dict:
    hook_input = {
        "session_id": session_id,
        "last_assistant_message": last_message,
        "cwd": "/tmp",
        "stop_hook_active": False,
    }
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


class TestMalformedSignal:
    def test_scenario_f_no_auto_continue(self, config, capsys):
        """不正シグナルで自動継続しない"""
        session_id = "test-malformed"

        sdir = paths.session_dir(config, session_id)
        sdir.mkdir(parents=True)
        mgr = StateManager(sdir)
        state = SessionState(
            session_id=session_id,
            status=SessionStatus.RUNNING,
            cwd="/tmp",
            created_at=now_iso(),
        )
        mgr.write(state)

        # 壊れたJSON
        _run_stop_hook(config, session_id,
            'AUTOGOAL_SIGNAL: {broken json',
            capsys)

        state = mgr.read()
        assert state.status == SessionStatus.BLOCKED_PROTOCOL_ERROR

    def test_no_signal_blocks(self, config, capsys):
        """シグナルなしでBLOCKED_PROTOCOL_ERROR"""
        session_id = "test-no-signal"

        sdir = paths.session_dir(config, session_id)
        sdir.mkdir(parents=True)
        mgr = StateManager(sdir)
        state = SessionState(
            session_id=session_id,
            status=SessionStatus.RUNNING,
            cwd="/tmp",
            created_at=now_iso(),
        )
        mgr.write(state)

        _run_stop_hook(config, session_id,
            '普通のメッセージです',
            capsys)

        state = mgr.read()
        assert state.status == SessionStatus.BLOCKED_PROTOCOL_ERROR


class TestNormalCodex:
    def test_scenario_g_no_interference(self, tmp_path, capsys):
        """CODEX_AUTOGOAL_ENABLEDなしで通常動作に干渉しない"""
        config = Config(home=tmp_path / "autogoal", enabled=False)

        hook_input = {
            "session_id": "normal-session",
            "last_assistant_message": "通常のメッセージ",
            "cwd": "/tmp",
            "stop_hook_active": False,
        }
        input_json = json.dumps(hook_input)

        with patch.dict(os.environ, {"CODEX_AUTOGOAL_ENABLED": "0"}, clear=False), \
             patch("sys.stdin", io.StringIO(input_json)), \
             patch("codex_autogoal.hooks.stop.load_config", return_value=config):
            stop_hook_main()

        captured = capsys.readouterr()
        result = json.loads(captured.out.strip())

        # パススルー（通常動作に干渉しない）
        assert result.get("continue") is True
        # blockやrejectは出さない
        assert "decision" not in result


class TestCancelFlow:
    def test_cancel_during_running(self, config, capsys):
        """実行中にキャンセル"""
        session_id = "test-cancel-running"

        sdir = paths.session_dir(config, session_id)
        sdir.mkdir(parents=True)
        mgr = StateManager(sdir)
        state = SessionState(
            session_id=session_id,
            status=SessionStatus.RUNNING,
            cwd="/tmp",
            created_at=now_iso(),
        )
        mgr.write(state)

        # cancelledマーカー作成
        paths.cancelled_marker(config, session_id).touch()

        _run_stop_hook(config, session_id,
            'AUTOGOAL_SIGNAL: {"state":"continue","reason":"次"}',
            capsys)

        state = mgr.read()
        assert state.status == SessionStatus.CANCELLED
