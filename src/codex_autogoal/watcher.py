"""Watcher: ジョブ完了を待機し、Codexセッションを自動再開する"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
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
from codex_autogoal.process import get_python_executable, process_fingerprint


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
    env = os.environ.copy()
    env.update({
        "CODEX_AUTOGOAL_ENABLED": "1",
        "CODEX_AUTOGOAL_HOME": str(config.home),
    })
    with open(log_path, "a", encoding="utf-8") as log_f:
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
        if cancelled_path.exists():
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
        if done_path.exists():
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

    if state.resume_mode == "wezterm":
        visible_message = _build_visible_resume_message(job_id, job_status)
        success = _resume_visible(state, visible_message, mgr, logger)
    else:
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


def _resume_visible(
    state: SessionState,
    resume_message: str,
    mgr: StateManager,
    logger: logging.Logger,
) -> bool:
    """稼働中のWezTerm paneへメッセージとEnterを別送信する。"""
    pane_id = state.terminal_pane_id
    wezterm = shutil.which("wezterm")
    if not pane_id or not wezterm:
        logger.error("visible resumeに必要なWEZTERM_PANEまたはweztermがありません")
        mgr.transition(
            state,
            SessionStatus.BLOCKED_RESUME_FAILED,
            reason="visible resume target missing",
        )
        return False

    if not _wezterm_pane_runs_codex(wezterm, pane_id):
        logger.error("visible resume先がCodex foreground paneではありません")
        mgr.transition(
            state,
            SessionStatus.BLOCKED_RESUME_FAILED,
            reason="visible resume target is not Codex",
        )
        return False

    base = [wezterm, "cli", "send-text", "--pane-id", pane_id, "--no-paste"]
    try:
        message_result = subprocess.run(
            base,
            input=resume_message,
            text=True,
            capture_output=True,
            timeout=10,
        )
        if message_result.returncode != 0:
            raise RuntimeError(message_result.stderr.strip() or "message send failed")
        for attempt in range(2):
            if not _wezterm_pane_runs_codex(wezterm, pane_id):
                raise RuntimeError("visible resume target changed before Enter")
            enter_result = subprocess.run(
                base,
                input="\r",
                text=True,
                capture_output=True,
                timeout=10,
            )
            if enter_result.returncode != 0:
                raise RuntimeError(enter_result.stderr.strip() or "enter send failed")
            if attempt == 0:
                time.sleep(0.35)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        logger.error(f"visible resume送信失敗: {exc}")
        current = mgr.read()
        if current:
            mgr.transition(
                current,
                SessionStatus.BLOCKED_RESUME_FAILED,
                reason=f"visible resume送信失敗: {exc}",
            )
        return False

    current = mgr.read()
    if current:
        current.resume_count += 1
        mgr.transition(current, SessionStatus.RUNNING, reason="visible resume送信成功")
        mgr.append_event({
            "type": "visible_resume_sent",
            "pane_id": pane_id,
            "timestamp": now_iso(),
        })
    logger.info(f"visible resume送信成功: pane={pane_id}")
    return True


def _wezterm_pane_runs_codex(wezterm: str, pane_id: str) -> bool:
    """Fail closed unless WezTerm reports Codex as the pane foreground process."""
    try:
        result = subprocess.run(
            [wezterm, "cli", "list", "--format", "json"],
            text=True,
            capture_output=True,
            timeout=5,
        )
        if result.returncode != 0:
            return False
        panes = json.loads(result.stdout)
        for pane in panes:
            if str(pane.get("pane_id")) != str(pane_id):
                continue
            process = Path(str(pane.get("foreground_process_name", ""))).name.lower()
            return process == "codex" or process.startswith("codex-")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, TypeError):
        return False
    return False


def _read_job_status(config: Config, job_id: str) -> dict:
    """ジョブの状態を読み取る"""
    try:
        status_path = paths.job_status_json(config, job_id)
        return json.loads(status_path.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return {"status": "UNKNOWN", "exit_code": -1}


def _build_resume_message(config: Config, job_id: str, job_status: dict) -> str:
    """resume時のメッセージを構築する"""
    status = job_status.get("status", "UNKNOWN")
    exit_code = job_status.get("exit_code", -1)
    stdout_path = paths.job_stdout_log(config, job_id)
    stderr_path = paths.job_stderr_log(config, job_id)

    return (
        "AutoGoalで待機していたバックグラウンドジョブが完了しました。\n"
        "\n"
        f"job_id: {job_id}\n"
        f"status: {status}\n"
        f"exit_code: {exit_code}\n"
        f"stdout: {stdout_path}\n"
        f"stderr: {stderr_path}\n"
        "\n"
        "ログと生成物を確認してください。\n"
        "失敗している場合は原因を調査して修正し、元の目的が検証済みで達成されるまで作業を継続してください。\n"
        "長時間処理が再度必要ならautogoal-jobを使用してください。"
    )


def _build_visible_resume_message(job_id: str, job_status: dict) -> str:
    """Build a single-line, control-character-free visible resume message."""
    paths.validate_identifier(job_id, kind="job ID")
    status = str(job_status.get("status", "UNKNOWN")).upper()
    if status not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
        status = "UNKNOWN"
    try:
        exit_code = int(job_status.get("exit_code", -1))
    except (TypeError, ValueError):
        exit_code = -1
    return (
        f"AutoGoal job {job_id} completed with status {status} "
        f"and exit code {exit_code}. Please inspect its logs and continue."
    )


def _setup_logger(session_dir: Path) -> logging.Logger:
    """watcher用ロガー"""
    logger = logging.getLogger("autogoal.watcher")
    logger.setLevel(logging.DEBUG)

    if not logger.handlers:
        session_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(session_dir / "watcher.log", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s [watcher] %(message)s")
        )
        logger.addHandler(handler)

    return logger


if __name__ == "__main__":
    main()
