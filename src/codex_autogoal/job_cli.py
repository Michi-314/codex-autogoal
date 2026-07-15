"""autogoal-job CLI"""

from __future__ import annotations

import argparse
import json
import os
import sys

from codex_autogoal.config import load_config
from codex_autogoal.job_runner import (
    cancel_job,
    create_job,
    create_timer_job,
    get_job_status,
    is_job_done,
)
from codex_autogoal import paths
from codex_autogoal.attachment import attach_job


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="autogoal-job",
        description="AutoGoal バックグラウンドジョブ管理",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # start
    p_start = sub.add_parser("start", help="バックグラウンドジョブを開始する")
    p_start.add_argument("--name", help="ジョブ名")
    p_start.add_argument("--json", dest="output_json", action="store_true", help="JSON出力")
    p_start.add_argument("--cwd", help="作業ディレクトリ")
    _add_attachment_arguments(p_start)
    p_start.add_argument("command_args", nargs=argparse.REMAINDER, help="実行コマンド (-- の後)")

    # timer
    p_timer = sub.add_parser("timer", help="タイマージョブを開始する")
    p_timer.add_argument("--name", help="ジョブ名")
    p_timer.add_argument("--after", dest="after", help="待機時間 (例: 30m, 2h)")
    p_timer.add_argument("--at", dest="at_time", help="目標日時 (ISO 8601)")
    p_timer.add_argument("--json", dest="output_json", action="store_true", help="JSON出力")
    _add_attachment_arguments(p_timer)

    # status
    p_status = sub.add_parser("status", help="ジョブの状態を確認する")
    p_status.add_argument("job_id", help="ジョブID")
    p_status.add_argument("--json", dest="output_json", action="store_true", help="JSON出力")

    # logs
    p_logs = sub.add_parser("logs", help="ジョブのログを表示する")
    p_logs.add_argument("job_id", help="ジョブID")
    p_logs.add_argument("--stderr", action="store_true", help="stderrを表示")
    p_logs.add_argument("--tail", type=int, default=0, help="末尾N行のみ表示")

    # cancel
    p_cancel = sub.add_parser("cancel", help="ジョブをキャンセルする")
    p_cancel.add_argument("job_id", help="ジョブID")
    p_cancel.add_argument("--kill", action="store_true", help="プロセスも停止する")

    args = parser.parse_args()
    config = load_config()

    if args.command == "start":
        _cmd_start(config, args)
    elif args.command == "timer":
        _cmd_timer(config, args)
    elif args.command == "status":
        _cmd_status(config, args)
    elif args.command == "logs":
        _cmd_logs(config, args)
    elif args.command == "cancel":
        _cmd_cancel(config, args)


def _cmd_start(config, args) -> None:
    """ジョブ開始"""
    cmd_args = args.command_args
    # "--" をスキップ
    if cmd_args and cmd_args[0] == "--":
        cmd_args = cmd_args[1:]

    if not cmd_args:
        print("エラー: 実行コマンドを指定してください (-- の後)", file=sys.stderr)
        sys.exit(1)

    result = create_job(config, cmd_args, name=args.name, cwd=args.cwd)
    _maybe_attach(config, args, result)

    if args.output_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for k, v in result.items():
            print(f"{k}={v}")


def _cmd_timer(config, args) -> None:
    """タイマージョブ開始"""
    if not args.after and not args.at_time:
        print("エラー: --after または --at を指定してください", file=sys.stderr)
        sys.exit(1)

    try:
        result = create_timer_job(
            config,
            duration_str=args.after,
            at_str=args.at_time,
            name=args.name,
        )
        _maybe_attach(config, args, result)
    except ValueError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)

    if args.output_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        for k, v in result.items():
            print(f"{k}={v}")


def _add_attachment_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--attach-current",
        action="store_true",
        help="現在のCODEX_THREAD_IDへ接続する",
    )
    group.add_argument(
        "--no-attach",
        action="store_true",
        help="現在のCodex threadを自動検出しても接続しない",
    )
    parser.add_argument("--session-id", help="接続先Codex session ID")


def _maybe_attach(config, args, result: dict) -> None:
    """Codex内では既定で現在threadへ接続し、完了時にvisible resumeする。"""
    if args.no_attach:
        return
    env_session = os.environ.get("CODEX_THREAD_ID")
    session_id = args.session_id or env_session
    if args.attach_current and not env_session:
        raise ValueError("CODEX_THREAD_IDがありません")
    if not session_id:
        return
    pane_id = os.environ.get("WEZTERM_PANE")
    watcher_pid = attach_job(
        config,
        session_id=session_id,
        job_id=result["job_id"],
        cwd=getattr(args, "cwd", None) or os.getcwd(),
        pane_id=pane_id,
    )
    result["attached_session_id"] = session_id
    result["resume_mode"] = "wezterm" if pane_id else "headless"
    result["watcher_pid"] = watcher_pid


def _cmd_status(config, args) -> None:
    """ジョブ状態確認"""
    status = get_job_status(config, args.job_id)
    if status is None:
        print(f"エラー: ジョブが見つかりません: {args.job_id}", file=sys.stderr)
        sys.exit(1)

    if args.output_json:
        print(json.dumps(status, ensure_ascii=False))
    else:
        for k, v in status.items():
            print(f"{k}={v}")


def _cmd_logs(config, args) -> None:
    """ジョブログ表示"""
    if args.stderr:
        log_path = paths.job_stderr_log(config, args.job_id)
    else:
        log_path = paths.job_stdout_log(config, args.job_id)

    if not log_path.exists():
        print(f"エラー: ログファイルが見つかりません: {log_path}", file=sys.stderr)
        sys.exit(1)

    content = log_path.read_text()
    if args.tail > 0:
        lines = content.splitlines()
        content = "\n".join(lines[-args.tail:])

    print(content, end="")


def _cmd_cancel(config, args) -> None:
    """ジョブキャンセル"""
    success = cancel_job(config, args.job_id, kill=args.kill)
    if success:
        print(f"ジョブ {args.job_id} をキャンセルしました")
    else:
        print(f"エラー: ジョブが見つかりません: {args.job_id}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
