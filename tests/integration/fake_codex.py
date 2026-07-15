#!/usr/bin/env python3
"""Fake Codex CLI - 統合テスト用モック

codex exec --json と codex exec resume をシミュレートする。
環境変数 FAKE_CODEX_SCENARIO でシナリオを切り替える。
"""

import json
import os
import sys
import time


def main():
    args = sys.argv[1:]

    if "--version" in args:
        print("codex-cli 0.144.1 (fake)")
        return

    if not args or args[0] != "exec":
        print(f"Unknown command: {args}", file=sys.stderr)
        sys.exit(1)

    args = args[1:]  # exec を消す

    if args and args[0] == "resume":
        _handle_resume(args[1:])
    elif args and args[0] == "--help":
        print("Fake codex exec help")
    else:
        _handle_exec(args)


def _handle_exec(args: list[str]):
    """codex exec --json のシミュレーション"""
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "simple_done")
    thread_id = os.environ.get("FAKE_CODEX_THREAD_ID", "fake-thread-001")

    is_json = "--json" in args

    if not is_json:
        print("Non-JSON mode not supported in fake", file=sys.stderr)
        sys.exit(1)

    if scenario == "simple_done":
        _scenario_simple_done(thread_id)
    elif scenario == "continue_then_done":
        _scenario_continue_then_done(thread_id)
    elif scenario == "wait_for_job":
        _scenario_wait_for_job(thread_id)
    elif scenario == "malformed_signal":
        _scenario_malformed_signal(thread_id)
    elif scenario == "no_signal":
        _scenario_no_signal(thread_id)
    else:
        print(f"Unknown scenario: {scenario}", file=sys.stderr)
        sys.exit(1)


def _handle_resume(args: list[str]):
    """codex exec resume のシミュレーション"""
    scenario = os.environ.get("FAKE_CODEX_RESUME_SCENARIO", "done_after_resume")

    # session_idとpromptを取得
    session_id = None
    prompt = None
    is_json = "--json" in args

    non_flag_args = [a for a in args if not a.startswith("-")]
    if len(non_flag_args) >= 1:
        session_id = non_flag_args[0]
    if len(non_flag_args) >= 2:
        prompt = non_flag_args[1]

    if scenario == "done_after_resume":
        _scenario_done_after_resume(session_id or "unknown")
    elif scenario == "fail_resume":
        sys.exit(1)


def _emit(event: dict):
    """JSONLイベントを出力"""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def _scenario_simple_done(thread_id: str):
    """ターン1でdoneを返す"""
    _emit({"type": "thread.started", "thread_id": thread_id})
    _emit({"type": "turn.started", "turn_id": "turn-1"})
    _emit({
        "type": "message",
        "role": "assistant",
        "content": '作業完了しました。\nAUTOGOAL_SIGNAL: {"state":"done","reason":"全テスト通過"}'
    })
    _emit({"type": "turn.completed", "turn_id": "turn-1", "usage": {
        "input_tokens": 100, "output_tokens": 50
    }})


def _scenario_continue_then_done(thread_id: str):
    """ターン1: continue, ターン2: continue, ターン3: done"""
    turn_num = int(os.environ.get("FAKE_CODEX_TURN", "1"))

    _emit({"type": "thread.started", "thread_id": thread_id})
    _emit({"type": "turn.started", "turn_id": f"turn-{turn_num}"})

    if turn_num <= 2:
        _emit({
            "type": "message",
            "role": "assistant",
            "content": f'ターン{turn_num}の作業中\nAUTOGOAL_SIGNAL: {{"state":"continue","reason":"ターン{turn_num}完了、次へ"}}'
        })
    else:
        _emit({
            "type": "message",
            "role": "assistant",
            "content": 'AUTOGOAL_SIGNAL: {"state":"done","reason":"全作業完了"}'
        })

    _emit({"type": "turn.completed", "turn_id": f"turn-{turn_num}", "usage": {
        "input_tokens": 100, "output_tokens": 50
    }})


def _scenario_wait_for_job(thread_id: str):
    """ジョブ待ちシナリオ"""
    job_id = os.environ.get("FAKE_CODEX_JOB_ID", "test-job-001")

    _emit({"type": "thread.started", "thread_id": thread_id})
    _emit({"type": "turn.started", "turn_id": "turn-1"})
    _emit({
        "type": "message",
        "role": "assistant",
        "content": f'バックグラウンドジョブを開始しました。\nAUTOGOAL_SIGNAL: {{"state":"wait","job_id":"{job_id}","reason":"ジョブ完了待ち"}}'
    })
    _emit({"type": "turn.completed", "turn_id": "turn-1", "usage": {
        "input_tokens": 100, "output_tokens": 50
    }})


def _scenario_done_after_resume(session_id: str):
    """resume後にdone"""
    _emit({"type": "thread.started", "thread_id": session_id})
    _emit({"type": "turn.started", "turn_id": "turn-resume"})
    _emit({
        "type": "message",
        "role": "assistant",
        "content": 'ジョブ結果を確認しました。全て正常です。\nAUTOGOAL_SIGNAL: {"state":"done","reason":"ジョブ結果確認完了"}'
    })
    _emit({"type": "turn.completed", "turn_id": "turn-resume", "usage": {
        "input_tokens": 200, "output_tokens": 100
    }})


def _scenario_malformed_signal(thread_id: str):
    """壊れたシグナル"""
    _emit({"type": "thread.started", "thread_id": thread_id})
    _emit({"type": "turn.started", "turn_id": "turn-1"})
    _emit({
        "type": "message",
        "role": "assistant",
        "content": '作業中...\nAUTOGOAL_SIGNAL: {broken json here'
    })
    _emit({"type": "turn.completed", "turn_id": "turn-1", "usage": {
        "input_tokens": 100, "output_tokens": 50
    }})


def _scenario_no_signal(thread_id: str):
    """シグナルなし"""
    _emit({"type": "thread.started", "thread_id": thread_id})
    _emit({"type": "turn.started", "turn_id": "turn-1"})
    _emit({
        "type": "message",
        "role": "assistant",
        "content": 'テスト完了。問題なし。'
    })
    _emit({"type": "turn.completed", "turn_id": "turn-1", "usage": {
        "input_tokens": 100, "output_tokens": 50
    }})


if __name__ == "__main__":
    main()
