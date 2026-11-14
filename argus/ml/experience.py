"""Learning from the bot's own closed trades (§9A/§9D).

Closed trades carry their full entry context (signal, features, regime),
so they convert into the same SignalRecord rows the backtester produces -
one dataset format for backtest AND lived experience.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from ..analysis.regime import Regime
from ..backtest.engine import SignalRecord
from ..db.database import Database
from ..db.schema import Trade
from ..marketdata.model import Timeframe
from ..strategy.base import Signal

log = logging.getLogger(__name__)

_EXIT_OUTCOME = {"take_profit": "win", "stop_loss": "loss"}


def trade_to_record(t: Trade) -> SignalRecord | None:
    ctx = t.entry_context or {}
    sig_d = ctx.get("signal") or {}
    feats = ctx.get("features_m5") or {}
    if not sig_d or not feats or t.entry_price is None:
        return None
    outcome = _EXIT_OUTCOME.get(t.exit_reason or "", "timeout")
    risk_per_unit = abs((t.entry_price or 0.0) - (t.stop_loss or 0.0))
    if risk_per_unit <= 0 or not t.quantity:
        return None
    pl_r = (t.pl or 0.0) / (risk_per_unit * t.quantity)
    try:
        regime = Regime(ctx.get("regime") or "uncertain")
    except ValueError:
        regime = Regime.UNCERTAIN
    sig = Signal(
        strategy=t.strategy, symbol=t.symbol, direction=t.direction,
        entry=float(sig_d.get("entry", t.entry_price)),
        stop_loss=float(sig_d.get("stop_loss", t.stop_loss or 0.0)),
        take_profit=float(sig_d.get("take_profit", t.take_profit or 0.0)),
        confidence=float(sig_d.get("confidence", 0.5)),
        reasons=list(sig_d.get("reasons") or []), timeframe=Timeframe.M5)
    ts = int(t.entry_ts.timestamp()) if t.entry_ts else 0
    exit_ts = int(t.exit_ts.timestamp()) if t.exit_ts else None
    return SignalRecord(signal=sig, bar_index=-1, ts=ts, regime=regime,
                        score=float(ctx.get("score") or 0.0), features=feats,
                        outcome=outcome, exit_ts=exit_ts, bars_held=0, pl_r=pl_r)


def load_experience(db: Database, mode: str | None = None) -> list[SignalRecord]:
    with db.session() as s:
        q = select(Trade).where(Trade.status == "closed")
        if mode:
            q = q.where(Trade.mode == mode)
        rows = s.execute(q.order_by(Trade.id)).scalars().all()
    out = []
    for t in rows:
        rec = trade_to_record(t)
        if rec is not None:
            out.append(rec)
    log.info("experience: %d usable records from %d closed trades", len(out), len(rows))
    return out
