"""ファイルパス解決ユーティリティ"""

from __future__ import annotations

import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from codex_autogoal.config import Config


_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def validate_identifier(value: str, *, kind: str = "identifier") -> str:
    """Validate an identifier before it is used as a path component."""
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"不正な{kind}: {value!r}")
    return value


def secure_umask() -> None:
    """Make newly-created runtime state private to the current user."""
    os.umask(0o077)


def ensure_private_dir(path: Path) -> Path:
    if path.is_symlink():
        raise ValueError(f"runtime directory must not be a symlink: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def harden_runtime_permissions(config: Config) -> Path | None:
    """Harden runtime state, quarantining the entire legacy home on any symlink."""
    home = config.home
    quarantine = None
    if home.is_symlink() or (home.exists() and _runtime_tree_contains_unsafe_node(home)):
        quarantine = _quarantine_runtime_home(home)
    ensure_private_dir(home)
    for root, dirs, files in os.walk(home, followlinks=False):
        root_path = Path(root)
        root_path.chmod(0o700)
        for name in dirs:
            item = root_path / name
            item.chmod(0o700)
        for name in files:
            item = root_path / name
            item.chmod(0o600)
    return quarantine


def _runtime_tree_contains_unsafe_node(home: Path) -> bool:
    """Reject links and special nodes without following anything in the runtime tree."""
    for root, dirs, files in os.walk(home, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in dirs:
            try:
                if not stat.S_ISDIR((root_path / name).lstat().st_mode):
                    return True
            except FileNotFoundError:
                continue
        for name in files:
            try:
                if not stat.S_ISREG((root_path / name).lstat().st_mode):
                    return True
            except FileNotFoundError:
                continue
    return False


def _quarantine_runtime_home(home: Path) -> Path:
    """Atomically move an unsafe legacy home aside and create an empty replacement."""
    home.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = home.with_name(
        f"{home.name}.quarantine-{stamp}-{uuid.uuid4().hex[:8]}"
    )
    home.rename(quarantine)
    ensure_private_dir(home)
    return quarantine


def write_private_text(path: Path, content: str) -> None:
    """Write a trusted control file without following a final-component symlink."""
    ensure_private_dir(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
    fd = os.open(path, flags, 0o600)
    try:
        _validate_private_write_fd(fd, path)
        os.ftruncate(fd, 0)
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def touch_private(path: Path) -> None:
    """Create a private marker without following a symlink."""
    ensure_private_dir(path.parent)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        _validate_private_write_fd(fd, path)
    finally:
        os.close(fd)


def open_private_append(path: Path) -> TextIO:
    """Open a private append-only control log without following symlinks."""
    ensure_private_dir(path.parent)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        _validate_private_write_fd(fd, path)
    except Exception:
        os.close(fd)
        raise
    return os.fdopen(fd, "a", encoding="utf-8")


def open_private_write(path: Path) -> TextIO:
    """Open a private truncated file without following symlinks."""
    ensure_private_dir(path.parent)
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
        0o600,
    )
    try:
        _validate_private_write_fd(fd, path)
        os.ftruncate(fd, 0)
    except Exception:
        os.close(fd)
        raise
    return os.fdopen(fd, "w", encoding="utf-8")


def _validate_private_write_fd(fd: int, path: Path) -> None:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"control path is not a regular file: {path}")
    if info.st_uid != os.getuid():
        raise PermissionError(f"control file is not owned by current user: {path}")
    os.fchmod(fd, 0o600)


def read_private_text(path: Path, *, max_bytes: int = 1_048_576) -> str:
    """Read a trusted control file only if owner, mode, and type are safe."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"control path is not a regular file: {path}")
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PermissionError(f"unsafe control file ownership or mode: {path}")
        if info.st_size > max_bytes:
            raise ValueError(f"control file exceeds {max_bytes} bytes: {path}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            chunk = os.read(fd, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise ValueError(f"control file exceeds {max_bytes} bytes: {path}")
        return data.decode("utf-8")
    finally:
        os.close(fd)


def is_private_regular_file(path: Path) -> bool:
    """Check a marker without following symlinks."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077
    )


def state_dir(config: Config) -> Path:
    """セッション状態ルートディレクトリ"""
    return config.home / "state"


def jobs_dir(config: Config) -> Path:
    """ジョブルートディレクトリ"""
    return config.home / "jobs"


def session_dir(config: Config, session_id: str) -> Path:
    """特定セッションのディレクトリ"""
    return _safe_child(
        state_dir(config),
        validate_identifier(session_id, kind="session ID"),
    )


def job_dir(config: Config, job_id: str) -> Path:
    """特定ジョブのディレクトリ"""
    return _safe_child(
        jobs_dir(config),
        validate_identifier(job_id, kind="job ID"),
    )


def _safe_child(root: Path, identifier: str) -> Path:
    resolved_root = root.resolve()
    target = (root / identifier).resolve()
    if target.parent != resolved_root:
        raise ValueError(f"path escapes runtime root: {identifier!r}")
    return target


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


def job_process_identity_json(config: Config, job_id: str) -> Path:
    return job_dir(config, job_id) / "process-identity.json"


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
    try:
        validate_identifier(job_id, kind="job ID")
    except ValueError:
        return None
    root = jobs_dir(config).resolve()
    target = (root / job_id).resolve()
    # パストラバーサル防止
    if not str(target).startswith(str(root) + "/") and target != root:
        return None
    return target
