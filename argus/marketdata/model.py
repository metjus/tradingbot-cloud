"""Market data primitives: timeframes, ticks and candles.

All timestamps are UTC epoch seconds. A candle's ``ts`` is its OPEN time,
aligned to the timeframe boundary. ``complete`` is True only once the bar
is closed; decision logic in later phases must ignore incomplete bars to
avoid look-ahead bias.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"

    @property
    def seconds(self) -> int:
        return _TF_SECONDS[self]

    def align(self, ts: int) -> int:
        """Floor an epoch-seconds timestamp to this timeframe's boundary."""
        return ts - (ts % self.seconds)


_TF_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
}

# Higher timeframes are derived from 1m bars locally, so one REST/WS feed
# per symbol is enough for all five analysis timeframes.
DERIVED: list[Timeframe] = [Timeframe.M5, Timeframe.M15, Timeframe.H1, Timeframe.H4]


@dataclass(slots=True)
class Tick:
    symbol: str
    price: float
    volume: float
    ts: float  # epoch seconds, may be fractional
    source: str = ""


@dataclass(slots=True)
class Candle:
    symbol: str
    timeframe: Timeframe
    ts: int  # open time, epoch seconds, aligned
    open: float
    high: float
    low: float
    close: float
    volume: float
    source: str = ""
    complete: bool = True

    def merge_tick(self, price: float, volume: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume

    def copy(self) -> "Candle":
        return replace(self)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe.value,
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "source": self.source,
            "complete": self.complete,
        }
