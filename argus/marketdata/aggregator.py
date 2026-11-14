"""Aggregates a stream of closed 1m candles into higher timeframes.

The provider delivers (or the tick builder produces) closed 1m bars; this
class maintains the 5m/15m/1h/4h bars and emits each exactly once when it
closes. Out-of-order or duplicate 1m bars are ignored (logged), gaps are
tolerated: a bar closes as soon as a 1m bar belonging to the next bucket
arrives.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from .model import DERIVED, Candle, Timeframe

log = logging.getLogger(__name__)

EmitFn = Callable[[Candle], None]


class CandleAggregator:
    def __init__(self, on_close: EmitFn, on_partial: EmitFn | None = None) -> None:
        self._on_close = on_close
        self._on_partial = on_partial
        # (symbol, timeframe) -> open bucket
        self._open: dict[tuple[str, Timeframe], Candle] = {}
        # symbol -> last accepted 1m open time
        self._last_m1: dict[str, int] = {}

    def add_m1(self, bar: Candle) -> None:
        """Feed one CLOSED 1m candle."""
        if bar.timeframe is not Timeframe.M1 or not bar.complete:
            raise ValueError("aggregator expects closed 1m candles")
        last = self._last_m1.get(bar.symbol)
        if last is not None and bar.ts <= last:
            log.debug("ignoring duplicate/out-of-order 1m bar %s ts=%s", bar.symbol, bar.ts)
            return
        self._last_m1[bar.symbol] = bar.ts

        for tf in DERIVED:
            self._feed(tf, bar)

    def flush_before(self, symbol: str, now_ts: int) -> None:
        """Close any open bucket whose period has fully elapsed by ``now_ts``.
        Called periodically so quiet markets still close their bars."""
        for (sym, tf), bucket in list(self._open.items()):
            if sym == symbol and now_ts >= bucket.ts + tf.seconds:
                self._close((sym, tf))

    def _feed(self, tf: Timeframe, m1: Candle) -> None:
        key = (m1.symbol, tf)
        bucket_ts = tf.align(m1.ts)
        cur = self._open.get(key)

        if cur is not None and bucket_ts > cur.ts:
            self._close(key)
            cur = None

        if cur is None:
            self._open[key] = Candle(
                symbol=m1.symbol, timeframe=tf, ts=bucket_ts,
                open=m1.open, high=m1.high, low=m1.low, close=m1.close,
                volume=m1.volume, source=m1.source, complete=False,
            )
        else:
            cur.high = max(cur.high, m1.high)
            cur.low = min(cur.low, m1.low)
            cur.close = m1.close
            cur.volume += m1.volume

        # bar completed exactly at period end (last 1m slice present)
        bucket = self._open[key]
        if m1.ts + Timeframe.M1.seconds >= bucket.ts + tf.seconds:
            self._close(key)
        elif self._on_partial is not None:
            self._on_partial(bucket.copy())

    def _close(self, key: tuple[str, Timeframe]) -> None:
        bucket = self._open.pop(key, None)
        if bucket is None:
            return
        bucket.complete = True
        self._on_close(bucket)
