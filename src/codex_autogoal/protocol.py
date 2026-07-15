"""AutoGoalシグナルプロトコルのパースと検証"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class SignalState(str, Enum):
    """有効なAutoGoalシグナル状態"""
    CONTINUE = "continue"
    WAIT = "wait"
    DONE = "done"
    BLOCKED = "blocked"


class SignalError(str, Enum):
    """シグナルパースエラーの種類"""
    NO_SIGNAL = "no_signal"
    INVALID_JSON = "invalid_json"
    UNKNOWN_STATE = "unknown_state"
    MISSING_JOB_ID = "missing_job_id"
    INVALID_JOB_ID = "invalid_job_id"
    MISSING_REASON = "missing_reason"
    INPUT_TOO_LONG = "input_too_long"
    INVALID_TYPE = "invalid_type"


@dataclass(frozen=True)
class ParsedSignal:
    """パース済みシグナル"""
    state: SignalState
    reason: str
    job_id: str | None = None


@dataclass(frozen=True)
class SignalParseResult:
    """シグナルパース結果"""
    signal: ParsedSignal | None = None
    error: SignalError | None = None
    error_detail: str = ""

    @property
    def ok(self) -> bool:
        return self.signal is not None and self.error is None


# シグナルプレフィックス
_SIGNAL_PREFIX = "AUTOGOAL_SIGNAL:"

# ジョブID検証用正規表現
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# 入力最大長（十分大きめ）
_MAX_MESSAGE_LENGTH = 100_000

# シグナルJSON最大長
_MAX_SIGNAL_JSON_LENGTH = 2048


def extract_last_nonempty_line(text: str) -> str:
    """テキストの最後の非空行を取得する。"""
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def parse_signal(last_message: str) -> SignalParseResult:
    """最終メッセージからAutoGoalシグナルをパースする。

    最終メッセージの最後の非空行のみを検査する。

    Args:
        last_message: Codexの最終アシスタントメッセージ

    Returns:
        SignalParseResult（成功時は.signalにParsedSignal、失敗時は.errorにエラー種類）
    """
    if len(last_message) > _MAX_MESSAGE_LENGTH:
        return SignalParseResult(
            error=SignalError.INPUT_TOO_LONG,
            error_detail=f"メッセージが長すぎます ({len(last_message)} chars)"
        )

    last_line = extract_last_nonempty_line(last_message)

    if not last_line.startswith(_SIGNAL_PREFIX):
        return SignalParseResult(
            error=SignalError.NO_SIGNAL,
            error_detail="最終行にAutoGoalシグナルが見つかりません"
        )

    json_str = last_line[len(_SIGNAL_PREFIX):].strip()

    if len(json_str) > _MAX_SIGNAL_JSON_LENGTH:
        return SignalParseResult(
            error=SignalError.INPUT_TOO_LONG,
            error_detail=f"シグナルJSONが長すぎます ({len(json_str)} chars)"
        )

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        return SignalParseResult(
            error=SignalError.INVALID_JSON,
            error_detail=f"JSONパースエラー: {e}"
        )

    if not isinstance(data, dict):
        return SignalParseResult(
            error=SignalError.INVALID_TYPE,
            error_detail="シグナルはオブジェクトである必要があります"
        )

    return _validate_signal_data(data)


def _validate_signal_data(data: dict[str, Any]) -> SignalParseResult:
    """パース済みJSONデータを検証する。"""

    # state検証
    raw_state = data.get("state")
    if not isinstance(raw_state, str):
        return SignalParseResult(
            error=SignalError.INVALID_TYPE,
            error_detail=f"stateが文字列ではありません: {type(raw_state).__name__}"
        )

    try:
        state = SignalState(raw_state)
    except ValueError:
        return SignalParseResult(
            error=SignalError.UNKNOWN_STATE,
            error_detail=f"未知のstate: {raw_state!r}"
        )

    # reason検証
    reason = data.get("reason", "")
    if not isinstance(reason, str):
        return SignalParseResult(
            error=SignalError.INVALID_TYPE,
            error_detail=f"reasonが文字列ではありません: {type(reason).__name__}"
        )

    # wait時のjob_id検証
    job_id: str | None = None
    if state == SignalState.WAIT:
        raw_job_id = data.get("job_id")
        if raw_job_id is None or raw_job_id == "":
            return SignalParseResult(
                error=SignalError.MISSING_JOB_ID,
                error_detail="waitシグナルにjob_idがありません"
            )
        if not isinstance(raw_job_id, str):
            return SignalParseResult(
                error=SignalError.INVALID_TYPE,
                error_detail=f"job_idが文字列ではありません: {type(raw_job_id).__name__}"
            )
        if not _JOB_ID_RE.match(raw_job_id):
            return SignalParseResult(
                error=SignalError.INVALID_JOB_ID,
                error_detail=f"不正なjob_id形式: {raw_job_id!r}"
            )
        job_id = raw_job_id

    return SignalParseResult(
        signal=ParsedSignal(state=state, reason=reason, job_id=job_id)
    )


def validate_job_id(job_id: str) -> bool:
    """ジョブIDが有効な形式か検証する。"""
    return bool(_JOB_ID_RE.match(job_id))


def normalize_message_hash(message: str) -> str:
    """メッセージを正規化してハッシュ化する（ループ検出用）。

    空白を正規化してSHA-256ハッシュを返す。
    """
    # 空白正規化: 連続空白を単一スペースに、前後をstrip
    normalized = re.sub(r"\s+", " ", message.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
