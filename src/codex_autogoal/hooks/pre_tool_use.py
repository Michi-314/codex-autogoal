#!/usr/bin/env python3
"""AutoGoal PreToolUse Hook

AutoGoalモード時に、明らかな待機・ポーリングコマンドを検出して拒否する。
補助的なガードであり、完全なセキュリティ境界ではない。
"""

from __future__ import annotations

import json
import os
import re
import sys


# 拒否対象パターン
_REJECT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # 10秒以上のsleep
    (re.compile(r"\bsleep\s+(\d+)"), "sleep_long"),
    # watch コマンド
    (re.compile(r"^\s*watch\b"), "watch"),
    # tail -f
    (re.compile(r"\btail\s+.*-[fF]"), "tail_follow"),
    # while/until + sleep ループ
    (re.compile(r"\b(while|until)\b.*\bsleep\b"), "poll_loop"),
    # for + sleep ループ
    (re.compile(r"\bfor\b.*\bsleep\b"), "poll_loop"),
]

_REJECT_MESSAGE = (
    "AutoGoalモードではCodex自身による長時間待機・ポーリングは禁止されています。\n"
    "長時間処理は `autogoal-job start -- <command>` を使用し、\n"
    "時間経過待ちは `autogoal-job timer --after <duration>` を使用してください。"
)


def main() -> None:
    """PreToolUse Hookメインエントリポイント"""
    try:
        _run_hook()
    except Exception:
        # 例外時は通常動作を妨げない
        _emit_approve()


def _run_hook() -> None:
    # CODEX_AUTOGOAL_ENABLED確認
    if os.environ.get("CODEX_AUTOGOAL_ENABLED") != "1":
        _emit_approve()
        return

    # stdin読み取り
    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        _emit_approve()
        return

    # tool_name確認（Bashのみ対象）
    tool_name = hook_input.get("tool_name", "")
    if tool_name != "Bash":
        _emit_approve()
        return

    # コマンド取得
    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")
    if not command:
        _emit_approve()
        return

    # 正規のdetached runner内に長時間commandが含まれるのは許可する。
    if re.search(r"(?:^|[/\s\"'])autogoal-job\s+(?:start|timer)\b", command):
        _emit_approve()
        return

    # パターンチェック
    for pattern, reason in _REJECT_PATTERNS:
        match = pattern.search(command)
        if match:
            # sleepの場合、10秒未満は許可
            if reason == "sleep_long":
                try:
                    seconds = int(match.group(1))
                    if seconds < 10:
                        continue  # 短いsleepは許可
                except (ValueError, IndexError):
                    pass

            _emit_reject(reason)
            return

    _emit_approve()


def _emit_approve() -> None:
    """コマンド実行を許可する応答"""
    # PreToolUseではlegacy approveは未対応。空JSONのexit 0がno-op成功。
    print("{}", flush=True)


def _emit_reject(reason: str) -> None:
    """コマンド実行を拒否する応答"""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": _REJECT_MESSAGE,
        }
    }, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
