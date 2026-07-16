#!/usr/bin/env python3
"""AutoGoal Stop Hook

stdinからCodexのStop Hook JSONを読み取り、
AutoGoalシグナルに基づいてセッション状態を管理する。

標準出力にはHook応答JSONのみを出力する。
デバッグ情報はログファイルに書き込む。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import re
from datetime import datetime, timezone
from pathlib import Path

from codex_autogoal.config import Config, load_config
from codex_autogoal import paths
from codex_autogoal.protocol import (
    ParsedSignal,
    SignalError,
    SignalState,
    normalize_message_hash,
    parse_signal,
)
from codex_autogoal.state import (
    SessionState,
    SessionStatus,
    StateManager,
    now_iso,
)
from codex_autogoal.locking import FileLock
from codex_autogoal.watcher import launch_watcher


def main() -> None:
    """Stop Hookメインエントリポイント"""
    paths.secure_umask()
    try:
        _run_hook()
    except Exception as e:
        # Hook例外時はCodexの通常動作を妨げない
        _emit_passthrough()
        _log_error(f"Stop Hook例外: {e}")


def _run_hook() -> None:
    """Hook処理本体"""
    config = load_config()
    paths.harden_runtime_permissions(config)

    # 1. CODEX_AUTOGOAL_ENABLED確認
    if not config.enabled:
        _emit_passthrough()
        return

    # 2. stdin読み取り
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as e:
        _log_error(f"Hook入力パースエラー: {e}")
        _emit_passthrough()
        return

    # 3. 入力検証
    session_id = hook_input.get("session_id", "")
    last_message = hook_input.get("last_assistant_message", "")
    cwd = hook_input.get("cwd", "")
    stop_hook_active = hook_input.get("stop_hook_active", False)

    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        _log_error("session_idが不正です")
        _emit_passthrough()
        return

    # セットアップ
    sdir = paths.session_dir(config, session_id)
    mgr = StateManager(sdir)
    logger = _setup_logger(config, session_id)

    # stop_hook_active再入検出
    if stop_hook_active:
        logger.warning("stop_hook_activeがtrueです。再入の可能性あり。安全停止します。")
        state = mgr.read()
        if state:
            mgr.transition(state, SessionStatus.BLOCKED_PROTOCOL_ERROR,
                           reason="Stop Hook再入検出")
        _emit_stop()
        return

    # 4. 状態読み取り（なければ作成）
    state = mgr.read()
    if state is None:
        state = SessionState(
            session_id=session_id,
            cwd=cwd,
            status=SessionStatus.RUNNING,
            created_at=now_iso(),
        )
        mgr.ensure_dir()
        mgr.write(state)

    # キャンセル確認
    if paths.is_private_regular_file(paths.cancelled_marker(config, session_id)):
        logger.info("セッションがキャンセルされています")
        mgr.transition(state, SessionStatus.CANCELLED, reason="ユーザーキャンセル")
        _emit_stop()
        return

    # 5. last_messageを保存
    try:
        paths.write_private_text(paths.last_message_txt(config, session_id), last_message)
    except OSError:
        pass

    # 6. シグナルパース
    result = parse_signal(last_message)

    if not result.ok:
        logger.warning(f"シグナルエラー: {result.error} - {result.error_detail}")
        mgr.transition(state, SessionStatus.BLOCKED_PROTOCOL_ERROR,
                       reason=f"{result.error}: {result.error_detail}")
        mgr.append_event({
            "type": "signal_error",
            "error": result.error.value if result.error else "unknown",
            "detail": result.error_detail,
            "timestamp": now_iso(),
        })
        _emit_stop()
        return

    signal = result.signal
    assert signal is not None

    logger.info(f"シグナル受信: state={signal.state.value}, reason={signal.reason}")

    # 7. state別処理
    if signal.state == SignalState.CONTINUE:
        _handle_continue(config, mgr, state, signal, logger)
    elif signal.state == SignalState.WAIT:
        _handle_wait(config, mgr, state, signal, logger)
    elif signal.state == SignalState.DONE:
        _handle_done(config, mgr, state, signal, logger)
    elif signal.state == SignalState.BLOCKED:
        _handle_blocked(config, mgr, state, signal, logger)


def _handle_continue(
    config: Config,
    mgr: StateManager,
    state: SessionState,
    signal: ParsedSignal,
    logger: logging.Logger,
) -> None:
    """continue処理: 新しいターンを生成する"""

    # ループ検出
    msg_hash = normalize_message_hash(
        paths.read_private_text(paths.last_message_txt(config, state.session_id))
        if paths.last_message_txt(config, state.session_id).exists()
        else ""
    )

    if mgr.check_loop(msg_hash):
        logger.warning("ループ検出: 同一メッセージが3回連続")
        mgr.transition(state, SessionStatus.BLOCKED_LOOP_DETECTED,
                       reason="同一メッセージが3回連続しました")
        _emit_stop()
        return

    # reason反復チェック
    if mgr.check_reason_loop(signal.reason):
        logger.warning(f"同一reasonが連続: {signal.reason}")
        mgr.transition(state, SessionStatus.BLOCKED_LOOP_DETECTED,
                       reason=f"同一reasonが連続しています: {signal.reason}")
        _emit_stop()
        return

    # ターン数チェック
    state.continuation_count += 1
    if state.continuation_count >= config.max_turns:
        logger.warning(f"最大ターン数到達: {config.max_turns}")
        mgr.transition(state, SessionStatus.BLOCKED_LIMIT,
                       reason=f"最大ターン数({config.max_turns})に到達しました")
        _emit_stop()
        return

    # メッセージハッシュ記録
    state.last_message_hash = msg_hash
    state.recent_message_hashes.append(msg_hash)
    # 直近10件のみ保持
    state.recent_message_hashes = state.recent_message_hashes[-10:]
    state.recent_reasons.append(signal.reason)
    state.recent_reasons = state.recent_reasons[-10:]
    state.last_reason = signal.reason

    mgr.write(state)
    mgr.append_event({
        "type": "continue",
        "continuation_count": state.continuation_count,
        "reason": signal.reason,
        "timestamp": now_iso(),
    })

    logger.info(f"継続ターン #{state.continuation_count}")

    # block応答で次のターンを生成
    _emit_json({
        "decision": "block",
        "reason": (
            "AutoGoal継続: 直前の結果を確認し、目的達成に必要な次の作業を実行してください。"
            "待機が必要ならautogoal-jobを使用してください。"
        ),
    })


def _handle_wait(
    config: Config,
    mgr: StateManager,
    state: SessionState,
    signal: ParsedSignal,
    logger: logging.Logger,
) -> None:
    """wait処理: watcherを起動しCodexを停止する"""
    assert signal.job_id is not None

    # 同一Stopイベントの重複配送ではwatcherを増やさない。
    if state.status == SessionStatus.WAITING:
        if state.current_job_id == signal.job_id:
            logger.info(f"既にWAITINGです。重複waitを無視: job={signal.job_id}")
            _emit_stop()
            return
        logger.error("WAITING中に異なるjob_idのwaitを受信しました")
        _emit_stop()
        return
    if state.status != SessionStatus.RUNNING:
        logger.warning(f"状態が{state.status.value}のためwaitを無視します")
        _emit_stop()
        return

    # ジョブ存在確認
    safe_dir = paths.resolve_job_dir_safe(config, signal.job_id)
    if safe_dir is None or not safe_dir.exists():
        logger.error(f"ジョブが存在しません: {signal.job_id}")
        mgr.transition(state, SessionStatus.BLOCKED_PROTOCOL_ERROR,
                       reason=f"ジョブが存在しません: {signal.job_id}")
        _emit_stop()
        return

    # すでにdoneなら即時続行可能
    if paths.is_private_regular_file(paths.job_done_marker(config, signal.job_id)):
        logger.info(f"ジョブ {signal.job_id} はすでに完了しています。即時続行します。")
        # ジョブ結果を読んでcontinue
        _resume_with_job_result(config, state, signal.job_id, logger)
        return

    # WAITING状態に遷移
    mgr.transition(state, SessionStatus.WAITING,
                   reason=signal.reason, job_id=signal.job_id)

    # CWDを保存
    state.cwd = state.cwd or os.environ.get("PWD", os.getcwd())
    mgr.write(state)

    # watcherをdetachで起動
    _launch_watcher(config, state.session_id, signal.job_id, logger)

    logger.info(f"WAITING状態に遷移。watcher起動済み。job={signal.job_id}")

    # Codexを停止（次のターンを作らない）
    _emit_stop()


def _handle_done(
    config: Config,
    mgr: StateManager,
    state: SessionState,
    signal: ParsedSignal,
    logger: logging.Logger,
) -> None:
    """done処理: セッション完了"""
    mgr.transition(state, SessionStatus.DONE, reason=signal.reason)
    logger.info(f"セッション完了: {signal.reason}")
    _emit_stop()


def _handle_blocked(
    config: Config,
    mgr: StateManager,
    state: SessionState,
    signal: ParsedSignal,
    logger: logging.Logger,
) -> None:
    """blocked処理: ユーザー入力待ち"""
    mgr.transition(state, SessionStatus.BLOCKED, reason=signal.reason)
    logger.info(f"セッションブロック: {signal.reason}")
    _emit_stop()


def _launch_watcher(
    config: Config,
    session_id: str,
    job_id: str,
    logger: logging.Logger,
) -> None:
    """後方互換用wrapper。"""
    launch_watcher(config, session_id, job_id, logger)


def _resume_with_job_result(
    config: Config,
    state: SessionState,
    job_id: str,
    logger: logging.Logger,
) -> None:
    """ジョブが既完了の場合、結果を読んでcontinueする"""
    status = None
    try:
        status_data = json.loads(
            paths.read_private_text(paths.job_status_json(config, job_id))
        )
        status = status_data.get("status", "UNKNOWN")
        exit_code = status_data.get("exit_code", -1)
    except (json.JSONDecodeError, FileNotFoundError):
        status = "UNKNOWN"
        exit_code = -1

    reason = (
        f"ジョブ {job_id} は既に完了しています "
        f"(status={status}, exit_code={exit_code})。"
        "結果を確認して作業を続けてください。"
    )

    _emit_json({
        "decision": "block",
        "reason": reason,
    })


def _emit_json(data: dict) -> None:
    """Hook応答をstdoutに出力する。"""
    print(json.dumps(data, ensure_ascii=False), flush=True)


def _emit_passthrough() -> None:
    """通常のCodex動作を妨げない応答を出力する。"""
    _emit_json({"continue": True})


def _emit_stop() -> None:
    """Codexを停止する応答を出力する（次のターンを作らない）。"""
    _emit_json({"continue": True})


def _setup_logger(config: Config, session_id: str) -> logging.Logger:
    """セッション別のファイルロガーをセットアップする。"""
    logger = logging.getLogger(f"autogoal.stop_hook.{session_id}")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        log_path = paths.session_dir(config, session_id) / "hook.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.StreamHandler(paths.open_private_append(log_path))
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        )
        logger.addHandler(handler)

    return logger


def _log_error(msg: str) -> None:
    """エラーをstderrに出力（Hookの標準出力は汚さない）。"""
    print(f"[autogoal-stop-hook] {msg}", file=sys.stderr)


if __name__ == "__main__":
    main()
