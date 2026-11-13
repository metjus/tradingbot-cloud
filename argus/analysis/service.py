"""Analysis service: runs the decision pipeline on every closed 5m bar.

Phase 4 runs in SIGNAL-ONLY mode: decisions are computed, persisted and
published for the UI, but nothing is executed. Paper execution arrives in
phase 8, live execution in phase 14 - both will consume the same
DecisionResult objects.
"""

from __future__ import annotations

import asyncio
import logging

from ..core.events import CANDLE, EventBus
from ..db.database import Database
from ..db.schema import Decision
from ..decision import DecisionEngine, DecisionResult
from ..marketdata.model import Candle, Timeframe
from ..monitoring.health import HealthMonitor, Status
from ..risk import AccountState
from ..strategy.base import MarketContext
from ..ta.features import compute_features

log = logging.getLogger(__name__)

DECISION_EVENT = "decision"

NEED = {Timeframe.M5: 400, Timeframe.M15: 240, Timeframe.H1: 240}


class AnalysisService:
    def __init__(self, db: Database, bus: EventBus, health: HealthMonitor,
                 engine: DecisionEngine, paper_equity: float,
                 sentiment_tracker=None, calendar=None,
                 account_provider=None) -> None:
        self._db = db
        self._bus = bus
        self._health = health
        self._engine = engine
        self._paper_equity = paper_equity
        self._sentiment = sentiment_tracker    # SentimentTracker | None
        self._calendar = calendar              # EventCalendar | None
        self._account_provider = account_provider  # () -> AccountState | None
        self._blackout_logged: dict[str, str] = {}
        self._task: asyncio.Task | None = None
        health.register("analysis", stale_after=1800)

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="analysis")
        self._health.set_status("analysis", Status.OK, "signal-only mode")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        q = self._bus.subscribe(CANDLE)
        while True:
            ev = await q.get()
            bar: Candle = ev.data
            if bar.timeframe is not Timeframe.M5 or not bar.complete:
                continue
            try:
                result = self.evaluate_symbol(bar.symbol, bar.ts + bar.timeframe.seconds)
                if result is not None:
                    self._persist(result)
                    self._bus.publish(DECISION_EVENT, result.as_dict())
                self._health.heartbeat("analysis", Status.OK, "signal-only mode")
            except Exception as exc:
                log.exception("analysis failed for %s: %s", bar.symbol, exc)
                self._health.set_status("analysis", Status.DEGRADED, str(exc))

    def evaluate_symbol(self, symbol: str, now_ts: int) -> DecisionResult | None:
        # economic-event blackout: no NEW entries around high-impact events
        if self._calendar is not None:
            ev = self._calendar.blackout(symbol)
            if ev is not None:
                if self._blackout_logged.get(symbol) != ev:
                    self._blackout_logged[symbol] = ev
                    self._db.audit("risk", f"{symbol}: entries paused around event '{ev}'",
                                   "warning")
                    self._bus.publish(DECISION_EVENT, {
                        "symbol": symbol, "verdict": "blackout", "event": ev})
                return None
            self._blackout_logged.pop(symbol, None)

        candles: dict[Timeframe, list] = {}
        features = {}
        for tf, limit in NEED.items():
            bars = self._db.get_candles(symbol, tf, limit=limit)
            candles[tf] = bars
            fs = compute_features(bars)
            if fs is not None:
                features[tf] = fs
        if Timeframe.M5 not in features:
            return None  # not enough history yet

        if self._account_provider is not None:
            account = self._account_provider()
        else:
            account = AccountState(equity=self._paper_equity,
                                   day_start_equity=self._paper_equity)
        from .regime import Regime
        sentiment = self._sentiment.score_for(symbol) if self._sentiment else None
        ctx = MarketContext(symbol=symbol, now_ts=now_ts, candles=candles,
                            features=features, regime=Regime.UNCERTAIN,
                            sentiment=sentiment)
        return self._engine.evaluate(ctx, account)

    def _persist(self, res: DecisionResult) -> None:
        if res.verdict == "no_signal":
            return
        best = res.best
        with self._db.session() as s:
            s.add(Decision(
                symbol=res.symbol,
                strategy=best.signal.strategy if best else "",
                score=best.score if best else 0.0,
                verdict=res.verdict,
                vetoed_by="risk_supervisor" if res.verdict == "veto" else None,
                reasons=res.as_dict(),
            ))
            s.commit()
        if res.verdict == "veto" and res.risk is not None:
            self._db.audit("risk", f"{res.symbol}: signal vetoed", "warning",
                           {"vetoes": res.risk.vetoes})
