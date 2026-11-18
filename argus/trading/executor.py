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
        #: "limit" = maker entry at the signal price (cheaper), "market"
        self.entry_order = "market"
        #: symbol -> consecutive quiet 4h bars (volume-fade exit); set by engine
        self.vol_fade_provider = None
        self.state = "running"
        self._pending_entry: dict[str, dict] = {}   # client_order_id -> decision detail
        self._closing: set[str] = set()             # close requested, awaiting fill
        self._groups: dict[str, dict] = {}          # scaled-exit groups (CFE)
        self._group_of: dict[str, str] = {}
        self._processed_commands: set[str] = set()
        self._tasks: list[asyncio.Task] = []
        broker.on_fill = self._on_fill
        if hasattr(broker, "on_cancel"):
            broker.on_cancel = self._on_entry_cancel
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
        from ..strategy.futures import FLIP_EXIT_NAMES
        from ..strategy.trend_portfolio import FLIP_STATE
        for pos in list(self.portfolio.positions.values()):
            if pos.symbol != symbol or pos.client_order_id in self._closing:
                continue
            if pos.strategy == "cfe":
                continue           # CFE: break-even / lock rules on its parts only
            if pos.strategy in FLIP_EXIT_NAMES:
                # exits only when its system points the other way (+ safety stop)
                st = FLIP_STATE.get((pos.strategy, pos.symbol))
                if st is not None and st == (-1 if pos.direction == "LONG" else 1):
                    self._closing.add(pos.client_order_id)
                    self._db.audit("trade", f"{symbol}: systém sa otočil - výstup ({pos.strategy})")
                    self.broker.close_position(pos.client_order_id, "signal_flip")
                continue
            sentiment = self.sentiment_provider(symbol) if self.sentiment_provider else None
            regime = self.regime_provider(symbol) if self.regime_provider else None
            fade = self.vol_fade_provider(symbol) if self.vol_fade_provider else None
            for act in evaluate_exit(pos, price, self.exit_params, sentiment, regime,
                                     vol_fade=fade):
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
        from ..strategy.futures import FLIP_EXIT_NAMES, SLEEVE_OF
        sleeve = sig.get("sleeve") or ""
        flip = sig.get("strategy") in FLIP_EXIT_NAMES

        def mine() -> list:
            # one position per coin and account; the trend portfolio runs 12
            # independent systems, so there it is one per coin and SYSTEM
            return [p for p in self.portfolio.positions.values() if p.symbol == symbol and (
                p.strategy == sig.get("strategy") if flip
                else (SLEEVE_OF.get(p.strategy) or "") == sleeve)]
        # a flip strategy reverses: close its open position first
        if flip:
            for p in mine():
                if p.direction != sig["direction"] and p.client_order_id not in self._closing:
                    self._closing.add(p.client_order_id)
                    self.broker.close_position(p.client_order_id, "signal_flip")
        if mine():
            return
        # NOTE live: an exchange nets positions per contract, so separate
        # accounts on the same coin need sub-accounts before going live
        # a setup whose limit still rests in the book counts as taken
        if not flip and any(
                ((d.get("decision") or {}).get("best") or {}).get("signal", {}).get("symbol") == symbol
                and (SLEEVE_OF.get(d.get("strategy", "")) or "") == sleeve
                for d in self._pending_entry.values()):
            return
        tf_s = float(sig.get("expire_s") or 0) or {
            "1m": 60, "5m": 300, "15m": 900, "1h": 3600,
            "4h": 14400}.get(sig.get("timeframe"), 14400)
        entry_type = sig.get("entry_type") or (
            # the AI's level IS the trade (limit); the trend portfolio enters at
            # market as researched (a resting limit misses the best flips)
            {"ai": "limit", "active": "market"}.get(sleeve, self.entry_order))
        # scaled exit (CFE): equal parts with their own take-profit; the
        # parts form a group so TP1 can move the others to break-even
        tps = [float(x) for x in (sig.get("tps") or [])] or [sig["take_profit"]]
        qty = float(risk["quantity"]) / len(tps)
        group = new_client_order_id() if len(tps) > 1 else None
        legs = []
        for k, tp in enumerate(tps):
            coid = new_client_order_id()
            legs.append(coid)
            self._pending_entry[coid] = {
                "decision": decision, "stop_loss": sig["stop_loss"],
                "take_profit": tp, "strategy": sig["strategy"],
                "risk_pct": risk.get("risk_pct", 0.0) / len(tps),
                "leverage": risk.get("leverage", 1.0),
                "margin": risk.get("margin", 0.0) / len(tps),
                "liq_price": risk.get("liq_price"),
                "entry_reason": "; ".join(sig.get("reasons", []))[:500]
                + (f" [časť {k + 1}/{len(tps)}]" if group else ""),
            }
        if group:
            self._groups[group] = {"legs": legs, "tps": tps, "direction": sig["direction"],
                                   "entry": float(sig.get("entry") or 0.0), "stage": 0}
            for c in legs:
                self._group_of[c] = group
        for coid, tp in zip(legs, tps):
            self.broker.submit_bracket(BracketOrder(
                client_order_id=coid, symbol=symbol, direction=sig["direction"],
                quantity=qty, stop_loss=sig["stop_loss"], take_profit=tp,
                strategy=sig["strategy"], leverage=risk.get("leverage", 1.0),
                entry=entry_type, limit_price=float(sig.get("entry") or 0.0),
                expire_s=float(tf_s), cancel_price=float(sig.get("cancel_price") or 0.0),
                fill_through=bool(group)))

    def _on_group_tp(self, coid: str) -> None:
        """A part of a scaled exit hit its take-profit: TP1 -> the other parts
        to break-even, TP2 -> the last part's stop to TP1 (CFE rules)."""
        gid = self._group_of.get(coid)
        g = self._groups.get(gid) if gid else None
        if g is None:
            return
        g["stage"] += 1
        long_ = g["direction"] == "LONG"
        if g["stage"] == 1:
            first = next((p for c in g["legs"] if (p := self.portfolio.positions.get(c))), None)
            target = first.entry_price if first is not None else g["entry"]
            why = "break-even po TP1"
        elif g["stage"] == 2:
            target, why = g["tps"][0], "stop na TP1 po TP2"
        else:
            return
        for c in g["legs"]:
            pos = self.portfolio.positions.get(c)
            if pos is None or c in self._closing:
                continue
            better = target > pos.stop_loss if long_ else target < pos.stop_loss
            if better and self.broker.update_stop(c, target):
                pos.stop_loss = target
                pos.be_done = True
                self._db.audit("trade", f"{pos.symbol}: SL -> {target:.6g} ({why})")

    def cancel_resting(self, symbol: str, strategy: str) -> int:
        """Cancel resting (unfilled) limit entries of one strategy on one
        symbol - used when the AI trader re-places its level."""
        n = 0
        for coid, detail in list(self._pending_entry.items()):
            sig = (((detail.get("decision") or {}).get("best") or {}).get("signal") or {})
            if sig.get("symbol") == symbol and detail.get("strategy") == strategy \
                    and hasattr(self.broker, "cancel_pending") \
                    and self.broker.cancel_pending(coid):
                self._pending_entry.pop(coid, None)
                n += 1
        return n

    def has_resting(self, symbol: str, strategy: str) -> bool:
        for detail in self._pending_entry.values():
            sig = (((detail.get("decision") or {}).get("best") or {}).get("signal") or {})
            if sig.get("symbol") == symbol and detail.get("strategy") == strategy:
                return True
        return False

    def _forget_group_if_done(self, coid: str) -> None:
        gid = self._group_of.get(coid)
        g = self._groups.get(gid) if gid else None
        if g is None:
            return
        if not any(c in self.portfolio.positions or c in self._pending_entry for c in g["legs"]):
            for c in g["legs"]:
                self._group_of.pop(c, None)
            self._groups.pop(gid, None)

    def _on_entry_cancel(self, client_order_id: str, reason: str) -> None:
        self._forget_group_if_done(client_order_id)
        detail = self._pending_entry.pop(client_order_id, None)
        if detail is None:
            return
        sym = ((detail.get("decision") or {}).get("best") or {}).get("signal", {}).get("symbol", "?")
        self._db.audit("trade", f"{sym}: {reason} (cena neprišla k limitu)")

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
                               "features_m5": decision.get("features_m5")},
                leverage=detail.get("leverage", 1.0),
                margin=detail.get("margin", 0.0) or
                fill.price * fill.quantity / max(1.0, detail.get("leverage", 1.0)),
                liq_price=detail.get("liq_price"))
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
            if fill.kind == "take_profit":
                self._on_group_tp(fill.client_order_id)
            self._forget_group_if_done(fill.client_order_id)
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
                "leverage": pos.leverage, "liq_price": pos.liq_price,
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
            return {"ok": True, "state": self.state, "closing": pos.client_order_id,
                    "symbol": pos.symbol, "closed": 1}
        elif action == "emergency_stop":
            self.risk.halt("emergency stop")
            closed, failed = 0, 0
            for pos in list(self.portfolio.positions.values()):
                # own exit reason: the history must show it was the emergency
                # stop, not a manual close of one position
                ok = self.broker.close_position(pos.client_order_id, "emergency_stop")
                closed += ok
                failed += not ok
            self._set_state("stopped",
                            f"EMERGENCY STOP: {closed} closed, {failed} failed")
            if failed:
                self._db.audit("trade",
                               f"EMERGENCY STOP: {failed} positions NOT confirmed closed",
                               "critical")
            return {"ok": True, "state": self.state, "closed": closed, "failed": failed}
        else:
            return {"ok": False, "error": f"unknown action {action!r}",
                    "state": self.state}
        return {"ok": True, "state": self.state}

    def restore_state(self) -> None:
        """Re-apply a PAUSE / EMERGENCY STOP that was active before a restart -
        a bot stopped from the phone must not silently resume trading."""
        from ..db.schema import AppSetting
        with self._db.session() as s:
            row = s.get(AppSetting, "bot_state")
            saved = (row.value or {}).get("state") if row is not None else None
        if saved == "stopped":
            self.risk.halt("emergency stop (pred reštartom)")
            self.state = "stopped"
        elif saved in ("paused", "pausing"):
            self.state = "pausing" if self.portfolio.positions else "paused"
        if saved in ("stopped", "paused", "pausing"):
            self._db.audit("system", f"Stav bota obnovený po reštarte: {self.state}")
            self._health.set_status("execution", Status.OK, self.state)

    def _persist_state(self) -> None:
        from ..db.schema import AppSetting
        with self._db.session() as s:
            row = s.get(AppSetting, "bot_state")
            if row is None:
                s.add(AppSetting(key="bot_state", value={"state": self.state}))
            else:
                row.value = {"state": self.state}
            s.commit()

    def _set_state(self, state: str, why: str) -> None:
        if state == self.state:
            return
        self.state = state
        self._persist_state()
        self._db.audit("system", f"Bot state -> {state} ({why})",
                       "warning" if state in ("stopped",) else "info")
        self._health.set_status("execution", Status.OK, state)
        self._bus.publish(STATE_EVENT, {"state": state, "why": why})
