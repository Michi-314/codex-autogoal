"""protocol.py の単体テスト"""

import pytest

from codex_autogoal.protocol import (
    ParsedSignal,
    SignalError,
    SignalState,
    extract_last_nonempty_line,
    normalize_message_hash,
    parse_signal,
    validate_job_id,
)


class TestExtractLastNonemptyLine:
    def test_simple(self):
        assert extract_last_nonempty_line("hello\nworld") == "world"

    def test_trailing_newlines(self):
        assert extract_last_nonempty_line("hello\nworld\n\n\n") == "world"

    def test_single_line(self):
        assert extract_last_nonempty_line("hello") == "hello"

    def test_empty(self):
        assert extract_last_nonempty_line("") == ""

    def test_only_whitespace(self):
        assert extract_last_nonempty_line("   \n  \n  ") == ""


class TestParseSignalContinue:
    def test_normal(self):
        msg = 'テスト結果OK\nAUTOGOAL_SIGNAL: {"state":"continue","reason":"残りのテストを実行する"}'
        result = parse_signal(msg)
        assert result.ok
        assert result.signal.state == SignalState.CONTINUE
        assert result.signal.reason == "残りのテストを実行する"
        assert result.signal.job_id is None

    def test_with_trailing_newlines(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":"continue","reason":"OK"}\n\n'
        result = parse_signal(msg)
        assert result.ok
        assert result.signal.state == SignalState.CONTINUE


class TestParseSignalWait:
    def test_normal(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":"wait","job_id":"20260710T012345Z-a1b2c3","reason":"バックテスト完了待ち"}'
        result = parse_signal(msg)
        assert result.ok
        assert result.signal.state == SignalState.WAIT
        assert result.signal.job_id == "20260710T012345Z-a1b2c3"
        assert result.signal.reason == "バックテスト完了待ち"

    def test_missing_job_id(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":"wait","reason":"待ち"}'
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.MISSING_JOB_ID

    def test_empty_job_id(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":"wait","job_id":"","reason":"待ち"}'
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.MISSING_JOB_ID


class TestParseSignalDone:
    def test_normal(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":"done","reason":"実装と検証がすべて完了した"}'
        result = parse_signal(msg)
        assert result.ok
        assert result.signal.state == SignalState.DONE


class TestParseSignalBlocked:
    def test_normal(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":"blocked","reason":"ユーザーのAPIキー入力が必要"}'
        result = parse_signal(msg)
        assert result.ok
        assert result.signal.state == SignalState.BLOCKED


class TestParseSignalIgnoreNonLastLine:
    def test_signal_in_middle_ignored(self):
        msg = (
            'AUTOGOAL_SIGNAL: {"state":"continue","reason":"中間行"}\n'
            '最終行は普通のテキスト'
        )
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.NO_SIGNAL


class TestParseSignalErrors:
    def test_broken_json(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":"continue",'
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.INVALID_JSON

    def test_unknown_state(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":"unknown_state","reason":"test"}'
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.UNKNOWN_STATE

    def test_too_long(self):
        msg = "x" * 200_000
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.INPUT_TOO_LONG

    def test_path_traversal_job_id(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":"wait","job_id":"../../../etc/passwd","reason":"evil"}'
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.INVALID_JOB_ID

    def test_non_string_state(self):
        msg = 'AUTOGOAL_SIGNAL: {"state":123,"reason":"test"}'
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.INVALID_TYPE

    def test_non_object_signal(self):
        msg = 'AUTOGOAL_SIGNAL: [1,2,3]'
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.INVALID_TYPE

    def test_no_signal(self):
        msg = "これは普通のメッセージです"
        result = parse_signal(msg)
        assert not result.ok
        assert result.error == SignalError.NO_SIGNAL


class TestValidateJobId:
    def test_valid(self):
        assert validate_job_id("20260710T012345Z-a1b2c3")
        assert validate_job_id("test_job-123")
        assert validate_job_id("a")

    def test_invalid(self):
        assert not validate_job_id("")
        assert not validate_job_id("../evil")
        assert not validate_job_id("a" * 129)
        assert not validate_job_id("has space")
        assert not validate_job_id("has/slash")


class TestNormalizeMessageHash:
    def test_same_content_different_whitespace(self):
        h1 = normalize_message_hash("hello  world")
        h2 = normalize_message_hash("hello world")
        assert h1 == h2

    def test_different_content(self):
        h1 = normalize_message_hash("hello")
        h2 = normalize_message_hash("world")
        assert h1 != h2
