"""プロセス分離ユーティリティ"""

from __future__ import annotations

import os
import hashlib
import subprocess
import sys
from pathlib import Path
from typing import IO


def spawn_detached(
    command: list[str],
    *,
    stdout: IO[bytes] | int | None = subprocess.DEVNULL,
    stderr: IO[bytes] | int | None = subprocess.DEVNULL,
    env: dict[str, str] | None = None,
    cwd: str | Path | None = None,
) -> subprocess.Popen:
    """親プロセスの終了に巻き込まれない完全分離プロセスを起動する。

    Args:
        command: 実行コマンド（argv配列）
        stdout: 標準出力先
        stderr: 標準エラー出力先
        env: 環境変数（Noneなら現在の環境を継承）
        cwd: 作業ディレクトリ

    Returns:
        起動されたPopen オブジェクト
    """
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)

    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        close_fds=True,
        env=merged_env,
        cwd=cwd,
    )


def get_python_executable() -> str:
    """現在のPythonインタプリタのパスを返す。"""
    return sys.executable


def process_fingerprint(pid: int) -> str | None:
    """Return a process birth/command fingerprint for PID reuse checks."""
    try:
        result = subprocess.run(
            ["/bin/ps", "-o", "lstart=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
