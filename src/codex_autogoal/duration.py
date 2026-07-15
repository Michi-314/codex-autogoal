"""duration文字列(30s, 10m, 2h, 1d)のパースとISO 8601日時パース"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_DURATION_RE = re.compile(r"^(\d+)([smhd])$")

_UNIT_MAP = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
}


def parse_duration(text: str) -> timedelta:
    """'30s', '10m', '2h', '1d' 形式の文字列をtimedeltaに変換する。

    Raises:
        ValueError: 不正な形式
    """
    m = _DURATION_RE.match(text.strip())
    if not m:
        raise ValueError(f"不正なduration形式: {text!r}  (例: 30s, 10m, 2h, 1d)")
    value = int(m.group(1))
    unit = m.group(2)
    return timedelta(**{_UNIT_MAP[unit]: value})


def parse_datetime_or_duration(text: str) -> datetime:
    """ISO 8601日時文字列またはduration文字列を絶対日時に変換する。

    duration の場合は現在時刻に加算する。

    Raises:
        ValueError: パース失敗
    """
    text = text.strip()

    # durationパターンを先に試す
    if _DURATION_RE.match(text):
        return datetime.now(timezone.utc) + parse_duration(text)

    # ISO 8601パース
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise ValueError(
            f"不正な日時/duration形式: {text!r}  "
            "(例: 2026-07-10T18:00:00+09:00 または 30m)"
        )


def format_duration(td: timedelta) -> str:
    """timedeltaを人間可読な文字列に変換する。"""
    total_seconds = int(td.total_seconds())
    if total_seconds < 0:
        return "0s"
    if total_seconds == 0:
        return "0s"

    parts = []
    days = total_seconds // 86400
    if days:
        parts.append(f"{days}d")
    hours = (total_seconds % 86400) // 3600
    if hours:
        parts.append(f"{hours}h")
    minutes = (total_seconds % 3600) // 60
    if minutes:
        parts.append(f"{minutes}m")
    secs = total_seconds % 60
    if secs or not parts:
        parts.append(f"{secs}s")
    return "".join(parts)
