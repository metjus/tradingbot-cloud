"""Execution service: turns approved decisions into broker orders and
manages the bot state machine (§19/§20 controls).

States:
  running   - entries allowed (subject to risk, blackouts, sessions)
  pausing   - PAUSE pressed: no new entries; open positions run to their
              SL/TP; becomes 'paused' when flat
  paused    - flat and idle
  stopped   - EMERGENCY STOP: entries blocked, open positions closed at
              market, every close verified via broker fills

Commands carry ids and are idempotent; every transition is audited.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..analysis.service import DECISION_EVENT
from ..core.events import CANDLE, TICK, EventBus
from ..db.database import Database
from ..marketdata.model import Candle, Timeframe
from ..monitoring.health import HealthMonitor, Status
from ..risk import RiskSupervisor
from .broker import BrokerAdapter, BracketOrder, Fill, PaperBroker, new_client_order_id
from .exits import ExitParams, evaluate_exit
from .hours import can_enter, must_flatten
from .portfolio import Portfolio

log = logging.getLogger(__name__)

TRADE_EVENT = "trade"          # data: {kind: opened|closed, ...}
STATE_EVENT = "bot_state"


class ExecutionService:
    def __init__(self, db: Database, bus: EventBus, health: HealthMonitor,
                 broker: BrokerAdapter, portfolio: Portfolio,
                 risk: RiskSupervisor, trailing_enabled: bool = True,
                 break_even_at_r: float = 0.8, trail_start_r: float = 1.2,
                 trail_distance_r: float = 0.8,
                 exit_params: ExitParams | None = None) -> None:
        self._db = db
        self._bus = bus
        self._health = health
        self.broker = broker
        self.portfolio = portfolio
        self.risk = risk
        self.exit_params = exit_params or ExitParams(
            enabled=trailing_enabled, break_even_at_r=break_even_at_r,
            trail_start_r=trail_start_r, trail_distance_r=trail_distance_r)
        #: callables set by the engine: symbol -> float|None / Regime|None
        self.sentiment_provider = None
        self.regime_provider = None
        self.state = "running"
        self._pending_entry: dict[str, dict] = {}   # client_order_id -> decision detail
        self._closing: set[str] = set()             # close requested, awaiting fill
        self._processed_commands: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        broker.on_fill = self._on_fill
        health.register("execution", stale_after=3600)
        health.set_status("execution", Status.OK, self.state)

    # --- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        loop = asyncio.get_running_loop()
        # subscribe synchronously so no event published right after start()
        # is lost before the loop tasks get scheduled
        self._q_decisions = self._bus.subscribe(DECISION_EVENT)
        self._q_candles = self._bus.subscribe(CANDLE)
        self._q_ticks = self._bus.subscribe(TICK)
        self._tasks = [
            loop.create_task(self._decision_loop(), name="exec-decisions"),
            loop.create_task(self._candle_loop(), name="exec-candles"),
            loop.create_task(self._tick_loop(), name="exec-ticks"),
            loop.create_task(self._session_loop(), name="exec-sessions"),
        ]

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    # --- event loops ------------------------------------------------------
    async def _decision_loop(self) -> None:
        q = self._q_decisions
        while True:
            ev = await q.get()
            d = ev.data
            if not isinstance(d, dict) or d.get("verdict") != "enter":
                continue
            try:
                self._maybe_enter(d)
            except Exception as exc:
                log.exception("entry failed: %s", exc)
                self._db.audit("trade", f"Entry failed: {exc}", "error", d)

    async def _candle_loop(self) -> None:
        q = self._q_candles
        while True:
            ev = await q.get()
            bar: Candle = ev.data
            if bar.timeframe is not Timeframe.M1:
                continue
            if isinstance(self.broker, PaperBroker) or hasattr(self.broker, "on_candle"):
                self.broker.on_candle(bar)
            self.portfolio.mark_price(bar.symbol, bar.close)
            self._manage_stops(bar.symbol, bar.close)
            if self.state == "pausing" and not self.portfolio.positions:
                self._set_state("paused", "all positions closed after PAUSE")

    async def _tick_loop(self) -> None:
        """Real-time prices: mark-to-market, broker-side SL/TP emulation in
        paper mode and adaptive exits between candle closes."""
        q = self._q_ticks
        while True:
            ev = await q.get()
            tick = ev.data
            try:
                if hasattr(self.broker, "on_tick"):
                    self.broker.on_tick(tick.symbol, tick.price, tick.ts)
                self.portfolio.mark_price(tick.symbol, tick.price)
                self._manage_stops(tick.symbol, tick.price)
            except Exception as exc:  # never let one bad tick kill the loop
                log.warning("tick handling failed: %s", exc)

    def _manage_stops(self, symbol: str, price: float) -> None:
        """Apply adaptive exit rules (see exits.py). Stops only tighten."""
        for pos in list(self.portfolio.positions.values()):
            if pos.symbol != symbol or pos.client_order_id in self._closing:
                continue
            sentiment = self.sentiment_provider(symbol) if self.sentiment_provider else None
            regime = self.regime_provider(symbol) if self.regime_provider else None
            for act in evaluate_exit(pos, price, self.exit_params, sentiment, regime):
                if act.kind == "close":
                    self._closing.add(pos.client_order_id)
                    self._db.audit("trade", f"{symbol}: adaptívny výstup - {act.reason}",
                                   data={"price": price, "profit_r": round(pos.profit_r(price), 2)})
                    self.broker.close_position(pos.client_order_id, f"adaptive: {act.reason}")
                    break
                if act.kind == "stop" and self.broker.update_stop(pos.client_order_id, act.price):
                    pos.stop_loss = act.price
                    if "riziko" in act.reason:
                        pos.risk_reduced = True
                    if "break-even" in act.reason and not pos.be_done:
                        pos.be_done = True
                        pos.risk_reduced = True
                    self._db.audit("trade", f"{symbol}: SL -> {act.price:.6g} ({act.reason})",
                                   data={"profit_r": round(pos.profit_r(price), 2)})
                if act.kind == "tp" and self.broker.update_take_profit(
                        pos.client_order_id, act.price):
                    pos.take_profit = act.price
                    pos.tp_extensions += 1
                    self._db.audit("trade", f"{symbol}: TP -> {act.price:.6g} ({act.reason})")

    async def _session_loop(self) -> None:
        notified_day: int | None = None
        while True:
            await asyncio.sleep(30)
            if self.state == "stopped":
                continue
            now = datetime.now(timezone.utc)
            # daily loss limit alert (entries are already blocked by risk)
            acct = self.portfolio.account_state()
            if acct.daily_pl_pct <= -self.risk.limits.daily_max_loss_pct \
                    and notified_day != now.toordinal():
                notified_day = now.toordinal()
                msg = (f"Dosiahnutý denný limit straty: {acct.daily_pl_pct:.2f}% "
                       f"(limit -{self.risk.limits.daily_max_loss_pct}%)")
                self._db.audit("risk", msg, "critical")
                self._bus.publish("system_alert", {"category": "daily_limit",
                                                   "message": msg})
            for pos in list(self.portfolio.positions.values()):
                if must_flatten(pos.symbol, now):
                    self._db.audit("trade", f"{pos.symbol}: closing before session end")
                    self.broker.close_position(pos.client_order_id, "session_close")
            self._health.heartbeat("execution", Status.OK, self.state)

    # --- entries ------------------------------------------------------------
    def _maybe_enter(self, decision: dict) -> None:
        if self.state != "running" or self.risk.halted:
            return
        best = decision.get("best") or {}
        sig = best.get("signal") or {}
        risk = decision.get("risk") or {}
        symbol = sig.get("symbol")
        if not symbol or not risk.get("approved"):
            return
        if not can_enter(symbol):
            self._db.audit("trade", f"{symbol}: entry skipped (outside session)")
            return
        if any(p.symbol == symbol for p in self.portfolio.positions.values()):
            return  # one position per symbol
        coid = new_client_order_id()
        self._pending_entry[coid] = {
            "decision": decision, "stop_loss": sig["stop_loss"],
            "take_profit": sig["take_profit"], "strategy": sig["strategy"],
            "risk_pct": risk.get("risk_pct", 0.0),
            "entry_reason": "; ".join(sig.get("reasons", []))[:500],
        }
        self.broker.submit_bracket(BracketOrder(
            client_order_id=coid, symbol=symbol, direction=sig["direction"],
            quantity=risk["quantity"], stop_loss=sig["stop_loss"],
            take_profit=sig["take_profit"], strategy=sig["strategy"]))

    # --- fills (the only source of truth) -------------------------------------
    def _on_fill(self, fill: Fill) -> None:
        if fill.kind == "entry":
            detail = self._pending_entry.pop(fill.client_order_id, {})
            decision = detail.get("decision", {})
            best = (decision.get("best") or {})
            pos = self.portfolio.record_entry(
                client_order_id=fill.client_order_id, symbol=fill.symbol,
                direction=fill.direction, quantity=fill.quantity, price=fill.price,
                fee=fill.fee, stop_loss=detail.get("stop_loss", 0.0),
                take_profit=detail.get("take_profit", 0.0),
                strategy=detail.get("strategy", ""),
                risk_pct=detail.get("risk_pct", 0.0),
                entry_reason=detail.get("entry_reason", ""),
                entry_context={"regime": decision.get("regime"),
                               "score": best.get("score"),
                               "factors": best.get("factors"),
                               "signal": best.get("signal"),
                               "features_m5": decision.get("features_m5")})
            if self.sentiment_provider is not None:
                pos.entry_sentiment = self.sentiment_provider(fill.symbol)
            self._db.audit("trade", f"OPEN {fill.symbol} {fill.direction} "
                           f"qty {fill.quantity:.6g} @ {fill.price:.6g}", data={
                               "strategy": pos.strategy, "sl": pos.stop_loss,
                               "tp": pos.take_profit, "risk_pct": pos.risk_pct})
            self._bus.publish(TRADE_EVENT, {"kind": "opened", **self._pos_dict(pos)})
        else:
            self._closing.discard(fill.client_order_id)
            pl = self.portfolio.record_exit(fill.client_order_id, fill.price,
                                            fill.fee, reason=fill.kind)
            if pl is None:
                return
            self._db.audit("trade", f"CLOSE {fill.symbol} ({fill.kind}) "
                           f"P/L {pl:+.2f}", data={"price": fill.price})
            self._bus.publish(TRADE_EVENT, {
                "kind": "closed", "symbol": fill.symbol, "reason": fill.kind,
                "pl": round(pl, 2), "price": fill.price,
                "equity": round(self.portfolio.equity, 2)})
            if self.state == "pausing" and not self.portfolio.positions:
                self._set_state("paused", "all positions closed after PAUSE")

    @staticmethod
    def _pos_dict(pos) -> dict:
        return {"client_order_id": pos.client_order_id,
                "symbol": pos.symbol, "direction": pos.direction,
                "quantity": pos.quantity, "entry": pos.entry_price,
                "invested": round(pos.invested, 2),
                "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                "strategy": pos.strategy}

    # --- controls ------------------------------------------------------------
    def command(self, action: str, command_id: str | None = None,
                target: str | None = None) -> dict:
        if command_id:
            if command_id in self._processed_commands:
                return {"ok": True, "state": self.state, "duplicate": True}
            self._processed_commands.add(command_id)

        if action == "pause":
            if self.state == "running":
                self._set_state("pausing" if self.portfolio.positions else "paused",
                                "PAUSE requested")
        elif action == "start":
            if self.state in ("paused", "pausing", "stopped"):
                self.risk.resume()
                self._set_state("running", "START requested")
        elif action == "close_position":
            pos = self.portfolio.positions.get(target or "")
            if pos is None:
                return {"ok": False, "error": "pozícia neexistuje alebo už je zatvorená",
                        "state": self.state}
            if pos.client_order_id in self._closing:
                return {"ok": True, "state": self.state, "pending": True}
            self._closing.add(pos.client_order_id)
            if not self.broker.close_position(pos.client_order_id, "manual_close"):
                self._closing.discard(pos.client_order_id)
                return {"ok": False, "error": "broker zatvorenie odmietol", "state": self.state}
            self._db.audit("trade", f"{pos.symbol}: ručné zatvorenie pozície")
            return {"ok": True, "state": self.state, "closing": pos.client_order_id}
        elif action == "emergency_stop":
            self.risk.halt("emergency stop")
            closed, failed = 0, 0
            for pos in list(self.portfolio.positions.values()):
                ok = self.broker.close_position(pos.client_order_id, "manual_close")
                closed += ok
                failed += not ok
            self._set_state("stopped",
                            f"EMERGENCY STOP: {closed} closed, {failed} failed")
            if failed:
                self._db.audit("trade",
                               f"EMERGENCY STOP: {failed} positions NOT confirmed closed",
                               "critical")
        else:
            return {"ok": False, "error": f"unknown action {action!r}",
                    "state": self.state}
        return {"ok": True, "state": self.state}

    def _set_state(self, state: str, why: str) -> None:
        if state == self.state:
            return
        self.state = state
        self._db.audit("system", f"Bot state -> {state} ({why})",
                       "warning" if state in ("stopped",) else "info")
        self._health.set_status("execution", Status.OK, state)
        self._bus.publish(STATE_EVENT, {"state": state, "why": why})
