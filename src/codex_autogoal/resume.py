"""セッション再開（codex exec resume）"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from codex_autogoal.config import Config
from codex_autogoal import paths
from codex_autogoal.state import (
    SessionState,
    SessionStatus,
    StateManager,
    now_iso,
)

# リトライ間隔（秒）: 指数バックオフ
_RETRY_DELAYS = [60, 300, 900]  # 1分, 5分, 15分


def resume_session(
    *,
    config: Config,
    session_id: str,
    resume_message: str,
    cwd: str | None = None,
    state_manager: StateManager,
    logger: logging.Logger,
) -> bool:
    """Codexセッションを再開する。

    指数バックオフでリトライし、ターン開始後の失敗はambiguousとして扱う。

    Returns:
        resume成功ならTrue
    """
    max_attempts = config.max_resume_attempts

    for attempt in range(max_attempts):
        state = state_manager.read()
        if state is None:
            logger.error("セッション状態が読み取れません")
            return False

        # キャンセル確認
        if paths.is_private_regular_file(paths.cancelled_marker(config, session_id)):
            logger.info("セッションがキャンセルされました")
            state_manager.transition(state, SessionStatus.CANCELLED,
                                     reason="resume中にキャンセル")
            return False

        # 状態確認
        if state.status not in (SessionStatus.RESUMING, SessionStatus.WAITING):
            logger.warning(f"状態が{state.status.value}のため、resumeをスキップします")
            return False

        logger.info(f"resume試行 #{attempt + 1}/{max_attempts}")

        # resume_attempts記録
        state.resume_attempts = attempt + 1
        state_manager.write(state)

        success, turn_started = _execute_resume(
            config=config,
            session_id=session_id,
            resume_message=resume_message,
            cwd=cwd,
            state_manager=state_manager,
            logger=logger,
        )

        if success:
            # resume成功 → RUNNINGに遷移
            state = state_manager.read()
            if state:
                state.resume_count += 1
                state_manager.transition(state, SessionStatus.RUNNING,
                                         reason="resume成功")
            return True

        if turn_started:
            # ターン開始後に失敗 → ambiguous
            logger.error("ターン開始後に失敗しました。重複作業防止のため停止します。")
            state = state_manager.read()
            if state:
                state_manager.transition(
                    state, SessionStatus.BLOCKED_RESUME_AMBIGUOUS,
                    reason="ターン開始後のresume失敗"
                )
            return False

        # リトライ待機
        if attempt < max_attempts - 1:
            delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
            logger.info(f"resume失敗。{delay}秒後にリトライします。")

            # 待機中もキャンセル確認
            for _ in range(delay):
                if paths.is_private_regular_file(paths.cancelled_marker(config, session_id)):
                    logger.info("待機中にキャンセルされました")
                    state = state_manager.read()
                    if state:
                        state_manager.transition(state, SessionStatus.CANCELLED,
                                                 reason="resume待機中にキャンセル")
                    return False
                time.sleep(1)

    # 最大試行回数超過
    logger.error(f"resume失敗: 最大試行回数({max_attempts})に到達")
    state = state_manager.read()
    if state:
        state_manager.transition(
            state, SessionStatus.BLOCKED_RESUME_FAILED,
            reason=f"resume失敗: {max_attempts}回試行済み"
        )
    return False


def _execute_resume(
    *,
    config: Config,
    session_id: str,
    resume_message: str,
    cwd: str | None,
    state_manager: StateManager,
    logger: logging.Logger,
) -> tuple[bool, bool]:
    """codex exec resume を実行する。

    Returns:
        (success, turn_started) のタプル
    """
    cmd = [
        config.codex_bin,
        "exec", "resume",
        session_id,
        resume_message,
        "--json",
    ]
    if config.bypass_hook_trust:
        cmd.insert(3, "--dangerously-bypass-hook-trust")

    codex_log_path = paths.codex_jsonl(config, session_id)
    resume_log_path = paths.resume_log(config, session_id)

    env = os.environ.copy()
    env["CODEX_AUTOGOAL_ENABLED"] = "1"
    env["CODEX_AUTOGOAL_HOME"] = str(config.home)

    turn_started = False

    try:
        logger.info(f"実行: {' '.join(cmd[:5])}...")

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        # JSONLストリーム処理
        with open(codex_log_path, "a") as codex_log, \
             open(resume_log_path, "a") as rlog:

            rlog.write(f"\n--- resume開始: {now_iso()} ---\n")

            if proc.stdout:
                for line_bytes in proc.stdout:
                    line = line_bytes.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue

                    # 生データ保存
                    codex_log.write(line + "\n")
                    codex_log.flush()

                    # JSONパース
                    try:
                        event = json.loads(line)
                        event_type = event.get("type", "")

                        if event_type == "turn.started":
                            turn_started = True
                            rlog.write(f"turn.started 検出\n")

                        # token usage記録
                        if event_type == "turn.completed":
                            usage = event.get("usage", {})
                            if usage:
                                state = state_manager.read()
                                if state:
                                    state.token_usage.add(usage)
                                    state_manager.write(state)

                    except json.JSONDecodeError:
                        pass

            exit_code = proc.wait()
            rlog.write(f"exit_code: {exit_code}\n")

            # stderr
            if proc.stderr:
                stderr_content = proc.stderr.read().decode("utf-8", errors="replace")
                if stderr_content:
                    rlog.write(f"stderr:\n{stderr_content}\n")

        return exit_code == 0, turn_started

    except FileNotFoundError:
        logger.error(f"Codex CLIが見つかりません: {config.codex_bin}")
        return False, False
    except Exception as e:
        logger.error(f"resume実行エラー: {e}")
        return False, turn_started
