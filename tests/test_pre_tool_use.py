"""PreToolUse hook output compatibility tests."""

from __future__ import annotations

import io
import json
from unittest.mock import patch

from codex_autogoal.hooks.pre_tool_use import main


def _run(command: str, capsys) -> dict:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }
    with patch.dict("os.environ", {"CODEX_AUTOGOAL_ENABLED": "1"}), patch(
        "sys.stdin", io.StringIO(json.dumps(payload))
    ):
        main()
    return json.loads(capsys.readouterr().out)


def test_short_sleep_is_noop(capsys):
    assert _run("sleep 1", capsys) == {}


def test_long_sleep_uses_current_deny_shape(capsys):
    result = _run("sleep 10", capsys)
    output = result["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"


def test_long_sleep_inside_autogoal_job_is_allowed(capsys):
    assert _run("autogoal-job start -- sh -c 'sleep 300'", capsys) == {}


def test_non_bash_is_noop(capsys):
    payload = {"tool_name": "apply_patch", "tool_input": {"command": "sleep 30"}}
    with patch.dict("os.environ", {"CODEX_AUTOGOAL_ENABLED": "1"}), patch(
        "sys.stdin", io.StringIO(json.dumps(payload))
    ):
        main()
    assert json.loads(capsys.readouterr().out) == {}
