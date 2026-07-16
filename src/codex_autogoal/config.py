"""環境変数とデフォルト設定の管理"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Config:
    """AutoGoalの設定値。環境変数 → デフォルト値のフォールバック。"""

    home: Path = field(default_factory=lambda: Path(
        os.environ.get("CODEX_AUTOGOAL_HOME", "")
    ) if os.environ.get("CODEX_AUTOGOAL_HOME") else Path.home() / ".codex" / "autogoal")

    enabled: bool = field(default_factory=lambda: os.environ.get(
        "CODEX_AUTOGOAL_ENABLED", "0"
    ) == "1")

    max_turns: int = field(default_factory=lambda: int(os.environ.get(
        "CODEX_AUTOGOAL_MAX_TURNS", "50"
    )))

    max_resume_attempts: int = field(default_factory=lambda: int(os.environ.get(
        "CODEX_AUTOGOAL_MAX_RESUME_ATTEMPTS", "3"
    )))

    max_job_log_bytes: int = field(default_factory=lambda: int(os.environ.get(
        "CODEX_AUTOGOAL_MAX_JOB_LOG_BYTES", str(100 * 1024 * 1024)
    )))

    codex_bin: str = field(default_factory=lambda: os.environ.get(
        "CODEX_AUTOGOAL_CODEX_BIN", "codex"
    ))

    bypass_hook_trust: bool = field(default_factory=lambda: os.environ.get(
        "CODEX_AUTOGOAL_BYPASS_HOOK_TRUST", "0"
    ) == "1")


def load_config() -> Config:
    """現在の環境変数からConfigを生成する。"""
    return Config()
