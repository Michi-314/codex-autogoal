"""Codex exec セッション起動とJSONLストリーム処理"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from codex_autogoal.config import Config
from codex_autogoal import paths
from codex_autogoal.process import sanitized_environment
from codex_autogoal.state import (
    SessionState,
    SessionStatus,
    StateManager,
    now_iso,
)


def build_prompt(
    user_prompt: str,
    protocol_template: str,
) -> str:
    """ユーザープロンプトにAutoGoalプロトコル指示を付加する。"""
    return (
        "--- AutoGoal Protocol ---\n"
        f"{protocol_template}\n"
        "--- End AutoGoal Protocol ---\n"
        "\n"
        "--- ユーザータスク ---\n"
        f"{user_prompt}\n"
        "--- End ユーザータスク ---"
    )


def run_codex_session(
    config: Config,
    prompt: str,
    *,
    cwd: str | None = None,
    sandbox: str = "workspace-write",
    model: str | None = None,
    bypass_hook_trust: bool = False,
) -> int:
    """Codexセッションを起動し、JSONLストリームを処理する。

    Args:
        config: 設定
        prompt: 組み立て済みプロンプト
        cwd: 作業ディレクトリ
        sandbox: サンドボックスモード

    Returns:
        Codexの終了コード
    """
    cmd = [
        config.codex_bin,
        "exec",
        "--sandbox", sandbox,
        "--json",
        "--",
        prompt,
    ]

    if model:
        cmd[2:2] = ["--model", model]
    if bypass_hook_trust:
        cmd[2:2] = ["--dangerously-bypass-hook-trust"]

    if cwd:
        cmd.insert(2, "--cd")
        cmd.insert(3, cwd)

    env = sanitized_environment()
    env["CODEX_AUTOGOAL_ENABLED"] = "1"
    env["CODEX_AUTOGOAL_HOME"] = str(config.home)
    if bypass_hook_trust:
        env["CODEX_AUTOGOAL_BYPASS_HOOK_TRUST"] = "1"

    # 一時バッファ（thread_id取得前）
    temp_dir = tempfile.mkdtemp(prefix="autogoal_")
    temp_log = Path(temp_dir) / "codex_buffer.jsonl"

    session_id: str | None = None
    state_mgr: StateManager | None = None

    try:
        print(f"[autogoal] Codexセッションを起動します...", file=sys.stderr)
        print(f"[autogoal] sandbox: {sandbox}", file=sys.stderr)

        stderr_path = Path(temp_dir) / "codex.stderr"
        with open(stderr_path, "wb") as stderr_f:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=stderr_f,
                env=env,
                cwd=cwd,
            )

        with open(temp_log, "w") as buf_f:
            if proc.stdout:
                for line_bytes in proc.stdout:
                    line = line_bytes.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue

                    # JSONパース
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        buf_f.write(line + "\n")
                        continue

                    event_type = event.get("type", "")

                    # thread_id取得
                    if event_type == "thread.started" and session_id is None:
                        session_id = event.get("thread_id", "")
                        if session_id:
                            print(f"[autogoal] session_id: {session_id}", file=sys.stderr)
                            # セッションディレクトリ作成
                            sdir = paths.session_dir(config, session_id)
                            sdir.mkdir(parents=True, exist_ok=True)

                            state_mgr = StateManager(sdir)

                            # 初期状態
                            state = SessionState(
                                session_id=session_id,
                                cwd=cwd or os.getcwd(),
                                status=SessionStatus.RUNNING,
                                created_at=now_iso(),
                                sandbox_mode=sandbox,
                                codex_pid=proc.pid,
                            )
                            state_mgr.write(state)

                            # バッファから正式ログへ移動
                            codex_log = paths.codex_jsonl(config, session_id)
                            buf_f.flush()
                            # バッファの内容をコピー
                            with open(temp_log, "r") as src:
                                with paths.open_private_write(codex_log) as dst:
                                    dst.write(src.read())

                    # 正式ログに書き込み
                    if session_id:
                        codex_log_path = paths.codex_jsonl(config, session_id)
                        with paths.open_private_append(codex_log_path) as log_f:
                            log_f.write(line + "\n")
                    else:
                        buf_f.write(line + "\n")

                    # イベント処理
                    _process_event(event, event_type, session_id, state_mgr)

        exit_code = proc.wait()

        # stderr
        stderr_content = stderr_path.read_text(encoding="utf-8", errors="replace")
        if stderr_content:
            print(f"[autogoal] stderr: {stderr_content}", file=sys.stderr)

        print(f"[autogoal] Codex終了: exit_code={exit_code}", file=sys.stderr)

        return exit_code

    except FileNotFoundError:
        print(f"[autogoal] Codex CLIが見つかりません: {config.codex_bin}", file=sys.stderr)
        return 127
    finally:
        # 一時ディレクトリ削除
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except OSError:
            pass


def _process_event(
    event: dict,
    event_type: str,
    session_id: str | None,
    state_mgr: StateManager | None,
) -> None:
    """JSONLイベントを処理する。"""
    if not session_id or not state_mgr:
        return

    if event_type == "turn.started":
        turn_id = event.get("turn_id", "")
        state_mgr.append_event({
            "type": "turn.started",
            "turn_id": turn_id,
            "timestamp": now_iso(),
        })
        print(f"[autogoal] turn開始: {turn_id}", file=sys.stderr)

    elif event_type == "turn.completed":
        usage = event.get("usage", {})
        state = state_mgr.read()
        if state and usage:
            state.token_usage.add(usage)
            state.last_turn_id = event.get("turn_id", "")
            state_mgr.write(state)

        state_mgr.append_event({
            "type": "turn.completed",
            "turn_id": event.get("turn_id", ""),
            "usage": usage,
            "timestamp": now_iso(),
        })
        print(f"[autogoal] turn完了", file=sys.stderr)

    elif event_type == "turn.failed":
        state_mgr.append_event({
            "type": "turn.failed",
            "error": event.get("error", ""),
            "timestamp": now_iso(),
        })
        print(f"[autogoal] turn失敗: {event.get('error', '')}", file=sys.stderr)

    elif event_type == "error":
        state_mgr.append_event({
            "type": "error",
            "error": event.get("error", ""),
            "timestamp": now_iso(),
        })
        print(f"[autogoal] エラー: {event.get('error', '')}", file=sys.stderr)

    elif event_type == "message":
        role = event.get("role", "")
        content = event.get("content", "")
        if role == "assistant" and content:
            # メッセージの一部を表示
            preview = content[:200].replace("\n", " ")
            print(f"[autogoal] assistant: {preview}...", file=sys.stderr)
