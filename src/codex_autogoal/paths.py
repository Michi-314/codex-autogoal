"""ファイルパス解決ユーティリティ"""

from __future__ import annotations

from pathlib import Path

from codex_autogoal.config import Config


def state_dir(config: Config) -> Path:
    """セッション状態ルートディレクトリ"""
    return config.home / "state"


def jobs_dir(config: Config) -> Path:
    """ジョブルートディレクトリ"""
    return config.home / "jobs"


def session_dir(config: Config, session_id: str) -> Path:
    """特定セッションのディレクトリ"""
    return state_dir(config) / session_id


def job_dir(config: Config, job_id: str) -> Path:
    """特定ジョブのディレクトリ"""
    return jobs_dir(config) / job_id


def config_file(config: Config) -> Path:
    """AutoGoal設定ファイル"""
    return config.home / "config.json"


def protocol_file(config: Config) -> Path:
    """プロトコルテンプレート"""
    return config.home / "protocol.md"


# --- セッションファイル ---

def session_json(config: Config, session_id: str) -> Path:
    return session_dir(config, session_id) / "session.json"


def status_json(config: Config, session_id: str) -> Path:
    return session_dir(config, session_id) / "status.json"


def events_jsonl(config: Config, session_id: str) -> Path:
    return session_dir(config, session_id) / "events.jsonl"


def codex_jsonl(config: Config, session_id: str) -> Path:
    return session_dir(config, session_id) / "codex.jsonl"


def last_message_txt(config: Config, session_id: str) -> Path:
    return session_dir(config, session_id) / "last-message.txt"


def watcher_log(config: Config, session_id: str) -> Path:
    return session_dir(config, session_id) / "watcher.log"


def resume_log(config: Config, session_id: str) -> Path:
    return session_dir(config, session_id) / "resume.log"


def watcher_lock(config: Config, session_id: str) -> Path:
    return session_dir(config, session_id) / "watcher.lock"


def cancelled_marker(config: Config, session_id: str) -> Path:
    return session_dir(config, session_id) / "cancelled"


# --- ジョブファイル ---

def job_metadata_json(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "metadata.json"


def job_command_json(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "command.json"


def job_stdout_log(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "stdout.log"


def job_stderr_log(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "stderr.log"


def job_pid_file(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "pid"


def job_exit_code_file(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "exit_code"


def job_started_at_file(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "started_at"


def job_finished_at_file(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "finished_at"


def job_status_json(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "status.json"


def job_done_marker(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "done"


def job_cancelled_marker(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "cancelled"


# --- Codex設定 ---

def codex_config_toml() -> Path:
    """Codexのユーザー設定ファイル"""
    return Path.home() / ".codex" / "config.toml"


def resolve_job_dir_safe(config: Config, job_id: str) -> Path | None:
    """ジョブIDを検証し、jobs root外へのパス解決を拒否する。

    Returns:
        安全なジョブディレクトリパス。不正な場合はNone。
    """
    root = jobs_dir(config).resolve()
    target = (root / job_id).resolve()
    # パストラバーサル防止
    if not str(target).startswith(str(root) + "/") and target != root:
        return None
    return target
