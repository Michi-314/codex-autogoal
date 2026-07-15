"""セッション状態管理（atomic write、状態遷移、ループ検出）"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any


class SessionStatus(str, Enum):
    """セッション状態"""
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    RESUMING = "RESUMING"
    DONE = "DONE"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    # 異常停止系
    BLOCKED_PROTOCOL_ERROR = "BLOCKED_PROTOCOL_ERROR"
    BLOCKED_LIMIT = "BLOCKED_LIMIT"
    BLOCKED_LOOP_DETECTED = "BLOCKED_LOOP_DETECTED"
    BLOCKED_RESUME_FAILED = "BLOCKED_RESUME_FAILED"
    BLOCKED_RESUME_AMBIGUOUS = "BLOCKED_RESUME_AMBIGUOUS"
    BLOCKED_CWD_MISSING = "BLOCKED_CWD_MISSING"
    BLOCKED_STATE_CORRUPT = "BLOCKED_STATE_CORRUPT"


# 終端状態（これらからの遷移は許可しない）
TERMINAL_STATUSES = frozenset({
    SessionStatus.DONE,
    SessionStatus.CANCELLED,
    SessionStatus.BLOCKED,
    SessionStatus.BLOCKED_PROTOCOL_ERROR,
    SessionStatus.BLOCKED_LIMIT,
    SessionStatus.BLOCKED_LOOP_DETECTED,
    SessionStatus.BLOCKED_RESUME_FAILED,
    SessionStatus.BLOCKED_RESUME_AMBIGUOUS,
    SessionStatus.BLOCKED_CWD_MISSING,
    SessionStatus.BLOCKED_STATE_CORRUPT,
})

# 有効な状態遷移マップ
_VALID_TRANSITIONS: dict[SessionStatus, frozenset[SessionStatus]] = {
    SessionStatus.STARTING: frozenset({SessionStatus.RUNNING, SessionStatus.FAILED}),
    SessionStatus.RUNNING: frozenset({
        SessionStatus.WAITING,
        SessionStatus.DONE,
        SessionStatus.BLOCKED,
        SessionStatus.CANCELLED,
        SessionStatus.FAILED,
        SessionStatus.BLOCKED_PROTOCOL_ERROR,
        SessionStatus.BLOCKED_LIMIT,
        SessionStatus.BLOCKED_LOOP_DETECTED,
    }),
    SessionStatus.WAITING: frozenset({
        SessionStatus.RESUMING,
        SessionStatus.CANCELLED,
        SessionStatus.BLOCKED_STATE_CORRUPT,
        # ジョブがすでに完了していた場合の即時続行
        SessionStatus.RUNNING,
    }),
    SessionStatus.RESUMING: frozenset({
        SessionStatus.RUNNING,
        SessionStatus.BLOCKED_RESUME_FAILED,
        SessionStatus.BLOCKED_RESUME_AMBIGUOUS,
        SessionStatus.CANCELLED,
    }),
    SessionStatus.FAILED: frozenset(),  # terminal扱い
}


@dataclass
class TokenUsage:
    """トークン使用量"""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0

    def add(self, other: dict[str, int]) -> None:
        self.input_tokens += other.get("input_tokens", 0)
        self.cached_input_tokens += other.get("cached_input_tokens", 0)
        self.output_tokens += other.get("output_tokens", 0)
        self.reasoning_output_tokens += other.get("reasoning_output_tokens", 0)


@dataclass
class SessionState:
    """セッション状態"""
    schema_version: int = 1
    session_id: str = ""
    cwd: str = ""
    status: SessionStatus = SessionStatus.STARTING
    created_at: str = ""
    updated_at: str = ""
    continuation_count: int = 0
    resume_count: int = 0
    resume_attempts: int = 0
    current_job_id: str | None = None
    last_turn_id: str = ""
    last_reason: str = ""
    last_message_hash: str = ""
    recent_message_hashes: list[str] = field(default_factory=list)
    recent_reasons: list[str] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    watcher_pid: int | None = None
    codex_pid: int | None = None
    sandbox_mode: str = "workspace-write"
    prompt_file: str | None = None
    resume_mode: str = "headless"
    terminal_pane_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        state = cls()
        state.schema_version = data.get("schema_version", 1)
        state.session_id = data.get("session_id", "")
        state.cwd = data.get("cwd", "")
        state.status = SessionStatus(data.get("status", "STARTING"))
        state.created_at = data.get("created_at", "")
        state.updated_at = data.get("updated_at", "")
        state.continuation_count = data.get("continuation_count", 0)
        state.resume_count = data.get("resume_count", 0)
        state.resume_attempts = data.get("resume_attempts", 0)
        state.current_job_id = data.get("current_job_id")
        state.last_turn_id = data.get("last_turn_id", "")
        state.last_reason = data.get("last_reason", "")
        state.last_message_hash = data.get("last_message_hash", "")
        state.recent_message_hashes = data.get("recent_message_hashes", [])
        state.recent_reasons = data.get("recent_reasons", [])
        usage_data = data.get("token_usage", {})
        state.token_usage = TokenUsage(
            input_tokens=usage_data.get("input_tokens", 0),
            cached_input_tokens=usage_data.get("cached_input_tokens", 0),
            output_tokens=usage_data.get("output_tokens", 0),
            reasoning_output_tokens=usage_data.get("reasoning_output_tokens", 0),
        )
        state.watcher_pid = data.get("watcher_pid")
        state.codex_pid = data.get("codex_pid")
        state.sandbox_mode = data.get("sandbox_mode", "workspace-write")
        state.prompt_file = data.get("prompt_file")
        state.resume_mode = data.get("resume_mode", "headless")
        state.terminal_pane_id = data.get("terminal_pane_id")
        return state


def now_iso() -> str:
    """現在時刻をISO 8601形式で返す。"""
    return datetime.now(timezone.utc).isoformat()


def validate_transition(current: SessionStatus, target: SessionStatus) -> bool:
    """状態遷移が有効か検証する。"""
    if current in TERMINAL_STATUSES:
        return False
    allowed = _VALID_TRANSITIONS.get(current)
    if allowed is None:
        return False
    return target in allowed


class StateManager:
    """セッション状態のatomic読み書きとイベント記録を管理する。"""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self._status_path = session_dir / "status.json"
        self._events_path = session_dir / "events.jsonl"

    def ensure_dir(self) -> None:
        """セッションディレクトリを作成する。"""
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def read(self) -> SessionState | None:
        """現在の状態を読み取る。ファイルが存在しなければNone。"""
        if not self._status_path.exists():
            return None
        try:
            data = json.loads(self._status_path.read_text(encoding="utf-8"))
            return SessionState.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def write(self, state: SessionState) -> None:
        """状態をatomicに書き込む。"""
        state.updated_at = now_iso()
        self._atomic_write_json(self._status_path, state.to_dict())

    def transition(
        self,
        state: SessionState,
        target: SessionStatus,
        *,
        reason: str = "",
        job_id: str | None = None,
    ) -> bool:
        """状態遷移を行う。無効な遷移ならFalseを返す。"""
        if not validate_transition(state.status, target):
            return False
        old_status = state.status
        state.status = target
        if reason:
            state.last_reason = reason
        if job_id is not None:
            state.current_job_id = job_id
        self.write(state)
        self.append_event({
            "type": "state_transition",
            "from": old_status.value,
            "to": target.value,
            "reason": reason,
            "timestamp": now_iso(),
        })
        return True

    def append_event(self, event: dict[str, Any]) -> None:
        """イベントをevents.jsonlに追記する。"""
        self.ensure_dir()
        with open(self._events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()

    def check_loop(self, message_hash: str, max_repeats: int = 3) -> bool:
        """直近のメッセージハッシュからループを検出する。

        Returns:
            ループ検出時True
        """
        state = self.read()
        if state is None:
            return False

        recent = state.recent_message_hashes
        if len(recent) >= max_repeats - 1:
            # 直近max_repeats-1個が全て同じハッシュなら、今回も同じならループ
            if all(h == message_hash for h in recent[-(max_repeats - 1):]):
                return True
        return False

    def check_reason_loop(self, reason: str, max_repeats: int = 5) -> bool:
        """同一reasonの連続をチェックする。

        Returns:
            ループ検出時True
        """
        state = self.read()
        if state is None:
            return False

        recent = state.recent_reasons
        if len(recent) >= max_repeats - 1:
            if all(r == reason for r in recent[-(max_repeats - 1):]):
                return True
        return False

    def _atomic_write_json(self, path: Path, data: dict[str, Any]) -> None:
        """JSONファイルをatomicに書き込む。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=".tmp_",
            suffix=".json",
        )
        fd_open = True
        try:
            content = json.dumps(data, ensure_ascii=False, indent=2)
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
            os.close(fd)
            fd_open = False
            os.replace(tmp_path, str(path))
        except Exception:
            if fd_open:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
