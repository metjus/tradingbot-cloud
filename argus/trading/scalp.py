"""Scalp mode runner: on every closed 1m bar of a scalp coin, look for a
signal, size it (capital / slots x leverage) and place a resting limit
entry with a take-profit that nets the target after fees.

The target grows with the capital (100 % of the profit is reinvested):
target_now = target_eur x capital / starting capital of the paper account.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from ..core.events import CANDLE, EventBus
from ..db.database import Database
from ..marketdata.model import Candle, Timeframe
from ..monitoring.health import HealthMonitor, Status
from ..risk import RiskSupervisor
from ..strategy import scalp
from ..strategy.base import Signal
from .executor import ExecutionService
from .portfolio import Portfolio

log = logging.getLogger(__name__)

NAME = "scalp"


class ScalpService:
    def __init__(self, db: Database, bus: EventBus, health: HealthMonitor,
                 risk: RiskSupervisor, executor: ExecutionService, portfolio: Portfolio,
                 spread_provider: Callable[[str], float | None] | None = None,
                 regime_provider: Callable[[str], object] | None = None) -> None:
        self._db = db
        self._bus = bus
        self._health = health
        self.risk = risk
        self.executor = executor
        self.portfolio = portfolio
        self._spread = spread_provider
        self._regime = regime_provider
        self.enabled = True
        self.kind = "breakout"
        self.slots = 5
        self.target_eur = 1.5
        self.sl_pct = 0.6
        self.valid_min = 5
        #: coins the scalp mode trades (set by the engine: top by volume)
        self.symbols: list[str] = []
        self.signals = 0
        self.last_signal: dict | None = None
        self.last_skip: str | None = None
        self._task: asyncio.Task | None = None
        health.register("scalp", stale_after=900)

    # --- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._q = self._bus.subscribe(CANDLE)
        self._task = asyncio.get_running_loop().create_task(self._run(), name="scalp")
        self._health.set_status("scalp", Status.OK, "čakám na minútové sviečky")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            ev = await self._q.get()
            bar: Candle = ev.data
            if bar.timeframe is not Timeframe.M1 or not bar.complete:
                continue
            try:
                self.on_bar(bar)
                self._health.heartbeat("scalp", Status.OK,
                                       f"{len(self.symbols)} mincí, {self.signals} signálov")
            except Exception as exc:          # never let one bar kill the loop
                log.exception("scalp failed for %s: %s", bar.symbol, exc)
                self._health.set_status("scalp", Status.DEGRADED, str(exc))

    # --- one closed 1m bar ---------------------------------------------------
    def target_now(self) -> float:
        """Target per winning trade in EUR, grown with the capital."""
        start = self.portfolio.start_equity or 0.0
        if start <= 0:
            return self.target_eur
        return self.target_eur * max(self.portfolio.equity, 0.0) / start

    def on_bar(self, bar: Candle, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        if not self.enabled or bar.symbol not in self.symbols:
            return None
        if bar.ts + 60 < now - 180:                       # replayed / stale bar
            return None
        ex = self.executor
        if ex.state != "running" or ex.target_reached or self.risk.halted:
            return None
        from ..strategy.futures import SLEEVE_OF
        if any(p.symbol == bar.symbol and SLEEVE_OF.get(p.strategy) == "scalp"
               for p in self.portfolio.positions.values()) or ex.has_resting(bar.symbol, NAME):
            return None
        used, margin_used = ex.sleeve_usage("scalp")
        if used >= self.slots:
            return None
        bars = self._db.get_candles(bar.symbol, Timeframe.M1, limit=scalp.NEED_BARS)
        if not bars or bars[-1].ts != bar.ts:
            return None
        sig = scalp.signal(bars, self.kind)
        if sig is None:
            return None
        self.signals += 1
        s = 1.0 if sig.direction == "LONG" else -1.0
        sl = sig.level * (1 - s * self.sl_pct / 100.0)
        spread = self._spread(bar.symbol) if self._spread else None
        verdict = self.risk.check_scalp(sig.direction, sig.level, sl,
                                        self.portfolio.account_state(), self.slots, used,
                                        margin_used, spread)
        if not verdict.approved:
            self.last_skip = f"{bar.symbol}: " + "; ".join(verdict.vetoes)
            return None
        target = self.target_now()
        tp_pct = scalp.take_profit_pct(target, verdict.notional)
        sl, tp = scalp.levels(sig, tp_pct, self.sl_pct)
        signal = Signal(NAME, bar.symbol, sig.direction, sig.level, sl, tp, 0.5, [
            sig.reason,
            f"cieľ {target:.2f} € po poplatkoch (TP {tp_pct:.2f} %), stop {self.sl_pct:.2f} %",
            f"pozícia {verdict.notional:.0f} € = {verdict.margin:.0f} € × páka {verdict.leverage:g}"],
            timeframe=Timeframe.M1, exploration=True, sleeve="scalp",
            expire_s=float(self.valid_min * 60))
        regime = self._regime(bar.symbol) if self._regime else None
        decision = {"symbol": bar.symbol, "verdict": "enter",
                    "regime": getattr(regime, "value", regime),
                    "best": {"signal": signal.as_dict(), "score": None, "factors": {}},
                    "risk": verdict.as_dict(), "evaluated": [], "notes": []}
        self.last_signal = {"symbol": bar.symbol, "direction": sig.direction,
                            "level": sig.level, "tp": tp, "sl": sl, "target_eur": round(target, 2),
                            "notional": round(verdict.notional, 2), "ts": bar.ts + 60}
        ex._maybe_enter(decision)
        return decision
