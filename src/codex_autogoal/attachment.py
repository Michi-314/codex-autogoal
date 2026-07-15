"""既存Codex threadとバックグラウンドジョブの明示的な接続。"""

from __future__ import annotations

import logging
import os
import re

from codex_autogoal import paths
from codex_autogoal.config import Config
from codex_autogoal.state import SessionState, SessionStatus, StateManager, now_iso
from codex_autogoal.watcher import launch_watcher


def attach_job(
    config: Config,
    *,
    session_id: str,
    job_id: str,
    cwd: str | None = None,
    pane_id: str | None = None,
    reason: str = "autogoal-job auto attachment",
) -> int:
    """jobを既存threadへ接続し、完了watcherを起動する。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("不正なsession ID")
    job_dir = paths.resolve_job_dir_safe(config, job_id)
    if job_dir is None or not job_dir.exists():
        raise ValueError(f"ジョブが存在しません: {job_id}")

    mgr = StateManager(paths.session_dir(config, session_id))
    state = mgr.read()
    if state is None:
        state = SessionState(
            session_id=session_id,
            cwd=cwd or os.getcwd(),
            status=SessionStatus.RUNNING,
            created_at=now_iso(),
        )
        mgr.ensure_dir()
        mgr.write(state)
    if state.status != SessionStatus.RUNNING:
        raise ValueError(f"sessionはRUNNINGではありません: {state.status.value}")

    state.cwd = cwd or state.cwd or os.getcwd()
    state.resume_mode = "wezterm" if pane_id else "headless"
    state.terminal_pane_id = pane_id
    mgr.write(state)
    if not mgr.transition(state, SessionStatus.WAITING, reason=reason, job_id=job_id):
        raise ValueError("WAITINGへの遷移に失敗しました")
    mgr.append_event({
        "type": "job_attached",
        "job_id": job_id,
        "resume_mode": state.resume_mode,
        "pane_id": pane_id,
        "timestamp": now_iso(),
    })

    logger = logging.getLogger(f"autogoal.attachment.{session_id}")
    return launch_watcher(config, session_id, job_id, logger)
