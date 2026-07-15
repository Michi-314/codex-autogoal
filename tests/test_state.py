"""state.py の単体テスト"""

import json
import os
import tempfile
from pathlib import Path

import pytest

from codex_autogoal.state import (
    SessionState,
    SessionStatus,
    StateManager,
    TERMINAL_STATUSES,
    validate_transition,
    now_iso,
)


@pytest.fixture
def tmp_session_dir(tmp_path):
    sdir = tmp_path / "session-test-001"
    sdir.mkdir()
    return sdir


@pytest.fixture
def state_manager(tmp_session_dir):
    return StateManager(tmp_session_dir)


class TestSessionState:
    def test_round_trip(self):
        state = SessionState(
            session_id="test-123",
            cwd="/tmp/test",
            status=SessionStatus.RUNNING,
            created_at="2026-07-10T00:00:00Z",
            continuation_count=5,
        )
        d = state.to_dict()
        restored = SessionState.from_dict(d)
        assert restored.session_id == "test-123"
        assert restored.status == SessionStatus.RUNNING
        assert restored.continuation_count == 5

    def test_from_dict_defaults(self):
        state = SessionState.from_dict({})
        assert state.session_id == ""
        assert state.status == SessionStatus.STARTING
        assert state.continuation_count == 0


class TestValidateTransition:
    def test_starting_to_running(self):
        assert validate_transition(SessionStatus.STARTING, SessionStatus.RUNNING)

    def test_running_to_waiting(self):
        assert validate_transition(SessionStatus.RUNNING, SessionStatus.WAITING)

    def test_running_to_done(self):
        assert validate_transition(SessionStatus.RUNNING, SessionStatus.DONE)

    def test_waiting_to_resuming(self):
        assert validate_transition(SessionStatus.WAITING, SessionStatus.RESUMING)

    def test_resuming_to_running(self):
        assert validate_transition(SessionStatus.RESUMING, SessionStatus.RUNNING)

    def test_terminal_state_reject(self):
        for status in TERMINAL_STATUSES:
            assert not validate_transition(status, SessionStatus.RUNNING), \
                f"{status}からRUNNINGへの遷移は拒否されるべき"

    def test_invalid_transition(self):
        # STARTINGからWAITINGは不正
        assert not validate_transition(SessionStatus.STARTING, SessionStatus.WAITING)


class TestStateManager:
    def test_write_and_read(self, state_manager):
        state = SessionState(
            session_id="test-001",
            status=SessionStatus.RUNNING,
            cwd="/tmp",
        )
        state_manager.write(state)
        read = state_manager.read()
        assert read is not None
        assert read.session_id == "test-001"
        assert read.status == SessionStatus.RUNNING

    def test_read_nonexistent(self, state_manager):
        assert state_manager.read() is None

    def test_atomic_write(self, state_manager):
        """書き込み中に中断しても既存状態を壊さない"""
        state1 = SessionState(session_id="v1", status=SessionStatus.RUNNING)
        state_manager.write(state1)

        state2 = SessionState(session_id="v2", status=SessionStatus.WAITING)
        state_manager.write(state2)

        read = state_manager.read()
        assert read.session_id == "v2"

    def test_transition(self, state_manager):
        state = SessionState(
            session_id="test-001",
            status=SessionStatus.RUNNING,
        )
        state_manager.ensure_dir()
        state_manager.write(state)

        success = state_manager.transition(state, SessionStatus.WAITING,
                                            reason="ジョブ待ち")
        assert success
        assert state.status == SessionStatus.WAITING

        read = state_manager.read()
        assert read.status == SessionStatus.WAITING
        assert read.last_reason == "ジョブ待ち"

    def test_invalid_transition(self, state_manager):
        state = SessionState(
            session_id="test-001",
            status=SessionStatus.DONE,
        )
        state_manager.ensure_dir()
        state_manager.write(state)

        success = state_manager.transition(state, SessionStatus.RUNNING,
                                            reason="不正遷移")
        assert not success

    def test_append_event(self, state_manager):
        state_manager.ensure_dir()
        state_manager.append_event({"type": "test", "value": 1})
        state_manager.append_event({"type": "test", "value": 2})

        events_path = state_manager.session_dir / "events.jsonl"
        lines = events_path.read_text().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["value"] == 1
        assert json.loads(lines[1])["value"] == 2

    def test_corrupted_state_file(self, state_manager):
        """壊れたJSONファイルでもNoneを返す"""
        status_path = state_manager.session_dir / "status.json"
        status_path.write_text("{broken json")
        assert state_manager.read() is None


class TestLoopDetection:
    def test_no_loop(self, state_manager):
        state = SessionState(
            session_id="test-001",
            status=SessionStatus.RUNNING,
            recent_message_hashes=["aaa", "bbb"],
        )
        state_manager.ensure_dir()
        state_manager.write(state)
        assert not state_manager.check_loop("ccc")

    def test_loop_detected(self, state_manager):
        state = SessionState(
            session_id="test-001",
            status=SessionStatus.RUNNING,
            recent_message_hashes=["aaa", "aaa"],
        )
        state_manager.ensure_dir()
        state_manager.write(state)
        assert state_manager.check_loop("aaa")

    def test_reason_loop(self, state_manager):
        state = SessionState(
            session_id="test-001",
            status=SessionStatus.RUNNING,
            recent_reasons=["same", "same", "same", "same"],
        )
        state_manager.ensure_dir()
        state_manager.write(state)
        assert state_manager.check_reason_loop("same")

    def test_no_reason_loop(self, state_manager):
        state = SessionState(
            session_id="test-001",
            status=SessionStatus.RUNNING,
            recent_reasons=["a", "b", "c", "d"],
        )
        state_manager.ensure_dir()
        state_manager.write(state)
        assert not state_manager.check_reason_loop("e")
