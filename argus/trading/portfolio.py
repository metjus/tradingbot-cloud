"""Portfolio manager: equity, open positions, trade records (learning memory).

Every closed trade persists the full §9A context so the ML pipeline can
train from live/paper experience exactly as it does from backtests.
Positions are recoverable: open trades are reloaded from the DB at start.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select

from ..db.database import Database
from ..db.schema import Trade
from ..risk import AccountState

log = logging.getLogger(__name__)


def _explore_names() -> set[str]:
    from ..strategy.futures import EXPLORATION_NAMES
    return EXPLORATION_NAMES


def _sleeve_of(strategy: str) -> str | None:
    from ..strategy.futures import SLEEVE_OF
    return SLEEVE_OF.get(strategy)


@dataclass(slots=True)
class OpenPosition:
    trade_id: int
    client_order_id: str
    symbol: str
    direction: str
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: datetime
    strategy: str
    risk_pct: float
    entry_fee: float = 0.0
    last_price: float | None = None
    initial_stop: float = 0.0        # SL at entry; current SL may trail
    be_done: bool = False            # break-even protection applied
    risk_reduced: bool = False       # first risk-reduction step applied
    max_favorable_r: float = 0.0     # best unrealised profit seen, in R
    entry_sentiment: float | None = None
    tp_extensions: int = 0
    leverage: float = 1.0
    margin: float = 0.0              # money blocked (notional / leverage)
    liq_price: float | None = None   # futures: estimated liquidation price

    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity

    @property
    def invested(self) -> float:
        """Money actually blocked in the position (margin on futures)."""
        return self.margin if self.margin > 0 else self.notional

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry_price - (self.initial_stop or self.stop_loss))

    def profit_r(self, price: float) -> float:
        r = self.risk_per_unit
        if r <= 0:
            return 0.0
        d = price - self.entry_price
        if self.direction == "SHORT":
            d = -d
        return d / r

    @property
    def unrealized_pl(self) -> float | None:
        if self.last_price is None:
            return None
        d = self.last_price - self.entry_price
        if self.direction == "SHORT":
            d = -d
        return d * self.quantity - self.entry_fee


@dataclass
class Portfolio:
    db: Database
    broker_name: str
    mode: str
    start_equity: float
    equity: float = 0.0
    day_start_equity: float = 0.0
    trades_today: int = 0
    explore_today: int = 0
    sleeve_today: dict = field(default_factory=dict)
    _day: int = -1
    positions: dict[str, OpenPosition] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.equity = self._load_equity()
        self._roll_day()
        self._recover_open_positions()

    # --- persistence helpers -------------------------------------------------
    def _load_equity(self) -> float:
        with self.db.session() as s:
            realized = s.execute(
                select(Trade.pl).where(Trade.status == "closed", Trade.mode == self.mode)
            ).scalars().all()
        return self.start_equity + sum(p or 0.0 for p in realized)

    def _recover_open_positions(self) -> None:
        with self.db.session() as s:
            rows = s.execute(select(Trade).where(
                Trade.status == "open", Trade.mode == self.mode)).scalars().all()
        for t in rows:
            ec = t.entry_context or {}
            self.positions[t.client_order_id] = OpenPosition(
                trade_id=t.id, client_order_id=t.client_order_id, symbol=t.symbol,
                direction=t.direction, quantity=t.quantity or 0.0,
                entry_price=t.entry_price or 0.0, stop_loss=t.stop_loss or 0.0,
                take_profit=t.take_profit or 0.0,
                opened_at=t.entry_ts or datetime.now(timezone.utc),
                strategy=t.strategy, risk_pct=t.risk_pct or 0.0, entry_fee=t.fees,
                initial_stop=t.stop_loss or 0.0,
                leverage=float(ec.get("leverage") or 1.0),
                margin=float(ec.get("margin") or 0.0),
                liq_price=ec.get("liq_price"))
        if rows:
            log.info("recovered %d open %s positions from DB", len(rows), self.mode)

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).toordinal()
        if today != self._day:
            self._day = today
            self.day_start_equity = self.equity
            self.trades_today = 0
            self.explore_today = 0
            self.sleeve_today = {}

    # --- lifecycle ------------------------------------------------------------
    def record_entry(self, client_order_id: str, symbol: str, direction: str,
                     quantity: float, price: float, fee: float, stop_loss: float,
                     take_profit: float, strategy: str, risk_pct: float,
                     entry_reason: str, entry_context: dict,
                     leverage: float = 1.0, margin: float = 0.0,
                     liq_price: float | None = None) -> OpenPosition:
        self._roll_day()
        entry_context = {**entry_context, "leverage": leverage,
                         "margin": margin, "liq_price": liq_price}
        with self.db.session() as s:
            t = Trade(client_order_id=client_order_id, broker=self.broker_name,
                      mode=self.mode, symbol=symbol, direction=direction,
                      strategy=strategy, regime=entry_context.get("regime"),
                      entry_ts=datetime.now(timezone.utc), entry_price=price,
                      quantity=quantity, stop_loss=stop_loss, take_profit=take_profit,
                      fees=fee, risk_pct=risk_pct, entry_reason=entry_reason,
                      status="open", entry_context=entry_context)
            s.add(t)
            s.commit()
            trade_id = t.id
        pos = OpenPosition(trade_id, client_order_id, symbol, direction, quantity,
                           price, stop_loss, take_profit,
                           datetime.now(timezone.utc), strategy, risk_pct, fee,
                           initial_stop=stop_loss, leverage=leverage,
                           margin=margin, liq_price=liq_price)
        self.positions[client_order_id] = pos
        self.trades_today += 1
        sl = _sleeve_of(strategy)
        if sl is not None:
            self.sleeve_today[sl] = self.sleeve_today.get(sl, 0) + 1
            if sl == "explore":
                self.explore_today += 1
        return pos

    def record_exit(self, client_order_id: str, price: float, fee: float,
                    reason: str, exit_context: dict | None = None) -> float | None:
        pos = self.positions.pop(client_order_id, None)
        if pos is None:
            log.warning("exit for unknown position %s", client_order_id)
            return None
        d = price - pos.entry_price
        if pos.direction == "SHORT":
            d = -d
        pl = d * pos.quantity - pos.entry_fee - fee
        self.equity += pl
        with self.db.session() as s:
            t = s.execute(select(Trade).where(
                Trade.client_order_id == client_order_id)).scalar_one_or_none()
            if t is not None:
                t.exit_ts = datetime.now(timezone.utc)
                t.exit_price = price
                t.pl = pl
                t.fees = pos.entry_fee + fee
                t.exit_reason = reason
                t.status = "closed"
                t.exit_context = exit_context
                s.commit()
        return pl

    def mark_price(self, symbol: str, price: float) -> None:
        for pos in self.positions.values():
            if pos.symbol == symbol:
                pos.last_price = price

    # --- views ------------------------------------------------------------
    def account_state(self) -> AccountState:
        self._roll_day()
        open_risk = sum(p.risk_pct for p in self.positions.values())
        trial = _explore_names()
        main = [p for p in self.positions.values() if p.strategy not in trial]
        return AccountState(equity=self.equity, day_start_equity=self.day_start_equity,
                            open_positions=len(main),
                            trades_today=self.trades_today, open_risk_pct=open_risk,
                            open_notional=self.invested_total(),
                            open_long=sum(1 for p in main if p.direction == "LONG"),
                            open_short=sum(1 for p in main if p.direction == "SHORT"),
                            explore_positions=sum(1 for p in self.positions.values()
                                                  if _sleeve_of(p.strategy) == "explore"),
                            explore_today=self.explore_today,
                            sleeve_open=self._sleeve_open(),
                            sleeve_today=dict(self.sleeve_today))

    def _sleeve_open(self) -> dict:
        out: dict = {}
        for p in self.positions.values():
            sl = _sleeve_of(p.strategy)
            if sl is not None:
                out[sl] = out.get(sl, 0) + 1
        return out

    def invested_total(self) -> float:
        return sum(p.invested for p in self.positions.values())

    def explore_stats(self, sleeve: str = "explore") -> dict:
        """Result of one trial sleeve, NET of fees."""
        from ..strategy.futures import sleeve_names
        names = sleeve_names(sleeve)
        with self.db.session() as s:
            rows = s.execute(select(Trade.pl, Trade.fees).where(
                Trade.status == "closed", Trade.mode == self.mode,
                Trade.strategy.in_(names))).all()
        pls = [r[0] or 0.0 for r in rows]
        fees = sum(r[1] or 0.0 for r in rows)
        pos = sum(p for p in pls if p > 0)
        neg = -sum(p for p in pls if p <= 0)
        return {"trades": len(pls), "net_pl": round(sum(pls), 2),
                "fees": round(fees, 2), "gross_pl": round(sum(pls) + fees, 2),
                "profit_factor": round(pos / neg, 2) if neg > 0 else (None if not pos else 99.0),
                "open": sum(1 for p in self.positions.values() if p.strategy in names),
                "today": self.sleeve_today.get(sleeve, 0)}

    def stats(self) -> dict:
        """Win rate / profit factor / max drawdown from closed trades."""
        with self.db.session() as s:
            pls = s.execute(select(Trade.pl).where(
                Trade.status == "closed", Trade.mode == self.mode)
                .order_by(Trade.id)).scalars().all()
        pls = [p or 0.0 for p in pls]
        wins = [p for p in pls if p > 0]
        losses = [-p for p in pls if p <= 0]
        eq, peak, maxdd = self.start_equity, self.start_equity, 0.0
        for p in pls:
            eq += p
            peak = max(peak, eq)
            if peak > 0:
                maxdd = max(maxdd, (peak - eq) / peak * 100.0)
        return {
            "closed_trades": len(pls),
            "win_rate": round(len(wins) / len(pls) * 100.0, 1) if pls else None,
            "profit_factor": round(sum(wins) / sum(losses), 2)
            if losses and sum(losses) > 0 else (None if not wins else 99.0),
            "max_drawdown_pct": round(maxdd, 2) if pls else None,
        }

    def snapshot(self) -> dict:
        self._roll_day()
        day_pl = self.equity - self.day_start_equity
        return {
            **self.stats(),
            "equity": round(self.equity, 2),
            "day_pl": round(day_pl, 2),
            "day_pl_pct": round(day_pl / self.day_start_equity * 100.0, 3)
            if self.day_start_equity else 0.0,
            "total_pl": round(self.equity - self.start_equity, 2),
            "trades_today": self.trades_today,
            "invested_total": round(self.invested_total(), 2),
            "open_positions": [
                {"client_order_id": p.client_order_id,
                 "symbol": p.symbol, "direction": p.direction,
                 "invested": round(p.invested, 2),
                 "notional": round(p.notional, 2),
                 "leverage": p.leverage, "liq_price": p.liq_price,
                 "quantity": p.quantity, "entry": p.entry_price,
                 "stop_loss": p.stop_loss, "take_profit": p.take_profit,
                 "strategy": p.strategy,
                 "opened_at": p.opened_at.isoformat(),
                 "last_price": p.last_price,
                 "unrealized_pl": round(p.unrealized_pl, 2)
                 if p.unrealized_pl is not None else None}
                for p in self.positions.values()],
        }

    def trade_history(self, limit: int = 100) -> list[dict]:
        with self.db.session() as s:
            rows = s.execute(select(Trade).where(Trade.status == "closed",
                                                 Trade.mode == self.mode)
                             .order_by(Trade.id.desc()).limit(limit)).scalars().all()
        return [{"symbol": t.symbol, "direction": t.direction, "strategy": t.strategy,
                 "entry_ts": t.entry_ts.isoformat() if t.entry_ts else None,
                 "exit_ts": t.exit_ts.isoformat() if t.exit_ts else None,
                 "entry": t.entry_price, "exit": t.exit_price, "pl": t.pl,
                 "fees": t.fees, "exit_reason": t.exit_reason,
                 "entry_reason": t.entry_reason} for t in rows]
