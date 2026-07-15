"""統合テスト: continueフロー（シナリオA）

シナリオ: turn 1 → continue → turn 2 → continue → turn 3 → done
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_autogoal.config import Config
from codex_autogoal import paths
from codex_autogoal.state import SessionStatus, StateManager
from codex_autogoal.hooks.stop import main as stop_hook_main

import io


@pytest.fixture
def config(tmp_path):
    home = tmp_path / "autogoal"
    home.mkdir()
    (home / "state").mkdir()
    (home / "jobs").mkdir()
    return Config(home=home, enabled=True, max_turns=50)


def _run_stop_hook(config, session_id, last_message, capsys) -> dict:
    """Stop Hookを実行"""
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


class TestContinueFlow:
    def test_scenario_a_continue_to_done(self, config, capsys):
        """3ターンの自動継続 → done"""
        session_id = "test-continue-flow"

        # セッション初期化
        sdir = paths.session_dir(config, session_id)
        sdir.mkdir(parents=True)
        from codex_autogoal.state import SessionState, now_iso
        mgr = StateManager(sdir)
        state = SessionState(
            session_id=session_id,
            status=SessionStatus.RUNNING,
            cwd="/tmp",
            created_at=now_iso(),
        )
        mgr.write(state)

        # ターン1: continue
        result1 = _run_stop_hook(config, session_id,
            'ターン1完了\nAUTOGOAL_SIGNAL: {"state":"continue","reason":"テスト実行"}',
            capsys)
        assert result1.get("decision") == "block"

        state = mgr.read()
        assert state.continuation_count == 1

        # ターン2: continue
        result2 = _run_stop_hook(config, session_id,
            'ターン2完了\nAUTOGOAL_SIGNAL: {"state":"continue","reason":"修正適用"}',
            capsys)
        assert result2.get("decision") == "block"

        state = mgr.read()
        assert state.continuation_count == 2

        # ターン3: done
        result3 = _run_stop_hook(config, session_id,
            '全テスト通過\nAUTOGOAL_SIGNAL: {"state":"done","reason":"全テスト通過"}',
            capsys)

        state = mgr.read()
        assert state.status == SessionStatus.DONE

    def test_no_infinite_loop(self, config, capsys):
        """max_turnsを超えて継続しない"""
        config_limited = Config(home=config.home, enabled=True, max_turns=3)
        session_id = "test-max-turns"

        sdir = paths.session_dir(config_limited, session_id)
        sdir.mkdir(parents=True)
        from codex_autogoal.state import SessionState, now_iso
        mgr = StateManager(sdir)
        state = SessionState(
            session_id=session_id,
            status=SessionStatus.RUNNING,
            cwd="/tmp",
            created_at=now_iso(),
            continuation_count=2,
        )
        mgr.write(state)

        # 3ターン目でBLOCKED_LIMIT
        _run_stop_hook(config_limited, session_id,
            'AUTOGOAL_SIGNAL: {"state":"continue","reason":"次"}',
            capsys)

        state = mgr.read()
        assert state.status == SessionStatus.BLOCKED_LIMIT
