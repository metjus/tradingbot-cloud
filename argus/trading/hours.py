"""Trading session rules (§16). All times UTC internally.

Crypto trades 24/7. Stock sessions use fixed UTC windows configured per
symbol prefix; the executor refuses entries outside the session and
flattens positions shortly before the close so nothing is held overnight.
DST note: US cash session is configured in UTC and should be adjusted via
config when DST shifts; a timezone-aware session table arrives with the
live stock adapter (phase 12).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone


@dataclass(slots=True)
class Session:
    always_open: bool = False
    open_utc: time = time(14, 30)
    close_utc: time = time(21, 0)
    flat_before_close_min: int = 10
    weekdays_only: bool = True


CRYPTO = Session(always_open=True)
US_STOCKS = Session(always_open=False, open_utc=time(14, 30), close_utc=time(21, 0),
                    flat_before_close_min=10, weekdays_only=True)


def session_for(symbol: str) -> Session:
    return CRYPTO if "/" in symbol else US_STOCKS


def _minutes(t: time) -> int:
    return t.hour * 60 + t.minute


def can_enter(symbol: str, now: datetime | None = None) -> bool:
    s = session_for(symbol)
    if s.always_open:
        return True
    now = now or datetime.now(timezone.utc)
    if s.weekdays_only and now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return _minutes(s.open_utc) <= m < _minutes(s.close_utc) - s.flat_before_close_min


def must_flatten(symbol: str, now: datetime | None = None) -> bool:
    """True inside the pre-close window (or outside the session entirely)."""
    s = session_for(symbol)
    if s.always_open:
        return False
    now = now or datetime.now(timezone.utc)
    if s.weekdays_only and now.weekday() >= 5:
        return True
    m = now.hour * 60 + now.minute
    return not (_minutes(s.open_utc) <= m < _minutes(s.close_utc) - s.flat_before_close_min)
