"""Engine orchestration: market data (candles + real-time ticks) ->
aggregation -> analysis/decisions -> execution, plus monitoring, news,
ML scheduling, cloud sync and the local API for the desktop terminal.

User-editable settings (RuntimeSettings) are applied live to the risk
supervisor, decision engine, exit manager and paper broker.
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
from .core.events import CANDLE, CANDLE_PARTIAL, TICK, EventBus
from .db.database import Database
from .decision import DecisionEngine
from .marketdata.aggregator import CandleAggregator
from .marketdata.model import Candle, Tick, Timeframe
from .marketdata.kraken_futures import (KrakenFuturesProvider, KrakenFuturesTicker,
                                        fetch_universe)
from .marketdata.providers import (AlpacaProvider, KrakenProvider, KrakenTicker,
                                   PollingProvider)
from .monitoring.health import HealthMonitor, Status
from .fees import round_trip_cost_pct, venue
from .risk import RiskLimits, RiskSupervisor
from .runtime_settings import RuntimeSettings

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
        self.runtime = RuntimeSettings(self.db)
        # explicit config/env wins (tests, power users); otherwise the
        # symbol list edited in the UI is used
        self.crypto_symbols: list[str] = (
            list(settings.crypto_symbols) if "crypto_symbols" in settings.model_fields_set
            else list(self.runtime["symbols"]))
        self.ticker: KrakenTicker | KrakenFuturesTicker | None = None
        self.universe: list[dict] = []     # auto-selected coins (futures)
        self.last_tick: dict[str, Tick] = {}
        self.forming_m1: dict[str, Candle] = {}

        self.risk = RiskSupervisor(RiskLimits())
        from .ml.registry import ModelRegistry
        self.registry = ModelRegistry(self.db, settings.data_dir / "models")
        ml_scorer = self.registry.load_active("setup-scorer")
        if ml_scorer is not None:
            log.info("loaded active ML model setup-scorer v%s", ml_scorer.meta.version)
        from .analysis.selector import StrategySelector
        self.selector = StrategySelector(audit=self.db.audit)
        strategies = None
        if venue(self.runtime["fee_venue"]).futures:
            from .strategy.futures import FUTURES_STRATEGIES
            strategies = list(FUTURES_STRATEGIES)
        self.decision_engine = DecisionEngine(self.risk, strategies=strategies,
                                              enter_threshold=self.runtime["enter_threshold"],
                                              ml_scorer=ml_scorer, selector=self.selector)

        from .news.calendar import EventCalendar
        from .news.llm import LLMRouter
        from .news.service import NewsService
        from .secrets import SecretStore
        self.secrets = SecretStore(settings.data_dir)
        if settings.anthropic_api_key and not self.secrets.get("anthropic_api_key"):
            self.secrets.set("anthropic_api_key", settings.anthropic_api_key)  # one-time import
        self.llm = LLMRouter(self.runtime, self.secrets)
        self.calendar = EventCalendar(self.db, settings.event_blackout_before_min,
                                      settings.event_blackout_after_min)
        self.news: NewsService | None = None
        if settings.news_enabled:
            tracked = self.crypto_symbols + settings.stock_symbols
            self.news = NewsService(self.db, self.bus, self.health, tracked,
                                    poll_seconds=settings.news_poll_seconds,
                                    llm_analyzer=self.llm)

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
                                         self.broker, self.portfolio, self.risk)
        self.executor.restore_state()
        if isinstance(self.broker, PaperBroker):
            for pos in self.portfolio.positions.values():
                self.broker.restore(pos.client_order_id, pos.symbol, pos.direction,
                                    pos.quantity, pos.entry_price, pos.stop_loss,
                                    pos.take_profit, pos.opened_at.timestamp(), pos.strategy,
                                    leverage=pos.leverage)
            if self.portfolio.positions:
                self.db.audit("trade", f"Obnovené otvorené paper pozície po reštarte: "
                              f"{len(self.portfolio.positions)}")

        from .ml.scheduler import TrainingScheduler
        tracked = self.crypto_symbols + settings.stock_symbols
        # 4h futures strategies trade rarely: calibration needs a year of 1h
        hist_days = settings.ml_history_days
        if venue(self.runtime["fee_venue"]).futures:
            hist_days = max(hist_days, 365)
        self.ml_scheduler = TrainingScheduler(
            self.db, self.bus, self.health, self.registry, self.selector,
            symbols=tracked or ["BTC/USD"],
            train_hour_utc=settings.ml_train_hour_utc,
            history_days=hist_days,
            enabled=settings.ml_train_enabled, runtime=self.runtime)
        self.ml_scheduler.on_model_change = self.reload_model

        self.analysis = AnalysisService(
            self.db, self.bus, self.health, self.decision_engine,
            settings.paper_equity,
            sentiment_tracker=self.news.tracker if self.news else None,
            calendar=self.calendar,
            account_provider=self.portfolio.account_state,
            spread_provider=self._spread_for)
        self.executor.sentiment_provider = (
            self.news.tracker.score_for if self.news is not None else None)
        self.executor.regime_provider = self.analysis.last_regime.get
        self.executor.vol_fade_provider = self.analysis.last_vol_fade.get

        self.apply_settings()
        self.runtime.on_change(lambda _changed: self.apply_settings())

        from .cloud.sync import CloudSync
        self.cloud: CloudSync | None = None
        url = settings.cloud_url or self.secrets.get("cloud_url")
        token = settings.cloud_token or self.secrets.get("cloud_engine_token")
        if url and token:
            self.cloud = self._make_cloud(url, token)

    # --- cloud / mobile pairing ---------------------------------------------
    def _make_cloud(self, url: str, token: str):
        from .cloud.sync import CloudSync
        return CloudSync(self.db, self.bus, self.health, self.executor, self.portfolio,
                         self.news, url, token, self.settings.cloud_push_seconds,
                         self.settings.cloud_poll_seconds,
                         market_provider=self._market_compact)

    async def pair_cloud(self, url: str, pairing_code: str = "") -> dict:
        """Claim a freshly deployed ARGUS Worker and start syncing to it."""
        import httpx
        url = url.strip().rstrip("/")
        if not url.startswith("https://") and not url.startswith("http://127.0.0.1"):
            raise ValueError("adresa musí začínať https://")
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(f"{url}/setup/claim", json={"pairing_code": pairing_code})
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or not data.get("ok"):
            raise ValueError(data.get("message") or data.get("error") or f"HTTP {r.status_code}")
        self.secrets.set("cloud_url", url)
        self.secrets.set("cloud_engine_token", data["engine_token"])
        self.secrets.set("cloud_dashboard_token", data["dashboard_token"])
        await self._restart_cloud(url, data["engine_token"])
        self.db.audit("security", f"Mobilná aplikácia spárovaná s {url}")
        return {"url": url}

    async def unpair_cloud(self) -> None:
        import httpx
        url = self.secrets.get("cloud_url")
        token = self.secrets.get("cloud_engine_token")
        if url and token:
            try:
                async with httpx.AsyncClient(timeout=15.0) as c:
                    await c.post(f"{url}/setup/reset",
                                 headers={"Authorization": f"Bearer {token}"})
            except Exception as exc:  # unpair locally even if the Worker is unreachable
                log.warning("cloud reset failed: %s", exc)
        if self.cloud is not None:
            await self.cloud.stop()
            self.cloud = None
        for k in ("cloud_url", "cloud_engine_token", "cloud_dashboard_token"):
            self.secrets.delete(k)
        self.db.audit("security", "Párovanie s mobilnou aplikáciou zrušené")

    async def rotate_dashboard_token(self) -> str:
        import httpx
        url = self.secrets.get("cloud_url")
        token = self.secrets.get("cloud_engine_token")
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{url}/setup/rotate-dashboard",
                             headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        new = r.json()["dashboard_token"]
        self.secrets.set("cloud_dashboard_token", new)
        self.db.audit("security", "Nový párovací kód pre mobil (starý prestal platiť)")
        return new

    async def create_pairing_pin(self) -> dict:
        import httpx
        url = self.secrets.get("cloud_url")
        token = self.secrets.get("cloud_engine_token")
        dash = self.secrets.get("cloud_dashboard_token")
        if not (url and token and dash):
            raise ValueError("nie je spárované")
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.post(f"{url}/setup/pin", json={"dashboard_token": dash},
                             headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()

    async def _restart_cloud(self, url: str, token: str) -> None:
        if self.cloud is not None:
            await self.cloud.stop()
        self.cloud = self._make_cloud(url, token)
        self.cloud.start()

    def cloud_status(self) -> dict:
        url = self.secrets.get("cloud_url") or self.settings.cloud_url or None
        dash = self.secrets.get("cloud_dashboard_token")
        comp = next((c for c in self.health.snapshot() if c["name"] == "cloud_sync"), None)
        return {"paired": bool(url and (self.secrets.get("cloud_engine_token")
                                        or self.settings.cloud_token)),
                "url": url,
                "pair_link": f"{url}/#pair={dash}" if url and dash else None,
                "sync": comp,
                "last_sync_ts": self.cloud.last_sync_ts if self.cloud else None,
                "phones": (self.cloud.remote.get("phones") or []) if self.cloud else [],
                "pin_redeemed_ts": (self.cloud.remote.get("pin_redeemed_ts") or 0)
                if self.cloud else 0,
                "server_ts": self.cloud.remote.get("server_ts") if self.cloud else None}

    def _market_compact(self) -> dict:
        """Small per-coin market summary for the phone."""
        out = {}
        info = self.ticker.info if self.ticker is not None             and hasattr(self.ticker, "info") else {}
        for sym in self.crypto_symbols:
            m = info.get(sym)
            if m:
                out[sym] = {"last": m["last"], "vq": round(m["volume_quote"]),
                            "v": m["vol24h"], "f": m["funding_rate_h"]}
        return {"futures": venue(self.runtime["fee_venue"]).futures, "coins": out}

    def market_info(self) -> dict:
        """Per-coin market info for the UI: 24h volume (coin + USD),
        funding, mark price, open interest."""
        info = dict(self.ticker.info) if self.ticker is not None             and hasattr(self.ticker, "info") else {}
        return {"venue": self.runtime["fee_venue"],
                "futures": venue(self.runtime["fee_venue"]).futures,
                "symbols": self.crypto_symbols,
                "auto": bool(self.universe),
                "info": info}

    def reload_model(self) -> None:
        """Hot-swap the active ML model into the decision engine."""
        scorer = self.registry.load_active("setup-scorer")
        self.decision_engine.ml_scorer = scorer
        log.info("ML model reloaded: %s", f"v{scorer.meta.version}" if scorer else "none")

    # --- settings -----------------------------------------------------------
    def apply_settings(self) -> None:
        rs = self.runtime
        cost = round_trip_cost_pct(rs["fee_venue"], 
                                   "maker" if rs["entry_order"] == "limit" else "taker",
                                   rs["slippage_pct"])
        L = self.risk.limits
        L.risk_per_trade_pct = rs["risk_per_trade_pct"]
        L.daily_max_loss_pct = rs["daily_max_loss_pct"]
        L.max_open_positions = rs["max_open_positions"]
        L.max_daily_trades = rs["max_daily_trades"]
        L.min_rr = rs["min_rr_net"]
        L.budget_eur = rs["budget_eur"]
        L.max_position_eur = rs["max_position_eur"]
        L.round_trip_cost_pct = cost
        v = venue(rs["fee_venue"])
        L.allow_short = rs["allow_short"] and (v.futures or v.can_short_spot)
        L.leverage = rs["leverage"] if v.futures else 1.0
        L.max_leverage = v.max_leverage if v.futures else 1.0
        L.maintenance_margin_pct = v.maintenance_margin_pct if v.futures else 0.0
        L.max_aligned_positions = max(2, round(rs["max_open_positions"] * 0.6))
        self.decision_engine.enter_threshold = rs["enter_threshold"]
        self.executor.entry_order = rs["entry_order"]
        ep = self.executor.exit_params
        ep.enabled = rs["adaptive_exits"]
        ep.reduce_risk_at_r = rs["reduce_risk_at_r"]
        ep.break_even_at_r = rs["break_even_at_r"]
        ep.trail_start_r = rs["trail_start_r"]
        ep.trail_distance_r = rs["trail_distance_r"]
        ep.giveback_pct = rs["giveback_pct"]
        ep.giveback_min_r = rs["giveback_min_r"]
        ep.volume_exit = rs["volume_exit"]
        ep.volume_exit_min_r = rs["volume_exit_min_r"]
        ep.sentiment_exit = rs["sentiment_exit"]
        ep.regime_exit = rs["regime_exit"]
        ep.round_trip_cost_pct = cost
        if hasattr(self.broker, "fee_pct"):
            self.broker.fee_pct = v.taker_pct
            self.broker.maker_fee_pct = v.maker_pct
            self.broker.slippage_pct = rs["slippage_pct"]
            self.broker.maintenance_margin_pct = (
                v.maintenance_margin_pct if v.futures else 0.0)
        log.info("settings applied: venue %s, round-trip cost %.3f%%, budget %.0f",
                 rs["fee_venue"], cost, rs["budget_eur"])

    # --- real-time ticks & forming candles -----------------------------------
    def _spread_for(self, symbol: str) -> float | None:
        tick = self.last_tick.get(symbol)
        if tick is None or time.time() - tick.ts > 30:
            return None
        return tick.spread_pct

    def _on_tick(self, tick: Tick) -> None:
        self.last_tick[tick.symbol] = tick
        bucket = Timeframe.M1.align(int(tick.ts))
        bar = self.forming_m1.get(tick.symbol)
        if bar is None or bar.ts != bucket:
            self.forming_m1[tick.symbol] = Candle(
                symbol=tick.symbol, timeframe=Timeframe.M1, ts=bucket,
                open=tick.price, high=tick.price, low=tick.price, close=tick.price,
                volume=0.0, source="ticks", complete=False)
        else:
            bar.merge_tick(tick.price, 0.0)
        self.bus.publish(TICK, tick)

    def forming_bar(self, symbol: str, tf: Timeframe) -> Candle | None:
        """The current, not yet closed candle of ``tf``: closed 1m bars of
        this bucket (aggregator) merged with the live 1m bar from ticks."""
        m1 = self.forming_m1.get(symbol)
        if m1 is None:
            return None
        if tf is Timeframe.M1:
            return m1.copy()
        bucket = tf.align(m1.ts)
        agg = self.aggregator._open.get((symbol, tf))
        if agg is not None and agg.ts == bucket:
            bar = agg.copy()
            bar.high = max(bar.high, m1.high)
            bar.low = min(bar.low, m1.low)
            bar.close = m1.close
        else:
            bar = Candle(symbol=symbol, timeframe=tf, ts=bucket, open=m1.open,
                         high=m1.high, low=m1.low, close=m1.close, volume=0.0,
                         source="ticks", complete=False)
        bar.complete = False
        return bar

    # --- candle flow ------------------------------------------------------
    def _on_m1(self, bar: Candle) -> None:
        self.db.upsert_candles([bar])
        self.bus.publish(CANDLE, bar)
        self.aggregator.add_m1(bar)

    def _on_closed_candle(self, bar: Candle) -> None:
        self.db.upsert_candles([bar])
        self.bus.publish(CANDLE, bar)

    # --- lifecycle ----------------------------------------------------------
    async def _auto_universe(self) -> None:
        """Futures venue: pick the most liquid perpetuals by 24h USD volume
        (meme coins and tokenised stocks excluded). Falls back to the
        configured list when the venue is unreachable."""
        rs = self.runtime
        if not venue(rs["fee_venue"]).futures or not rs["auto_symbols"]:
            return
        try:
            uni = await fetch_universe(limit=rs["max_symbols"])
        except Exception as exc:
            log.warning("universe fetch failed: %s", exc)
            self.db.audit("data", f"Automatický výber mincí zlyhal: {exc}", "warning")
            return
        if not uni:
            return
        self.universe = uni
        syms = [u["symbol"] for u in uni]
        if "BTC/USD" not in syms:
            syms.insert(0, "BTC/USD")          # BTC data drives the trend filter
        self.crypto_symbols = syms
        self.db.audit("data", f"Mince podľa likvidity (24h objem v USD): {', '.join(syms)}",
                      data={u["symbol"]: round(u["volume_quote"]) for u in uni})

    def _build_providers(self) -> None:
        s = self.settings
        if self.crypto_symbols:
            if venue(self.runtime["fee_venue"]).futures:
                self.providers.append(KrakenFuturesProvider(
                    self.crypto_symbols, self._on_m1, self.health,
                    poll_seconds=s.candle_poll_seconds))
                self.ticker = KrakenFuturesTicker(self.crypto_symbols, self._on_tick,
                                                  self.health, interval=s.tick_seconds)
                from .trading.broker import PaperBroker
                if isinstance(self.broker, PaperBroker):
                    self.broker.funding_provider = lambda sym: (
                        (self.ticker.info.get(sym) or {}).get("funding_rate_h")
                        if self.ticker else None)
            elif s.crypto_provider == "kraken":
                self.providers.append(KrakenProvider(
                    self.crypto_symbols, self._on_m1, self.health,
                    poll_seconds=s.candle_poll_seconds))
                self.ticker = KrakenTicker(self.crypto_symbols, self._on_tick, self.health,
                                           interval=s.tick_seconds)
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
        # futures: pull 1h/4h history from the charts API so the 4h EMA200
        # and the analysis windows exist right from the first start
        if venue(self.runtime["fee_venue"]).futures and self.crypto_symbols:
            from .marketdata.kraken_futures import fetch_history
            for sym in self.crypto_symbols:
                for days, res in ((50, "1h"), (90, "4h")):
                    try:
                        bars = await fetch_history(sym, days, res)
                        if bars:
                            self.db.upsert_candles(bars)
                    except Exception as exc:
                        log.warning("futures backfill %s %s failed: %s", sym, res, exc)
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

        await self._auto_universe()
        self._build_providers()
        await self._backfill()
        for p in self.providers:
            p.start()
        if self.ticker is not None:
            self.ticker.start()
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
                         settings=self.settings, engine=self)
        # log_config=None: uvicorn's default config builds console formatters
        # that call sys.stdout.isatty(), which crashes when there is no console
        # (pythonw / windowed EXE). Its loggers propagate to our root handlers.
        config = uvicorn.Config(app, host=self.settings.api_host,
                                port=self.settings.api_port, log_level="warning",
                                log_config=None)
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
            await self.llm.aclose()
            for p in self.providers:
                await p.stop()
            if self.ticker is not None:
                await self.ticker.stop()
            self.health.stop()
            self.db.audit("system", "Engine stopped")
            log.info("engine stopped")
