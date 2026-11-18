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
        self._explore_off_reason: str | None = None
        self._sleeve_off_reason: dict[str, str] = {}
        self._risk_guard_active = False
        self._risk_guard_reason: str | None = None
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
            from .strategy.futures import (ACTIVE_STRATEGIES, EXPLORATION_STRATEGIES,
                                           FUTURES_STRATEGIES)
            strategies = (list(FUTURES_STRATEGIES) + list(EXPLORATION_STRATEGIES)
                          + list(ACTIVE_STRATEGIES))
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

        from .ai_trader import AITrader
        self.ai_trader = AITrader(
            self.db, self.bus, self.health, self.llm, self.runtime, self.risk,
            self.executor, self.portfolio,
            price_provider=lambda sym: (self.last_tick[sym].price
                                        if sym in self.last_tick else None),
            funding_provider=lambda sym: ((self.ticker.info.get(sym) or {}).get("funding_rate_h")
                                          if self.ticker is not None
                                          and hasattr(self.ticker, "info") else None),
            account_provider=self.portfolio.account_state)

        self.apply_settings()
        self.runtime.on_change(lambda _changed: self.apply_settings())
        try:
            self.check_risk_guard()
        except Exception as exc:                 # never block start-up
            log.warning("risk guard check failed: %s", exc)

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

    # --- exploration (skúšobný režim) guard ---------------------------------
    EXPLORE_MIN_TRADES = 20
    EXPLORE_MAX_LOSS_PCT = 1.0      # of the bot budget, net of fees
    # the trend portfolio: its trades cluster (win rate ~36 %) - 40 % of all
    # 500-trade windows of a PF 1.43 history have PF < 1 - so the PF verdict
    # waits ~8 months; the loss cap catches a real breakdown earlier
    SLEEVE_MIN_TRADES = {"active": 3000}
    SLEEVE_MAX_LOSS_PCT = {"active": 25.0}

    SLEEVE_SETTING = {"explore": "explore_enabled", "ai": "ai_trader_enabled",
                      "active": "active_enabled"}
    SLEEVE_LABEL = {"explore": "Skúšobný režim", "ai": "AI trader", "active": "Aktívny režim"}

    def explore_status(self, sleeve: str = "explore") -> dict:
        st = self.portfolio.explore_stats(sleeve)
        rs = self.runtime
        scale = {"explore": rs["explore_risk_scale"], "ai": rs["ai_risk_scale"],
                 "active": rs["active_risk_pct"] * self.decision_engine.active_vol_k
                 / max(1e-9, rs["risk_per_trade_pct"])}[sleeve]
        if sleeve == "explore":
            max_open, max_daily = (self.risk.limits.max_explore_positions,
                                   self.risk.limits.max_explore_daily)
        else:
            max_open, max_daily, _ = self.risk.limits.sleeve_caps[sleeve]
        st.update({"enabled": rs[self.SLEEVE_SETTING[sleeve]],
                   "risk_pct": round(rs["risk_per_trade_pct"] * scale, 3),
                   "min_trades": self.SLEEVE_MIN_TRADES.get(sleeve, self.EXPLORE_MIN_TRADES),
                   "max_loss_eur": round(rs["budget_eur"] * self.SLEEVE_MAX_LOSS_PCT.get(
                       sleeve, self.EXPLORE_MAX_LOSS_PCT) / 100.0, 2),
                   "max_daily": max_daily, "max_open": max_open,
                   "disabled_reason": self._sleeve_off_reason.get(sleeve)})
        if sleeve == "active":
            from .strategy.futures import ACTIVE_STRATEGIES
            st.update({"coins": rs["active_coins"], "systems": len(ACTIVE_STRATEGIES),
                       "system_labels": [s.label for s in ACTIVE_STRATEGIES],
                       "vol_factor": round(self.decision_engine.active_vol_k, 2),
                       "market_vol_pct": round(self._market_vol * 100, 2)
                       if self._market_vol else None,
                       "base_risk_pct": rs["active_risk_pct"]})
            from .strategy.trend_portfolio import FLIP_STATE
            names = {s.name for s in ACTIVE_STRATEGIES}
            sides = [v for (n, sym), v in FLIP_STATE.items()
                     if n in names and sym in self.crypto_symbols]
            st["trend_long"], st["trend_short"] = sides.count(1), sides.count(-1)
        return st

    _market_vol: float | None = None

    def update_market_vol(self) -> float:
        """Median 30-day std of daily returns across the traded coins (from
        local 1h candles, UTC days) -> risk multiplier of the trend portfolio."""
        from .strategy.trend_portfolio import market_vol_factor
        import statistics
        vols = []
        for sym in self.crypto_symbols:
            bars = self.db.get_candles(sym, Timeframe.H1, limit=24 * 32)
            closes = [b.close for b in bars if b.ts % 86400 == 82800]
            if len(closes) >= 21:
                rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))][-30:]
                vols.append(statistics.pstdev(rets))
        self._market_vol = statistics.median(vols) if vols else None
        self.decision_engine.active_vol_k = market_vol_factor(self._market_vol)
        return self.decision_engine.active_vol_k

    def _check_explore(self) -> None:
        """Switch a trial account off once it demonstrably loses AFTER fees:
        profit factor < 1 after 20 trades, or a net loss beyond 1 % of the
        budget at any point. Checked for every trial sleeve."""
        for sleeve, key in self.SLEEVE_SETTING.items():
            if not self.runtime[key]:
                continue
            st = self.portfolio.explore_stats(sleeve)
            max_loss = self.runtime["budget_eur"] * self.SLEEVE_MAX_LOSS_PCT.get(
                sleeve, self.EXPLORE_MAX_LOSS_PCT) / 100.0
            min_trades = self.SLEEVE_MIN_TRADES.get(sleeve, self.EXPLORE_MIN_TRADES)
            reason = None
            if st["trades"] >= min_trades and (st["profit_factor"] or 0) < 1.0:
                reason = (f"po {st['trades']} obchodoch prerába po poplatkoch "
                          f"(PF {st['profit_factor']}, čistý výsledok {st['net_pl']:+.2f} €)")
            elif st["net_pl"] <= -max_loss:
                reason = (f"čistá strata {st['net_pl']:.2f} € prekročila limit "
                          f"{max_loss:.2f} €")
            if reason:
                self._sleeve_off_reason[sleeve] = reason
                if sleeve == "explore":
                    self._explore_off_reason = reason
                self.runtime.update({key: False})
                msg = (f"{self.SLEEVE_LABEL[sleeve]} vypnutý: {reason}. "
                       f"Hlavná stratégia beží ďalej.")
                self.db.audit("risk", msg, "warning", st)
                self.bus.publish("system_alert", {"category": "bot_state", "message": msg})

    # --- evidence guard: risk back to 1 % when live results disagree --------
    GUARD_RISK_PCT = 1.0
    GUARD_MIN_TRADES = 20

    def main_strategy_stats(self) -> dict:
        """Closed trades of the validated strategy only (not the trial
        accounts, not legacy 5m spot trades): PF and drawdown, net of fees."""
        from sqlalchemy import select
        from .db.schema import Trade
        names = [s.name for s in self.decision_engine.strategies if not s.exploration]
        with self.db.session() as s:
            pls = s.execute(select(Trade.pl).where(
                Trade.status == "closed", Trade.mode == "paper",
                Trade.strategy.in_(names)).order_by(Trade.id)).scalars().all()
        pls = [p or 0.0 for p in pls]
        pos, neg = sum(p for p in pls if p > 0), -sum(p for p in pls if p <= 0)
        eq = peak = self.settings.paper_equity
        dd = 0.0
        for p in pls:
            eq += p
            peak = max(peak, eq)
            dd = max(dd, (peak - eq) / peak * 100.0 if peak > 0 else 0.0)
        cur_dd = (peak - eq) / peak * 100.0 if peak > 0 else 0.0
        return {"trades": len(pls), "profit_factor": round(pos / neg, 2) if neg > 0 else None,
                "net_pl": round(sum(pls), 2), "max_dd_pct": round(dd, 1),
                "current_dd_pct": round(cur_dd, 1)}

    def check_risk_guard(self) -> bool:
        """Turn the guard on when the live edge is not confirmed: PF < 1.0
        after 20 trades, or a 20 % drawdown of the strategy; off again at
        PF >= 1.2 and drawdown under 10 %."""
        st = self.main_strategy_stats()
        pf = st["profit_factor"] if st["profit_factor"] is not None else 99.0
        was = self._risk_guard_active
        if not was and ((st["trades"] >= self.GUARD_MIN_TRADES and pf < 1.0)
                        or st["current_dd_pct"] >= 20.0):
            self._risk_guard_active = True
            self._risk_guard_reason = (
                f"PF {pf:.2f} po {st['trades']} obchodoch" if st["current_dd_pct"] < 20.0
                else f"prepad stratégie {st['current_dd_pct']:.1f} %")
        elif was and pf >= 1.2 and st["current_dd_pct"] < 10.0:
            self._risk_guard_active = False
            self._risk_guard_reason = None
        if was != self._risk_guard_active:
            msg = (f"Poistka rizika ZAPNUTÁ ({self._risk_guard_reason}): riziko na obchod "
                   f"znížené na {self.GUARD_RISK_PCT:g} %, kým výsledky nepotvrdia edge."
                   if self._risk_guard_active else
                   "Poistka rizika vypnutá: výsledky sa zlepšili, riziko späť na nastavenú hodnotu.")
            self.db.audit("risk", msg, "warning" if self._risk_guard_active else "info", st)
            self.bus.publish("system_alert", {"category": "bot_state", "message": msg})
            self.apply_settings()
        return self._risk_guard_active

    def risk_status(self) -> dict:
        st = self.main_strategy_stats()
        return {**st, "guard_enabled": self.runtime["risk_guard"],
                "guard_active": self._risk_guard_active,
                "guard_reason": self._risk_guard_reason,
                "risk_setting_pct": self.runtime["risk_per_trade_pct"],
                "risk_effective_pct": self.risk.limits.effective_risk_pct(),
                "max_open_positions": self.runtime["max_open_positions"],
                "tradable": sorted(self.decision_engine.allowed_symbols or [])}

    async def _explore_guard_loop(self, q) -> None:
        while True:
            ev = await q.get()
            if isinstance(ev.data, dict) and ev.data.get("kind") == "closed":
                try:
                    self._check_explore()
                    self.check_risk_guard()
                except Exception as exc:
                    log.warning("explore guard failed: %s", exc)

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
    def set_paper_capital(self, amount: float) -> dict:
        """Capital of the paper account (deposit / withdrawal). When the bot
        budget covered the whole capital before, it follows the new capital,
        so 'the bot uses everything' stays true after the change."""
        before = self.portfolio.equity
        delta = self.portfolio.set_capital(amount)
        changes = {}
        if self.runtime["budget_eur"] >= before - 0.005:
            changes = self.runtime.update({"budget_eur": float(amount)})
        self.db.audit("system", f"Kapitál paper účtu nastavený na {float(amount):.2f} € "
                      f"({delta:+.2f} €)", data={"before": round(before, 2),
                                                 "after": float(amount), **changes})
        return {"capital": round(self.portfolio.equity, 2), "delta": round(delta, 2),
                "budget_eur": self.runtime["budget_eur"]}

    def apply_settings(self) -> None:
        rs = self.runtime
        cost = round_trip_cost_pct(rs["fee_venue"], 
                                   "maker" if rs["entry_order"] == "limit" else "taker",
                                   rs["slippage_pct"])
        L = self.risk.limits
        L.risk_per_trade_pct = rs["risk_per_trade_pct"]
        if rs["risk_guard"] and self._risk_guard_active:
            L.risk_per_trade_pct = min(L.risk_per_trade_pct, self.GUARD_RISK_PCT)
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
        self.decision_engine.explore_enabled = rs["explore_enabled"] and v.futures
        self.decision_engine.explore_risk_scale = rs["explore_risk_scale"]
        self.decision_engine.active_enabled = rs["active_enabled"] and v.futures
        self.decision_engine.active_risk_pct = rs["active_risk_pct"]
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
        # history replayed at startup only rebuilds the candles; analysing
        # hours-old bars would just burn CPU and delay the live feed
        if not getattr(self, "_backfilling", False):
            self.bus.publish(CANDLE, bar)

    # --- lifecycle ----------------------------------------------------------
    async def _auto_universe(self) -> None:
        """Futures venue: pick the most liquid perpetuals by 24h USD volume
        (meme coins and tokenised stocks excluded). Falls back to the
        configured list when the venue is unreachable."""
        rs = self.runtime
        if not venue(rs["fee_venue"]).futures or not rs["auto_symbols"]:
            return
        if "crypto_symbols" in self.settings.model_fields_set:
            return                     # explicit config (env/.env/tests) wins
        n = rs["max_symbols"]
        # active mode trades a wider set; the focused strategy only the top n
        want = max(n + 3, rs["active_coins"] if rs["active_enabled"] else 0)
        try:
            # the wider active set goes down to 1 M$/day (positions there are
            # tiny: a tenth of the risk); the focused top n stay the giants
            uni = await fetch_universe(limit=want, rank_by_30d=True,
                                       min_volume_usd=1e6 if want > n + 3 else 2e6)
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
        # trade only the top N by 30-day USD volume; the rest is watched so
        # the daily re-ranking can promote it without a restart
        self.decision_engine.allowed_symbols = {u["symbol"] for u in uni[:n]}
        self.db.audit("data", "Obchodované mince (top podľa 30-dňového objemu v USD): "
                      f"{', '.join(u['symbol'] for u in uni[:n])}; sledované navyše: "
                      f"{', '.join(u['symbol'] for u in uni[n:]) or '-'}",
                      data={u["symbol"]: round(u.get("volume_30d") or 0) for u in uni})

    def rerank_universe(self) -> list[str] | None:
        """Daily: re-rank the subscribed coins by 30-day USD volume from the
        local 1h candles and update which ones may trade."""
        if self.decision_engine.allowed_symbols is None:
            return None
        n = self.runtime["max_symbols"]
        vol = {}
        since = int(time.time()) - 30 * 86400
        for sym in self.crypto_symbols:
            bars = [b for b in self.db.get_candles(sym, Timeframe.H1, limit=24 * 31)
                    if b.ts >= since]
            if len(bars) >= 24 * 20:
                vol[sym] = sum(b.volume * b.close for b in bars)
        if len(vol) < n:
            return None
        top = sorted(vol, key=lambda s: -vol[s])[:n]
        if set(top) != self.decision_engine.allowed_symbols:
            self.db.audit("data", f"Nové poradie podľa 30-dňového objemu: obchodujem "
                          f"{', '.join(top)}")
        self.decision_engine.allowed_symbols = set(top)
        return top

    async def _rerank_loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(3600)
            n += 1
            try:
                self.update_market_vol()
                if n % 24 == 0:
                    self.rerank_universe()
            except Exception as exc:
                log.warning("universe re-rank failed: %s", exc)

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
        self._backfilling = True
        try:
            await self._backfill_inner()
        finally:
            self._backfilling = False

    async def _backfill_inner(self) -> None:
        # futures: pull 1h/4h history from the charts API so the 4h EMA200
        # and the analysis windows exist right from the first start
        if venue(self.runtime["fee_venue"]).futures and self.crypto_symbols:
            from .marketdata.kraken_futures import fetch_history
            for sym in self.crypto_symbols:
                # 1h: 150 days - the trend portfolio's 8h systems need EMA200
                for days, res in ((4, "15m"), (150, "1h"), (90, "4h")):
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
                    # start the aggregator on a 4h boundary: a bucket built
                    # from a partial window would overwrite the complete
                    # API bar (wrong open, too little volume)
                    if bars:
                        first = Timeframe.H4.align(bars[0].ts)
                        if first < bars[0].ts:
                            first += Timeframe.H4.seconds
                        for b in bars:
                            if b.ts < first:
                                self.db.upsert_candles([b])
                        bars = [b for b in bars if b.ts >= first]
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

    async def _start_market_data(self) -> None:
        try:
            self.health.set_status("engine", Status.OK, "načítavam trhové dáta")
            await self._auto_universe()
            tracked = self.crypto_symbols + self.settings.stock_symbols
            self.ml_scheduler._symbols = tracked or ["BTC/USD"]
            self._build_providers()
            if getattr(self, "_info", None) is not None:
                self._info.update({k: v for k, v in self.info().items()
                                   if k in ("provider", "symbols")})
            await self._backfill()
            try:
                self.update_market_vol()
            except Exception as exc:
                log.warning("market volatility unavailable: %s", exc)
            for p in self.providers:
                p.start()
            if self.ticker is not None:
                self.ticker.start()
            self.health.set_status("engine", Status.OK, "running")
            self.db.audit("data", f"Trhové dáta pripravené: {len(self.crypto_symbols)} mincí")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("market data start failed: %s", exc)
            self.health.set_status("engine", Status.DEGRADED, f"dáta: {exc}")
            self.db.audit("data", f"Štart trhových dát zlyhal: {exc}", "error")

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

        self.analysis.start()
        self.executor.start()
        self.ai_trader.start()
        if hasattr(self.broker, "start"):
            self.broker.start()
        self.ml_scheduler.start()
        if self.news is not None:
            self.news.start()
        if self.cloud is not None:
            self.cloud.start()
        flush_task = asyncio.get_running_loop().create_task(self._flush_loop(), name="flush-loop")
        rerank_task = asyncio.get_running_loop().create_task(self._rerank_loop(), name="rerank")
        explore_q = self.bus.subscribe("trade")
        explore_task = asyncio.get_running_loop().create_task(
            self._explore_guard_loop(explore_q), name="explore-guard")

        info = self.info()
        self._info = info
        # market data (coin selection, history download, live feeds) starts
        # in the background so the window and API are up immediately
        data_task = asyncio.get_running_loop().create_task(
            self._start_market_data(), name="market-data-start")
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
            data_task.cancel()
            explore_task.cancel()
            rerank_task.cancel()
            self.analysis.stop()
            self.executor.stop()
            self.ai_trader.stop()
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
