"""Risk Supervisor - the hard, deterministic safety layer (§14).

Pure code, no AI involvement, and the ONLY component allowed to size
positions or approve order submission. Every check that fails is a veto
with an explicit reason; vetoes are logged by the caller. These limits
cannot be bypassed by any strategy, model or LLM output.

All profitability checks are NET of trading costs: the round-trip fee +
slippage of the configured venue is subtracted from the reward and added
to the risk before the reward/risk ratio is compared and before the
position is sized. A setup whose take-profit does not clearly pay for the
fees is never taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .strategy.base import Signal


@dataclass(slots=True)
class RiskLimits:
    risk_per_trade_pct: float = 0.5        # % of trading capital risked per trade
    max_risk_per_trade_pct: float = 1.0    # absolute ceiling (config cannot exceed)
    daily_max_loss_pct: float = 2.0        # stop opening after this daily drawdown
    max_open_positions: int = 4
    max_daily_trades: int = 15
    min_rr: float = 1.2                    # minimum reward/risk AFTER costs
    max_spread_pct: float = 0.15           # reject if spread wider than this
    max_position_notional_pct: float = 25.0  # position size cap vs capital
    budget_eur: float | None = None        # max capital the bot may use (None = equity)
    max_position_eur: float | None = None  # max value of a single position
    round_trip_cost_pct: float = 0.0       # fees + slippage, entry AND exit, % of value
    allow_short: bool = True               # False on spot crypto venues

    def effective_risk_pct(self) -> float:
        return min(self.risk_per_trade_pct, self.max_risk_per_trade_pct)


@dataclass(slots=True)
class AccountState:
    equity: float
    day_start_equity: float
    open_positions: int = 0
    trades_today: int = 0
    open_risk_pct: float = 0.0   # sum of risk % currently at stake
    open_notional: float = 0.0   # value currently invested in open positions

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
    notional: float = 0.0
    cost_estimate: float = 0.0   # expected round-trip fees + slippage in EUR
    net_rr: float = 0.0

    def as_dict(self) -> dict:
        return {"approved": self.approved, "vetoes": self.vetoes,
                "quantity": self.quantity, "risk_amount": round(self.risk_amount, 2),
                "risk_pct": self.risk_pct, "notional": round(self.notional, 2),
                "cost_estimate": round(self.cost_estimate, 2),
                "net_rr": round(self.net_rr, 2)}


def net_reward_risk(signal: Signal, round_trip_cost_pct: float) -> tuple[float, float, float]:
    """(net_reward_per_unit, net_risk_per_unit, net_rr) after round-trip costs."""
    cost = signal.entry * round_trip_cost_pct / 100.0
    reward = abs(signal.take_profit - signal.entry) - cost
    risk = signal.risk_per_unit + cost
    rr = reward / risk if risk > 0 else 0.0
    return reward, risk, rr


class RiskSupervisor:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()
        self.halted: bool = False       # set by PAUSE / EMERGENCY STOP
        self.halt_reason: str = ""

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def resume(self) -> None:
        self.halted = False
        self.halt_reason = ""

    def capital(self, account: AccountState) -> float:
        """Capital the bot is allowed to trade with: equity capped by budget."""
        if self.limits.budget_eur is not None:
            return max(0.0, min(account.equity, self.limits.budget_eur))
        return account.equity

    def check(self, signal: Signal, account: AccountState,
              spread_pct: float | None = None) -> RiskVerdict:
        L = self.limits
        vetoes: list[str] = []

        if self.halted:
            vetoes.append(f"obchodovanie zastavené: {self.halt_reason or 'ručne'}")
        if not signal.valid():
            vetoes.append("neplatný signál: SL/TP nie sú na správnej strane vstupu")
        if signal.risk_per_unit <= 0:
            vetoes.append("chýba stop-loss alebo má nulovú vzdialenosť")
        if signal.direction == "SHORT" and not L.allow_short:
            vetoes.append("short nie je na spotovom trhu možný (iba LONG)")

        net_reward, net_risk, net_rr = net_reward_risk(signal, L.round_trip_cost_pct)
        if net_reward <= 0:
            vetoes.append(
                f"take-profit nepokryje poplatky ({L.round_trip_cost_pct:.2f} % za obchod)")
        elif net_rr < L.min_rr:
            vetoes.append(f"risk/reward po poplatkoch {net_rr:.2f} < minimum {L.min_rr}")

        if account.daily_pl_pct <= -L.daily_max_loss_pct:
            vetoes.append(
                f"denný limit straty dosiahnutý ({account.daily_pl_pct:.2f}% <= -{L.daily_max_loss_pct}%)")
        if account.open_positions >= L.max_open_positions:
            vetoes.append(f"max. počet otvorených pozícií ({account.open_positions})")
        if account.trades_today >= L.max_daily_trades:
            vetoes.append(f"max. počet obchodov za deň ({account.trades_today})")
        if spread_pct is not None and spread_pct > L.max_spread_pct:
            vetoes.append(f"spread {spread_pct:.3f}% nad limitom {L.max_spread_pct}%")

        capital = self.capital(account)
        if capital <= 0:
            vetoes.append("nulový obchodovateľný kapitál")

        risk_pct = L.effective_risk_pct()
        if account.open_risk_pct + risk_pct > L.daily_max_loss_pct:
            vetoes.append(
                f"otvorené riziko {account.open_risk_pct:.2f}% + nové {risk_pct:.2f}% "
                f"by prekročilo denný limit {L.daily_max_loss_pct}%")

        # budget left for a new position
        budget_left = None
        if L.budget_eur is not None:
            budget_left = min(L.budget_eur, account.equity) - account.open_notional
            if budget_left <= 0:
                vetoes.append("rozpočet bota je celý použitý v otvorených pozíciách")

        if vetoes:
            return RiskVerdict(approved=False, vetoes=vetoes, net_rr=net_rr)

        # size so that a stop-out INCLUDING costs loses exactly the risk amount
        risk_amount = capital * risk_pct / 100.0
        qty = risk_amount / net_risk
        caps = [capital * L.max_position_notional_pct / 100.0]
        if L.max_position_eur is not None:
            caps.append(L.max_position_eur)
        if budget_left is not None:
            caps.append(budget_left)
        max_notional = min(caps)
        if qty * signal.entry > max_notional:
            qty = max_notional / signal.entry
            risk_amount = qty * net_risk
        if qty <= 0:
            return RiskVerdict(approved=False, vetoes=["vypočítané množstvo je nulové"])

        notional = qty * signal.entry
        return RiskVerdict(approved=True, quantity=qty, risk_amount=risk_amount,
                           risk_pct=risk_pct, notional=notional,
                           cost_estimate=notional * L.round_trip_cost_pct / 100.0,
                           net_rr=net_rr)
