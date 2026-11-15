"""Kraken Futures market data (perpetual contracts, PF_*USD).

Why Kraken Futures: EU-regulated venue (MiFID, Cyprus entity) that still
serves Slovak retail accounts, fees comparable to Binance Futures
(0.02 % maker / 0.05 % taker at tier 1) and a public API with 1m candles,
tickers (bid/ask, 24h volume in coin AND USD, funding, mark price) and
historical funding rates - no key needed for market data.

Symbols keep the UI form "BTC/USD"; the venue form is "PF_XBTUSD".

  * KrakenFuturesProvider - closed 1m candles via the charts REST API
  * KrakenFuturesTicker   - real-time prices over WebSocket (feed "ticker"),
                            with a REST fallback poll when the socket drops;
                            keeps per-symbol market info (volume, funding,
                            mark, open interest) for the UI and the broker
  * fetch_universe        - liquid perpetuals sorted by 24h USD volume
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time

import httpx

from ..monitoring.health import HealthMonitor, Status
from .model import Candle, Tick, Timeframe
from .providers import PollingProvider

log = logging.getLogger(__name__)

BASE = "https://futures.kraken.com"
WS_URL = "wss://futures.kraken.com/ws/v1"

_BASE_MAP = {"BTC": "XBT"}                     # UI base -> venue base
_BASE_UNMAP = {v: k for k, v in _BASE_MAP.items()}

#: coins never auto-selected (meme/manipulation-prone); user can still add
#: them by hand in Settings
MEME_DENYLIST = {"PEPE", "PENGU", "PUMP", "BONK", "WIF", "SHIB", "FLOKI",
                 "TRUMP", "MEME", "MOG", "POPCAT", "FARTCOIN", "DOGS"}
#: non-crypto underlyings (tokenised stocks/metals) are out of scope
NON_CRYPTO = {"XAUT", "XAU", "XAG", "NVDAX", "CRCLX", "AAPLX", "TSLAX",
              "METAX", "MSTRX", "SPYX", "QQQX", "GOOGLX", "AMZNX", "COINX",
              "HOODX", "CRCL"}


def to_pf(symbol: str) -> str:
    """'BTC/USD' -> 'PF_XBTUSD'."""
    base, _quote = symbol.split("/")
    return f"PF_{_BASE_MAP.get(base, base)}USD"


def from_pf(pf: str) -> str:
    """'PF_XBTUSD' -> 'BTC/USD'."""
    base = pf.removeprefix("PF_").removesuffix("USD")
    return f"{_BASE_UNMAP.get(base, base)}/USD"


def _ssl_context() -> ssl.SSLContext:
    import os
    cafile = os.environ.get("SSL_CERT_FILE")
    for cand in (cafile, "/root/.ccr/ca-bundle.crt"):
        if cand:
            try:
                return ssl.create_default_context(cafile=cand)
            except (FileNotFoundError, ssl.SSLError):
                continue
    return ssl.create_default_context()


class KrakenFuturesProvider(PollingProvider):
    """Closed 1m candles from the public charts API."""

    name = "kraken_futures"

    async def fetch_m1(self, symbol: str, since_ts: int | None) -> list[Candle]:
        now = int(time.time())
        frm = (since_ts + 1) if since_ts is not None else now - 7200
        r = await self._client.get(
            f"{BASE}/api/charts/v1/trade/{to_pf(symbol)}/1m", params={"from": frm})
        r.raise_for_status()
        rows = r.json().get("candles") or []
        out: list[Candle] = []
        for row in rows:
            ts = int(row["time"]) // 1000
            if ts + 60 > now:          # forming bar
                continue
            if since_ts is not None and ts <= since_ts:
                continue
            out.append(Candle(
                symbol=symbol, timeframe=Timeframe.M1, ts=ts,
                open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]),
                volume=float(row["volume"]), source=self.name, complete=True))
        out.sort(key=lambda c: c.ts)
        return out


class KrakenFuturesTicker:
    """Real-time ticker: WebSocket feed with REST fallback.

    Emits Tick objects (price, bid, ask) and keeps ``info[symbol]`` with
    volume (coin + USD), funding, mark price and open interest.
    """

    name = "kf_ticker"

    def __init__(self, symbols: list[str], emit, health: HealthMonitor,
                 interval: float = 2.0) -> None:
        self.symbols = symbols
        self._emit = emit
        self._health = health
        self._interval = interval
        self._client = httpx.AsyncClient(timeout=10.0)
        self._task: asyncio.Task | None = None
        #: symbol -> dict(vol24h, volume_quote, funding_rate_h, funding_pred_h,
        #:                next_funding_ts, mark, open_interest, bid, ask, last)
        self.info: dict[str, dict] = {}
        health.register(self.name, stale_after=max(60.0, interval * 15))

    # --- shared ----------------------------------------------------------
    def _handle(self, pf: str, m: dict, now: float) -> None:
        sym = from_pf(pf)
        if sym not in self.symbols:
            return
        last = float(m.get("last") or 0)
        bid = float(m.get("bid") or 0) or None
        ask = float(m.get("ask") or 0) or None
        if not last and bid and ask:
            last = (bid + ask) / 2
        if not last:
            return
        self.info[sym] = {
            "vol24h": float(m.get("volume") or 0),
            "volume_quote": float(m.get("volumeQuote") or 0),
            "funding_rate_h": float(m.get("relative_funding_rate")
                                    or m.get("relativeFundingRate") or 0),
            "funding_pred_h": float(m.get("relative_funding_rate_prediction")
                                    or m.get("relativeFundingRatePrediction") or 0),
            "next_funding_ts": int(m.get("next_funding_rate_time") or 0) // 1000,
            "mark": float(m.get("markPrice") or 0) or None,
            "open_interest": float(m.get("openInterest") or 0),
            "bid": bid, "ask": ask, "last": last, "ts": now,
        }
        self._emit(Tick(symbol=sym, price=last, volume=0.0, ts=now,
                        source="kraken_futures", bid=bid, ask=ask))

    # --- websocket -------------------------------------------------------
    async def _run_ws(self) -> None:
        import websockets
        async with websockets.connect(WS_URL, ssl=_ssl_context(),
                                      open_timeout=15, ping_interval=20) as ws:
            await ws.send(json.dumps({
                "event": "subscribe", "feed": "ticker",
                "product_ids": [to_pf(s) for s in self.symbols]}))
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
                if m.get("feed") == "ticker" and m.get("product_id"):
                    self._handle(m["product_id"], m, time.time())
                    self._health.heartbeat(self.name, Status.OK,
                                           f"{len(self.symbols)} mincí, websocket")

    # --- REST fallback ---------------------------------------------------
    async def poll_once(self) -> None:
        r = await self._client.get(f"{BASE}/derivatives/api/v3/tickers")
        r.raise_for_status()
        now = time.time()
        wanted = {to_pf(s) for s in self.symbols}
        for t in r.json().get("tickers", []):
            if t.get("symbol") in wanted:
                t.setdefault("relative_funding_rate", t.get("relativeFundingRate"))
                self._handle(t["symbol"], t, now)

    async def run(self) -> None:
        errors = 0
        while True:
            try:
                await self._run_ws()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                errors += 1
                self._health.set_status(self.name, Status.DEGRADED,
                                        f"websocket: {type(exc).__name__}")
                log.warning("futures ws failed (%d): %s; REST fallback", errors, exc)
            # REST fallback for a while, then try the socket again
            for _ in range(15):
                try:
                    await self.poll_once()
                    self._health.heartbeat(self.name, Status.OK,
                                           f"{len(self.symbols)} mincí, REST záloha")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._health.set_status(self.name, Status.DEGRADED,
                                            f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(self._interval)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run(), name="kf-ticker")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self._client.aclose()


async def fetch_universe(min_volume_usd: float = 5_000_000.0,
                         limit: int = 12,
                         client: httpx.AsyncClient | None = None) -> list[dict]:
    """Liquid crypto perpetuals by 24h USD volume (for automatic coin
    selection). Returns [{symbol, pf, volume_quote, vol24h, last}] sorted
    by USD volume, meme coins and tokenised stocks excluded."""
    own = client is None
    client = client or httpx.AsyncClient(timeout=15.0)
    try:
        r = await client.get(f"{BASE}/derivatives/api/v3/tickers")
        r.raise_for_status()
        out = []
        for t in r.json().get("tickers", []):
            pf = t.get("symbol", "")
            if not pf.startswith("PF_") or t.get("suspended"):
                continue
            base = pf.removeprefix("PF_").removesuffix("USD")
            ui_base = _BASE_UNMAP.get(base, base)
            if ui_base in MEME_DENYLIST or ui_base in NON_CRYPTO or base in NON_CRYPTO:
                continue
            vq = float(t.get("volumeQuote") or 0)
            if vq < min_volume_usd:
                continue
            bid, ask = float(t.get("bid") or 0), float(t.get("ask") or 0)
            if bid <= 0 or ask <= 0 or (ask - bid) / ((ask + bid) / 2) > 0.002:
                continue                        # illiquid or too wide
            out.append({"symbol": from_pf(pf), "pf": pf, "volume_quote": vq,
                        "vol24h": float(t.get("vol24h") or 0), "last": float(t.get("last") or 0)})
        out.sort(key=lambda x: -x["volume_quote"])
        return out[:limit]
    finally:
        if own:
            await client.aclose()


async def fetch_history(symbol: str, days: int, resolution: str = "1h",
                        client: httpx.AsyncClient | None = None) -> list[Candle]:
    """Historical closed candles from the charts API (for backtests/ML).
    resolution: 15m | 1h | 4h."""
    tf = {"15m": Timeframe.M15, "1h": Timeframe.H1, "4h": Timeframe.H4}[resolution]
    own = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    out: list[Candle] = []
    try:
        cur = int(time.time()) - days * 86400
        now = int(time.time())
        while True:
            r = await client.get(f"{BASE}/api/charts/v1/trade/{to_pf(symbol)}/{resolution}",
                                 params={"from": cur})
            r.raise_for_status()
            d = r.json()
            rows = d.get("candles") or []
            if not rows:
                break
            for row in rows:
                ts = int(row["time"]) // 1000
                if ts + tf.seconds > now:
                    continue
                if out and ts <= out[-1].ts:
                    continue
                out.append(Candle(symbol=symbol, timeframe=tf, ts=ts,
                                  open=float(row["open"]), high=float(row["high"]),
                                  low=float(row["low"]), close=float(row["close"]),
                                  volume=float(row["volume"]),
                                  source="kraken_futures", complete=True))
            if not d.get("more_candles"):
                break
            nxt = int(rows[-1]["time"]) // 1000 + 1
            if nxt <= cur:
                break
            cur = nxt
            await asyncio.sleep(0.2)
    finally:
        if own:
            await client.aclose()
    log.info("futures history %s %s: %d bars", symbol, resolution, len(out))
    return out
