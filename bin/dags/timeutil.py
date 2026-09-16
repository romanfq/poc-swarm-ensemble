"""Wall-clock helpers with skew correction.

The logical clock (Ch.6.1) orders events, but lease expiry (Ch.6.5) needs to
know how much real time has passed. Each machine measures its offset from
GitHub's clock once at startup (``calibrate``) and every timestamp it writes or
compares goes through ``now()``, so residual skew between machines stays far
below the lease window.
"""
from __future__ import annotations

import email.utils
import threading
from datetime import datetime, timezone

_offset_seconds = 0.0
_lock = threading.Lock()


def set_offset(seconds: float) -> None:
    global _offset_seconds
    with _lock:
        _offset_seconds = float(seconds)


def offset() -> float:
    return _offset_seconds


def now() -> datetime:
    """Skew-corrected current UTC time."""
    return datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() + _offset_seconds, timezone.utc)


def iso(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def offset_from_http_date(http_date: str, local: datetime | None = None) -> float:
    """Seconds to add to the local clock so it matches a server's Date header."""
    server = email.utils.parsedate_to_datetime(http_date)
    if server.tzinfo is None:
        server = server.replace(tzinfo=timezone.utc)
    local = local or datetime.now(timezone.utc)
    return (server - local).total_seconds()


def age_seconds(then, at: datetime | None = None) -> float | None:
    dt = parse(then)
    if dt is None:
        return None
    return ((at or now()) - dt).total_seconds()
