"""Historical OHLCV download for backtesting and ML training.

Bitstamp public API: 5m candles, max 1000 per call, paginated by start
timestamp - no API key required. Data is cached into the local candles
table under source 'bitstamp' so repeated runs do not re-download.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from ..db.database import Database
from ..marketdata.model import Candle, Timeframe

log = logging.getLogger(__name__)

BASE = "https://www.bitstamp.net/api/v2"
_SYMBOL_MAP = {"BTC/USD": "btcusd", "ETH/USD": "ethusd", "BTC/EUR": "btceur",
               "ETH/EUR": "etheur", "SOL/USD": "solusd", "XRP/USD": "xrpusd"}


def to_pair(symbol: str) -> str:
    try:
        return _SYMBOL_MAP[symbol]
    except KeyError:
        return symbol.replace("/", "").lower()


async def fetch_history_5m(symbol: str, days: int,
                           client: httpx.AsyncClient | None = None) -> list[Candle]:
    """Download ``days`` of closed 5m candles, ascending."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    step = Timeframe.M5.seconds
    start = int(time.time()) - days * 86400
    end = int(time.time()) - step  # exclude the forming bar
    out: list[Candle] = []
    try:
        cursor = start
        while cursor < end:
            r = await client.get(f"{BASE}/ohlc/{to_pair(symbol)}/",
                                 params={"step": step, "limit": 1000, "start": cursor})
            r.raise_for_status()
            rows = r.json().get("data", {}).get("ohlc", [])
            if not rows:
                break
            batch = []
            for row in rows:
                ts = int(row["timestamp"])
                if ts <= cursor - step or ts + step > int(time.time()):
                    continue
                batch.append(Candle(
                    symbol=symbol, timeframe=Timeframe.M5, ts=ts,
                    open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]),
                    volume=float(row["volume"]), source="bitstamp", complete=True))
            if not batch:
                break
            out.extend(b for b in batch if not out or b.ts > out[-1].ts)
            new_cursor = batch[-1].ts + step
            if new_cursor <= cursor:
                break
            cursor = new_cursor
            await asyncio.sleep(0.15)  # polite pacing
    finally:
        if own:
            await client.aclose()
    log.info("history %s: %d x 5m bars (%.1f days)", symbol, len(out), len(out) * step / 86400)
    return out


async def ensure_history(db: Database, symbol: str, days: int) -> list[Candle]:
    """Return ``days`` of 5m candles, downloading only what is missing."""
    want_from = int(time.time()) - days * 86400
    have = db.get_candles(symbol, Timeframe.M5, limit=days * 288 + 10)
    have = [c for c in have if c.ts >= want_from]
    span_ok = have and have[0].ts <= want_from + 2 * 86400
    fresh_ok = have and have[-1].ts >= int(time.time()) - 3600
    if span_ok and fresh_ok and len(have) > days * 288 * 0.7:
        return have
    bars = await fetch_history_5m(symbol, days)
    if bars:
        db.upsert_candles(bars)
    return db.get_candles(symbol, Timeframe.M5, limit=days * 288 + 10)


def resample(candles: list[Candle], target: Timeframe) -> list[Candle]:
    """Resample closed 5m candles into a higher timeframe (closed buckets only)."""
    out: list[Candle] = []
    cur: Candle | None = None
    for c in candles:
        bucket_ts = target.align(c.ts)
        if cur is not None and bucket_ts > cur.ts:
            cur.complete = True
            out.append(cur)
            cur = None
        if cur is None:
            cur = Candle(symbol=c.symbol, timeframe=target, ts=bucket_ts,
                         open=c.open, high=c.high, low=c.low, close=c.close,
                         volume=c.volume, source=c.source, complete=False)
        else:
            cur.high = max(cur.high, c.high)
            cur.low = min(cur.low, c.low)
            cur.close = c.close
            cur.volume += c.volume
        if c.ts + c.timeframe.seconds >= cur.ts + target.seconds:
            cur.complete = True
            out.append(cur)
            cur = None
    return out


async def ensure_history_futures(db: Database, symbol: str, days: int) -> list[Candle]:
    """1h Kraken Futures candles for backtests/ML, downloaded only when the
    local cache is thin or stale."""
    from ..marketdata.kraken_futures import fetch_history
    want_from = int(time.time()) - days * 86400
    have = db.get_candles(symbol, Timeframe.H1, limit=days * 24 + 10)
    have = [c for c in have if c.ts >= want_from]
    span_ok = have and have[0].ts <= want_from + 3 * 86400
    fresh_ok = have and have[-1].ts >= int(time.time()) - 6 * 3600
    if span_ok and fresh_ok and len(have) > days * 24 * 0.7:
        return have
    bars = await fetch_history(symbol, days, "1h")
    if bars:
        db.upsert_candles(bars)
    return db.get_candles(symbol, Timeframe.H1, limit=days * 24 + 10)
