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
                 engine: DecisionEngine, paper_equity: float) -> None:
        self._db = db
        self._bus = bus
        self._health = health
        self._engine = engine
        self._paper_equity = paper_equity
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

        # Account state: until paper execution exists (phase 8), no open
        # positions are tracked -> configured paper equity, flat account.
        account = AccountState(equity=self._paper_equity,
                               day_start_equity=self._paper_equity)
        from .regime import Regime
        ctx = MarketContext(symbol=symbol, now_ts=now_ts, candles=candles,
                            features=features, regime=Regime.UNCERTAIN)
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
