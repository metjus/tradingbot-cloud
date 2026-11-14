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
from .analysis.service import AnalysisService
from .api.server import create_app
from .config import Settings
from .core.events import CANDLE, CANDLE_PARTIAL, EventBus
from .db.database import Database
from .decision import DecisionEngine
from .marketdata.aggregator import CandleAggregator
from .marketdata.model import Candle, Timeframe
from .marketdata.providers import AlpacaProvider, KrakenProvider, PollingProvider
from .monitoring.health import HealthMonitor, Status
from .risk import RiskLimits, RiskSupervisor

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

        limits = RiskLimits(
            risk_per_trade_pct=settings.risk_per_trade_pct,
            daily_max_loss_pct=settings.daily_max_loss_pct,
            max_open_positions=settings.max_open_positions,
            max_daily_trades=settings.max_daily_trades,
        )
        self.risk = RiskSupervisor(limits)
        from .ml.registry import ModelRegistry
        self.registry = ModelRegistry(self.db, settings.data_dir / "models")
        ml_scorer = self.registry.load_active("setup-scorer")
        if ml_scorer is not None:
            log.info("loaded active ML model setup-scorer v%s", ml_scorer.meta.version)
        from .analysis.selector import StrategySelector
        self.selector = StrategySelector(audit=self.db.audit)
        self.decision_engine = DecisionEngine(self.risk, enter_threshold=settings.enter_threshold,
                                              ml_scorer=ml_scorer, selector=self.selector)

        from .news.calendar import EventCalendar
        from .news.llm import LLMNewsAnalyzer
        from .news.service import NewsService
        self.calendar = EventCalendar(self.db, settings.event_blackout_before_min,
                                      settings.event_blackout_after_min)
        self.news: NewsService | None = None
        if settings.news_enabled:
            llm = (LLMNewsAnalyzer(settings.anthropic_api_key,
                                   settings.llm_daily_budget_usd)
                   if settings.anthropic_api_key else None)
            tracked = settings.crypto_symbols + settings.stock_symbols
            self.news = NewsService(self.db, self.bus, self.health, tracked,
                                    poll_seconds=settings.news_poll_seconds,
                                    llm_analyzer=llm)

        # --- execution (phase 8: paper only; live arrives in phase 12/14) ---
        if settings.trading_mode != "paper":
            raise SystemExit(
                "trading_mode='live' is not available yet: live execution is "
                "intentionally locked until broker integration (phase 12) and "
                "explicit user enablement (phase 14). Use trading_mode='paper'.")
        from .trading.broker import PaperBroker
        from .trading.executor import ExecutionService
        from .trading.portfolio import Portfolio
        if settings.paper_broker == "alpaca_paper" and settings.alpaca_key_id:
            from .trading.alpaca import AlpacaBroker
            self.broker = AlpacaBroker(settings.alpaca_key_id,
                                       settings.alpaca_secret_key, paper=True)
        else:
            self.broker = PaperBroker()
        self.portfolio = Portfolio(self.db, broker_name=self.broker.name,
                                   mode="paper", start_equity=settings.paper_equity)
        self.executor = ExecutionService(self.db, self.bus, self.health,
                                         self.broker, self.portfolio, self.risk,
                                         trailing_enabled=settings.trailing_enabled,
                                         break_even_at_r=settings.break_even_at_r,
                                         trail_start_r=settings.trail_start_r,
                                         trail_distance_r=settings.trail_distance_r)

        from .ml.scheduler import TrainingScheduler
        tracked = settings.crypto_symbols + settings.stock_symbols
        self.ml_scheduler = TrainingScheduler(
            self.db, self.bus, self.health, self.registry, self.selector,
            symbols=tracked or ["BTC/USD"],
            train_hour_utc=settings.ml_train_hour_utc,
            history_days=settings.ml_history_days,
            enabled=settings.ml_train_enabled)

        self.analysis = AnalysisService(
            self.db, self.bus, self.health, self.decision_engine,
            settings.paper_equity,
            sentiment_tracker=self.news.tracker if self.news else None,
            calendar=self.calendar,
            account_provider=self.portfolio.account_state)

        from .cloud.sync import CloudSync
        self.cloud: CloudSync | None = None
        if settings.cloud_url and settings.cloud_token:
            self.cloud = CloudSync(self.db, self.bus, self.health,
                                   self.executor, self.portfolio, self.news,
                                   settings.cloud_url, settings.cloud_token,
                                   settings.cloud_push_seconds,
                                   settings.cloud_poll_seconds)

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
        self.analysis.start()
        self.executor.start()
        if hasattr(self.broker, "start"):
            self.broker.start()
        self.ml_scheduler.start()
        if self.news is not None:
            self.news.start()
        if self.cloud is not None:
            self.cloud.start()
        flush_task = asyncio.get_running_loop().create_task(self._flush_loop(), name="flush-loop")

        info = self.info()
        app = create_app(self.db, self.bus, self.health, info,
                         news=self.news, calendar=self.calendar,
                         executor=self.executor, portfolio=self.portfolio,
                         decision_engine=self.decision_engine,
                         settings=self.settings)
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
            self.analysis.stop()
            self.executor.stop()
            self.ml_scheduler.stop()
            if hasattr(self.broker, "stop"):
                await self.broker.stop()
            if self.cloud is not None:
                await self.cloud.stop()
            if self.news is not None:
                await self.news.stop()
            for p in self.providers:
                await p.stop()
            self.health.stop()
            self.db.audit("system", "Engine stopped")
            log.info("engine stopped")
