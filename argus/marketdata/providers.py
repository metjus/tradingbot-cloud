"""Market data providers.

Phase 3 ships a robust REST polling pipeline (1m bars, incremental,
deduplicated, with backoff and health reporting). Low-latency WebSocket
streams are an optimisation layered on top in a later phase - for 1m+
timeframe day trading a 10 s poll is functionally equivalent and far more
resilient to proxy/firewall environments.

Implemented:
  * KrakenProvider  - public REST, no key required (crypto, 24/7)
  * AlpacaProvider  - IEX feed REST, requires API key (US stocks; paper OK)

Every provider only ever emits CLOSED 1m candles, in ascending order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Callable

import httpx

from ..marketdata.model import Candle, Timeframe
from ..monitoring.health import HealthMonitor, Status

log = logging.getLogger(__name__)

EmitFn = Callable[[Candle], None]


class PollingProvider(ABC):
    """Polls 1m OHLC incrementally and emits new closed bars."""

    name: str = "provider"

    def __init__(self, symbols: list[str], emit: EmitFn, health: HealthMonitor,
                 poll_seconds: float = 10.0) -> None:
        self.symbols = symbols
        self._emit = emit
        self._health = health
        self._poll = poll_seconds
        self._last_ts: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=15.0)
        health.register(self.name, stale_after=max(90.0, poll_seconds * 6))

    # --- to implement per venue -----------------------------------------
    @abstractmethod
    async def fetch_m1(self, symbol: str, since_ts: int | None) -> list[Candle]:
        """Return closed 1m candles with ts > since_ts (ascending)."""

    # --- common loop ------------------------------------------------------
    async def backfill(self, symbol: str, minutes: int = 720) -> list[Candle]:
        since = int(time.time()) - minutes * 60
        bars = await self.fetch_m1(symbol, since)
        if bars:
            self._last_ts[symbol] = bars[-1].ts
        return bars

    async def run(self) -> None:
        errors = 0
        while True:
            try:
                for symbol in self.symbols:
                    bars = await self.fetch_m1(symbol, self._last_ts.get(symbol))
                    for bar in bars:
                        last = self._last_ts.get(symbol)
                        if last is not None and bar.ts <= last:
                            continue
                        self._last_ts[symbol] = bar.ts
                        self._emit(bar)
                errors = 0
                self._health.heartbeat(self.name, Status.OK,
                                       f"{len(self.symbols)} symbols, poll {self._poll}s")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network / venue errors: degrade, back off
                errors += 1
                self._health.set_status(self.name, Status.DEGRADED, f"{type(exc).__name__}: {exc}")
                log.warning("%s poll error (%d): %s", self.name, errors, exc)
                await asyncio.sleep(min(60.0, 2.0 ** errors))
                continue
            await asyncio.sleep(self._poll)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run(), name=f"provider-{self.name}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self._client.aclose()


class KrakenProvider(PollingProvider):
    """Kraken public OHLC. Symbols use 'BTC/USD' form; mapped to venue pairs."""

    name = "kraken"
    BASE = "https://api.kraken.com/0/public"
    _PAIR_MAP = {"BTC": "XBT"}  # Kraken's historical naming

    def _pair(self, symbol: str) -> str:
        base, quote = symbol.split("/")
        return f"{self._PAIR_MAP.get(base, base)}{quote}"

    async def fetch_m1(self, symbol: str, since_ts: int | None) -> list[Candle]:
        params: dict = {"pair": self._pair(symbol), "interval": 1}
        if since_ts is not None:
            params["since"] = since_ts
        r = await self._client.get(f"{self.BASE}/OHLC", params=params)
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError(f"kraken error: {payload['error']}")
        result = payload["result"]
        series_key = next(k for k in result if k != "last")
        rows = result[series_key]
        now = int(time.time())
        out: list[Candle] = []
        for row in rows:
            ts = int(row[0])
            if ts + 60 > now:   # bar still forming -> not closed yet
                continue
            if since_ts is not None and ts <= since_ts:
                continue
            out.append(Candle(
                symbol=symbol, timeframe=Timeframe.M1, ts=ts,
                open=float(row[1]), high=float(row[2]), low=float(row[3]),
                close=float(row[4]), volume=float(row[6]),
                source=self.name, complete=True,
            ))
        out.sort(key=lambda c: c.ts)
        return out


class AlpacaProvider(PollingProvider):
    """Alpaca market data v2 (IEX feed on the free tier). Requires API keys;
    works for paper and live accounts alike. US stock symbols like 'NVDA'."""

    name = "alpaca"
    BASE = "https://data.alpaca.markets/v2"

    def __init__(self, symbols: list[str], emit: EmitFn, health: HealthMonitor,
                 key_id: str, secret_key: str, poll_seconds: float = 15.0) -> None:
        super().__init__(symbols, emit, health, poll_seconds)
        self._client.headers.update({
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        })

    async def fetch_m1(self, symbol: str, since_ts: int | None) -> list[Candle]:
        from datetime import datetime, timezone
        params: dict = {"timeframe": "1Min", "limit": 1000, "feed": "iex", "adjustment": "raw"}
        if since_ts is not None:
            params["start"] = datetime.fromtimestamp(since_ts + 1, tz=timezone.utc).isoformat()
        r = await self._client.get(f"{self.BASE}/stocks/{symbol}/bars", params=params)
        r.raise_for_status()
        bars = r.json().get("bars") or []
        now = int(time.time())
        out: list[Candle] = []
        for b in bars:
            ts = int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp())
            if ts + 60 > now:
                continue
            if since_ts is not None and ts <= since_ts:
                continue
            out.append(Candle(
                symbol=symbol, timeframe=Timeframe.M1, ts=ts,
                open=float(b["o"]), high=float(b["h"]), low=float(b["l"]),
                close=float(b["c"]), volume=float(b["v"]),
                source=self.name, complete=True,
            ))
        out.sort(key=lambda c: c.ts)
        return out


class KrakenTicker:
    """Real-time last price + bid/ask for all symbols in ONE request every
    ``interval`` seconds (Kraken public Ticker endpoint, no key). Drives the
    live forming candle, mark-to-market and spread checks.

    Kraken returns results under its internal pair keys (e.g. XXBTZUSD), so
    the key -> symbol map is resolved once via AssetPairs.
    """

    name = "kraken_ticker"
    BASE = "https://api.kraken.com/0/public"

    def __init__(self, symbols: list[str], emit, health: HealthMonitor,
                 interval: float = 2.0) -> None:
        self.symbols = symbols
        self._emit = emit
        self._health = health
        self._interval = interval
        self._client = httpx.AsyncClient(timeout=10.0)
        self._key_to_symbol: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        health.register(self.name, stale_after=max(60.0, interval * 15))

    @staticmethod
    def _altname(symbol: str) -> str:
        base, quote = symbol.split("/")
        return f"{KrakenProvider._PAIR_MAP.get(base, base)}{quote}"

    async def _resolve(self) -> None:
        alts = {self._altname(s): s for s in self.symbols}
        r = await self._client.get(f"{self.BASE}/AssetPairs",
                                   params={"pair": ",".join(alts)})
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError(f"kraken AssetPairs: {payload['error']}")
        for key, info in payload["result"].items():
            sym = alts.get(info.get("altname", ""))
            if sym:
                self._key_to_symbol[key] = sym

    async def poll_once(self) -> list:
        from .model import Tick
        if not self._key_to_symbol:
            await self._resolve()
        r = await self._client.get(f"{self.BASE}/Ticker",
                                   params={"pair": ",".join(self._altname(s) for s in self.symbols)})
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError(f"kraken Ticker: {payload['error']}")
        now = time.time()
        ticks = []
        for key, v in payload["result"].items():
            sym = self._key_to_symbol.get(key)
            if sym is None:
                continue
            ticks.append(Tick(symbol=sym, price=float(v["c"][0]), volume=float(v["c"][1]),
                              ts=now, source="kraken", bid=float(v["b"][0]),
                              ask=float(v["a"][0])))
        return ticks

    async def run(self) -> None:
        errors = 0
        while True:
            try:
                for tick in await self.poll_once():
                    self._emit(tick)
                errors = 0
                self._health.heartbeat(self.name, Status.OK,
                                       f"{len(self.symbols)} symbolov, každé {self._interval:g}s")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors += 1
                self._health.set_status(self.name, Status.DEGRADED, f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(min(30.0, 2.0 ** errors))
                continue
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run(), name="kraken-ticker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self._client.aclose()
