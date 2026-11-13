"""Engine orchestration: wires providers -> aggregator -> DB/event bus,
runs the health monitor and the local API server.

Phase 3 scope: market data pipeline + storage + monitoring + local API.
No trading logic lives here yet (phases 4+ plug into the same event bus).
"""

from __future__ import annotations

import asyncio
import logging
import time

import uvicorn

from . import __version__
from .api.server import create_app
from .config import Settings
from .core.events import CANDLE, CANDLE_PARTIAL, EventBus
from .db.database import Database
from .marketdata.aggregator import CandleAggregator
from .marketdata.model import Candle, Timeframe
from .marketdata.providers import AlpacaProvider, KrakenProvider, PollingProvider
from .monitoring.health import HealthMonitor, Status

log = logging.getLogger(__name__)


class Engine:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        self.bus = EventBus()
        self.db = Database(settings.db_path)
        self.health = HealthMonitor(self.bus)
        self.state = "starting"
        self.providers: list[PollingProvider] = []
        self.aggregator = CandleAggregator(
            on_close=self._on_closed_candle,
            on_partial=lambda c: self.bus.publish(CANDLE_PARTIAL, c),
        )
        self._server: uvicorn.Server | None = None

    # --- candle flow ------------------------------------------------------
    def _on_m1(self, bar: Candle) -> None:
        self.db.upsert_candles([bar])
        self.bus.publish(CANDLE, bar)
        self.aggregator.add_m1(bar)

    def _on_closed_candle(self, bar: Candle) -> None:
        self.db.upsert_candles([bar])
        self.bus.publish(CANDLE, bar)

    # --- lifecycle ----------------------------------------------------------
    def _build_providers(self) -> None:
        s = self.settings
        if s.crypto_symbols:
            if s.crypto_provider == "kraken":
                self.providers.append(KrakenProvider(
                    s.crypto_symbols, self._on_m1, self.health,
                    poll_seconds=s.candle_poll_seconds))
            else:
                raise ValueError(f"unsupported crypto provider: {s.crypto_provider}")
        if s.stock_symbols:
            if s.alpaca_key_id and s.alpaca_secret_key:
                self.providers.append(AlpacaProvider(
                    s.stock_symbols, self._on_m1, self.health,
                    key_id=s.alpaca_key_id, secret_key=s.alpaca_secret_key,
                    poll_seconds=max(15.0, s.candle_poll_seconds)))
            else:
                log.warning("stock symbols configured but Alpaca keys missing; skipping")
                self.db.audit("system", "Alpaca keys missing - stock data disabled", "warning")

    async def _backfill(self) -> None:
        for p in self.providers:
            for sym in p.symbols:
                try:
                    bars = await p.backfill(sym, minutes=720)
                    for b in bars:
                        self.db.upsert_candles([b])
                        self.aggregator.add_m1(b)
                    log.info("backfill %s %s: %d x 1m bars", p.name, sym, len(bars))
                except Exception as exc:
                    log.warning("backfill failed %s %s: %s", p.name, sym, exc)
                    self.db.audit("data", f"Backfill failed for {sym}: {exc}", "warning")

    async def _flush_loop(self) -> None:
        """Close idle higher-timeframe buckets in quiet markets."""
        while True:
            now = int(time.time())
            for p in self.providers:
                for sym in p.symbols:
                    self.aggregator.flush_before(sym, now)
            await asyncio.sleep(30)

    def info(self) -> dict:
        return {
            "mode": self.settings.trading_mode,
            "state": self.state,
            "provider": ",".join(p.name for p in self.providers),
            "symbols": [s for p in self.providers for s in p.symbols],
        }

    async def run(self) -> None:
        log.info("ARGUS engine %s starting (mode=%s)", __version__, self.settings.trading_mode)
        self.db.audit("system", f"Engine starting v{__version__}",
                      data={"mode": self.settings.trading_mode})
        self.health.register("engine", stale_after=3600)
        self.health.set_status("engine", Status.OK, "running")
        self.health.start()

        self._build_providers()
        await self._backfill()
        for p in self.providers:
            p.start()
        flush_task = asyncio.get_running_loop().create_task(self._flush_loop(), name="flush-loop")

        info = self.info()
        app = create_app(self.db, self.bus, self.health, info)
        config = uvicorn.Config(app, host=self.settings.api_host,
                                port=self.settings.api_port, log_level="warning")
        self._server = uvicorn.Server(config)
        self.state = info["state"] = "running"
        self.db.audit("system", "Engine running")
        try:
            await self._server.serve()
        finally:
            self.state = "stopping"
            flush_task.cancel()
            for p in self.providers:
                await p.stop()
            self.health.stop()
            self.db.audit("system", "Engine stopped")
            log.info("engine stopped")
