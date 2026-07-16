"""Watcher: ジョブ完了を待機し、Codexセッションを自動再開する"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from codex_autogoal.config import Config
from codex_autogoal import paths
from codex_autogoal.locking import FileLock
from codex_autogoal.state import (
    SessionState,
    SessionStatus,
    StateManager,
    now_iso,
)
from codex_autogoal.resume import resume_session
from codex_autogoal.process import (
    get_python_executable,
    process_fingerprint,
    sanitized_environment,
)


# ポーリング間隔（秒）
POLL_INTERVAL = 5


def launch_watcher(
    config: Config,
    session_id: str,
    job_id: str,
    logger: logging.Logger,
) -> int:
    """watcherをdetach起動してPIDを状態へ保存する。"""
    watcher_cmd = [
        get_python_executable(),
        "-m", "codex_autogoal.watcher",
        "--session-id", session_id,
        "--job-id", job_id,
        "--home", str(config.home),
    ]
    log_path = paths.watcher_log(config, session_id)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = sanitized_environment()
    env.update({
        "CODEX_AUTOGOAL_ENABLED": "1",
        "CODEX_AUTOGOAL_HOME": str(config.home),
    })
    with paths.open_private_append(log_path) as log_f:
        proc = subprocess.Popen(
            watcher_cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_f,
            stderr=log_f,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    mgr = StateManager(paths.session_dir(config, session_id))
    state = mgr.read()
    if state:
        state.watcher_pid = proc.pid
        state.watcher_fingerprint = process_fingerprint(proc.pid)
        mgr.write(state)
    logger.info(f"watcher起動: PID={proc.pid}")
    return proc.pid


def main() -> None:
    """Watcherメインエントリポイント（detachedプロセスとして実行される）"""
    paths.secure_umask()
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--home", required=True)
    args = parser.parse_args()

    config = Config(home=Path(args.home))
    paths.harden_runtime_permissions(config)
    session_id = paths.validate_identifier(args.session_id, kind="session ID")
    job_id = paths.validate_identifier(args.job_id, kind="job ID")

    sdir = paths.session_dir(config, session_id)
    logger = _setup_logger(sdir)

    logger.info(f"watcher開始: session={session_id}, job={job_id}")

    # 二重起動防止
    lock = FileLock(paths.watcher_lock(config, session_id))
    if not lock.acquire(blocking=False):
        logger.info("別のwatcherが実行中です。終了します。")
        return

    try:
        _watch_loop(config, session_id, job_id, logger)
    except Exception as e:
        logger.error(f"watcher例外: {e}", exc_info=True)
    finally:
        lock.release()
        logger.info("watcher終了")


def _watch_loop(
    config: Config,
    session_id: str,
    job_id: str,
    logger: logging.Logger,
) -> None:
    """ジョブ完了までポーリングし、完了後にresumeする"""
    done_path = paths.job_done_marker(config, job_id)
    cancelled_path = paths.cancelled_marker(config, session_id)

    while True:
        # キャンセル確認
        if paths.is_private_regular_file(cancelled_path):
            logger.info("セッションがキャンセルされました。watcherを終了します。")
            return

        # 状態確認
        mgr = StateManager(paths.session_dir(config, session_id))
        state = mgr.read()
        if state is None:
            logger.error("セッション状態が読み取れません")
            return

        # 終端状態なら終了
        if state.status in (
            SessionStatus.DONE,
            SessionStatus.CANCELLED,
            SessionStatus.BLOCKED,
        ) or state.status.value.startswith("BLOCKED_"):
            logger.info(f"セッションは既に{state.status.value}です。watcherを終了します。")
            return

        # ジョブ完了確認
        if paths.is_private_regular_file(done_path):
            logger.info(f"ジョブ {job_id} が完了しました。resumeを開始します。")
            _handle_job_done(config, session_id, job_id, logger)
            return

        time.sleep(POLL_INTERVAL)


def _handle_job_done(
    config: Config,
    session_id: str,
    job_id: str,
    logger: logging.Logger,
) -> None:
    """ジョブ完了後のresume処理"""
    sdir = paths.session_dir(config, session_id)
    mgr = StateManager(sdir)

    # 状態を再確認
    state = mgr.read()
    if state is None:
        logger.error("セッション状態が読み取れません")
        return

    if state.status != SessionStatus.WAITING:
        logger.warning(f"状態が{state.status.value}です（WAITINGではない）。resumeをスキップします。")
        return

    # RESUMING に遷移
    if not mgr.transition(state, SessionStatus.RESUMING, reason="ジョブ完了、resume開始"):
        logger.error("RESUMING への遷移に失敗しました")
        return

    # ジョブ結果を取得
    job_status = _read_job_status(config, job_id)
    resume_message = _build_resume_message(config, job_id, job_status)

    logger.info(f"resume メッセージ:\n{resume_message}")

    # CWD確認
    cwd = state.cwd
    if cwd and not Path(cwd).exists():
        logger.error(f"CWDが存在しません: {cwd}")
        mgr.transition(state, SessionStatus.BLOCKED_CWD_MISSING,
                       reason=f"CWDが存在しません: {cwd}")
        return

    if state.resume_mode != "headless":
        logger.error("legacy visible resume state rejected; terminal injection is disabled")
        mgr.transition(
            state,
            SessionStatus.BLOCKED_RESUME_FAILED,
            reason="visible resume is disabled for security",
        )
        return
    success = resume_session(
        config=config,
        session_id=session_id,
        resume_message=resume_message,
        cwd=cwd,
        state_manager=mgr,
        logger=logger,
    )

    if success:
        logger.info("resume成功")
    else:
        logger.error("resume失敗")


def _read_job_status(config: Config, job_id: str) -> dict:
    """ジョブの状態を読み取る"""
    try:
        status_path = paths.job_status_json(config, job_id)
        data = json.loads(paths.read_private_text(status_path))
        if not isinstance(data, dict):
            raise ValueError("job status must be an object")
        status = data.get("status")
        exit_code = data.get("exit_code", -1)
        if status not in {"SUCCEEDED", "FAILED"}:
            raise ValueError("invalid job status")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise ValueError("invalid exit code")
        return {"status": status, "exit_code": exit_code}
    except (OSError, json.JSONDecodeError, ValueError):
        return {"status": "UNKNOWN", "exit_code": -1}


def _build_resume_message(config: Config, job_id: str, job_status: dict) -> str:
    """Build a bounded resume notice without exposing control-home paths or logs."""
    status = job_status.get("status", "UNKNOWN")
    exit_code = job_status.get("exit_code", -1)

    return (
        "AutoGoalで待機していたバックグラウンドジョブが完了しました。\n"
        "\n"
        f"job_id: {job_id}\n"
        f"status: {status}\n"
        f"exit_code: {exit_code}\n"
        "\n"
        "control homeの生ログはモデルへ渡されません。必要ならユーザーへ、信頼済み端末で"
        "autogoal-job logsを確認するよう依頼してください。\n"
        "長時間処理が再度必要ならblockedを返し、信頼済み端末からの起動を依頼してください。"
    )


def _setup_logger(session_dir: Path) -> logging.Logger:
    """watcher用ロガー"""
    logger = logging.getLogger("autogoal.watcher")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        session_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.StreamHandler(
            paths.open_private_append(session_dir / "watcher.log")
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s [watcher] %(message)s")
        )
        logger.addHandler(handler)

    return logger


if __name__ == "__main__":
    main()
