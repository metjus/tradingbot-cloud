"""Risk Supervisor - the hard, deterministic safety layer (§14).

Pure code, no AI involvement, and the ONLY component allowed to size
positions or approve order submission. Every check that fails is a veto
with an explicit reason; vetoes are logged by the caller. These limits
cannot be bypassed by any strategy, model or LLM output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .strategy.base import Signal


@dataclass(slots=True)
class RiskLimits:
    risk_per_trade_pct: float = 0.5        # % of equity risked per trade
    max_risk_per_trade_pct: float = 1.0    # absolute ceiling (config cannot exceed)
    daily_max_loss_pct: float = 2.0        # stop opening after this daily drawdown
    max_open_positions: int = 4
    max_daily_trades: int = 15
    min_rr: float = 1.2
    max_spread_pct: float = 0.15           # reject if spread wider than this
    max_position_notional_pct: float = 25.0  # position size cap vs equity

    def effective_risk_pct(self) -> float:
        return min(self.risk_per_trade_pct, self.max_risk_per_trade_pct)


@dataclass(slots=True)
class AccountState:
    equity: float
    day_start_equity: float
    open_positions: int = 0
    trades_today: int = 0
    open_risk_pct: float = 0.0  # sum of risk % currently at stake

    @property
    def daily_pl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return (self.equity - self.day_start_equity) / self.day_start_equity * 100.0


@dataclass(slots=True)
class RiskVerdict:
    approved: bool
    vetoes: list[str] = field(default_factory=list)
    quantity: float = 0.0
    risk_amount: float = 0.0
    risk_pct: float = 0.0

    def as_dict(self) -> dict:
        return {"approved": self.approved, "vetoes": self.vetoes,
                "quantity": self.quantity, "risk_amount": round(self.risk_amount, 2),
                "risk_pct": self.risk_pct}


class RiskSupervisor:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()
        self.halted: bool = False       # set by PAUSE / EMERGENCY STOP (phase 8+)
        self.halt_reason: str = ""

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""

    def check(self, signal: Signal, account: AccountState,
              spread_pct: float | None = None) -> RiskVerdict:
        L = self.limits
        vetoes: list[str] = []

        if self.halted:
            vetoes.append(f"trading halted: {self.halt_reason or 'manual halt'}")
        if not signal.valid():
            vetoes.append("invalid signal: SL/TP not on the correct side of entry")
        if signal.risk_per_unit <= 0:
            vetoes.append("missing or zero stop-loss distance")
        if signal.rr < L.min_rr:
            vetoes.append(f"risk/reward {signal.rr:.2f} below minimum {L.min_rr}")
        if account.daily_pl_pct <= -L.daily_max_loss_pct:
            vetoes.append(
                f"daily loss limit reached ({account.daily_pl_pct:.2f}% <= -{L.daily_max_loss_pct}%)")
        if account.open_positions >= L.max_open_positions:
            vetoes.append(f"max open positions reached ({account.open_positions})")
        if account.trades_today >= L.max_daily_trades:
            vetoes.append(f"max daily trades reached ({account.trades_today})")
        if spread_pct is not None and spread_pct > L.max_spread_pct:
            vetoes.append(f"spread {spread_pct:.3f}% above limit {L.max_spread_pct}%")
        if account.equity <= 0:
            vetoes.append("non-positive equity")

        risk_pct = L.effective_risk_pct()
        # never let total open risk exceed the daily loss budget
        if account.open_risk_pct + risk_pct > L.daily_max_loss_pct:
            vetoes.append(
                f"open risk {account.open_risk_pct:.2f}% + new {risk_pct:.2f}% "
                f"would exceed daily budget {L.daily_max_loss_pct}%")

        if vetoes:
            return RiskVerdict(approved=False, vetoes=vetoes)

        risk_amount = account.equity * risk_pct / 100.0
        qty = risk_amount / signal.risk_per_unit
        # cap notional exposure
        max_notional = account.equity * L.max_position_notional_pct / 100.0
        notional = qty * signal.entry
        if notional > max_notional:
            qty = max_notional / signal.entry
            risk_amount = qty * signal.risk_per_unit
        if qty <= 0:
            return RiskVerdict(approved=False, vetoes=["calculated quantity is zero"])

        return RiskVerdict(approved=True, quantity=qty,
                           risk_amount=risk_amount, risk_pct=risk_pct)
