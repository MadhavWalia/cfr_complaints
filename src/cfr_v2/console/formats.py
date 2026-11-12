"""How the console writes times, durations, sizes and commands."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Sequence
from datetime import datetime


def when(iso: str, now: datetime | None = None) -> str:
    """Local time: ``16:50`` today, ``11 Nov 16:50`` before."""
    moment = datetime.fromisoformat(iso).astimezone()
    today = (now or datetime.now().astimezone()).date()
    return f"{moment:%H:%M}" if moment.date() == today else f"{moment:%-d %b %H:%M}"


def took(seconds: float) -> str:
    """``14 s``, ``8 min``, ``9 h 35 min``."""
    seconds = round(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


def size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:,} {unit}" if unit == "bytes" else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} GB"


def shell(argv: Sequence[str]) -> str:
    """The command as it would be typed: ``python -m …`` rather than the interpreter's full path."""
    parts = ["python" if i == 0 and a == sys.executable else a for i, a in enumerate(argv)]
    return shlex.join(parts)
