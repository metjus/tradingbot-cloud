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
    max_risk_per_trade_pct: float = 2.0    # absolute ceiling (config cannot exceed)
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
    leverage: float = 1.0                  # futures: notional / margin
    max_leverage: float = 1.0              # venue/regulatory cap
    maintenance_margin_pct: float = 0.0    # futures: liquidation threshold
    max_aligned_positions: int = 3         # positions in the SAME direction
                                           # (crypto moves together - this caps
                                           # stacking one big correlated bet)
    max_explore_positions: int = 1         # trial (exploration) positions at once
    max_explore_daily: int = 3             # trial entries per day (fee budget)
    max_explore_cost_share: float = 0.05   # trial: fees may eat at most 5 % of R
    #: per-sleeve caps (open positions, entries per day, max fee share of R)
    sleeve_caps: dict = field(default_factory=lambda: {
        "ai": (1, 12, 0.15),
        "active": (30, 60, 0.10)})

    def effective_risk_pct(self) -> float:
        return min(self.risk_per_trade_pct, self.max_risk_per_trade_pct)


@dataclass(slots=True)
class AccountState:
    equity: float
    day_start_equity: float
    open_positions: int = 0
    trades_today: int = 0
    open_risk_pct: float = 0.0   # sum of risk % currently at stake
    open_notional: float = 0.0   # margin currently blocked in open positions
    open_long: int = 0           # open LONG positions (correlation cap)
    open_short: int = 0
    explore_positions: int = 0   # open trial positions (not in the caps above)
    explore_today: int = 0
    sleeve_open: dict = field(default_factory=dict)    # sleeve -> open positions
    sleeve_today: dict = field(default_factory=dict)   # sleeve -> entries today

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
    margin: float = 0.0          # money blocked (notional / leverage)
    leverage: float = 1.0
    liq_price: float | None = None   # estimated isolated-margin liquidation
    cost_estimate: float = 0.0   # expected round-trip fees + slippage in EUR
    net_rr: float = 0.0

    def as_dict(self) -> dict:
        return {"approved": self.approved, "vetoes": self.vetoes,
                "quantity": self.quantity, "risk_amount": round(self.risk_amount, 2),
                "risk_pct": self.risk_pct, "notional": round(self.notional, 2),
                "margin": round(self.margin, 2), "leverage": self.leverage,
                "liq_price": self.liq_price,
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
        if signal.exploration:
            # trial positions have their own small caps and must not crowd
            # out the validated strategy
            sleeve = signal.sleeve or "explore"
            if sleeve == "explore":
                max_open, max_day, max_cost = (L.max_explore_positions, L.max_explore_daily,
                                               L.max_explore_cost_share)
                n_open = account.sleeve_open.get(sleeve, account.explore_positions)
                n_day = account.sleeve_today.get(sleeve, account.explore_today)
                label = "skúšobný režim"
            else:
                max_open, max_day, max_cost = L.sleeve_caps.get(sleeve, (1, 3, 0.05))
                n_open = account.sleeve_open.get(sleeve, 0)
                n_day = account.sleeve_today.get(sleeve, 0)
                label = {"ai": "AI trader", "active": "aktívny režim"}.get(sleeve, sleeve)
            if n_open >= max_open:
                vetoes.append(f"{label}: max. {max_open} pozície naraz")
            if n_day >= max_day:
                vetoes.append(f"{label}: max. {max_day} vstupov za deň (strop na poplatky)")
            cost_share = signal.entry * L.round_trip_cost_pct / 100.0 / signal.risk_per_unit \
                if signal.risk_per_unit > 0 else 1.0
            if cost_share > max_cost:
                vetoes.append(f"{label}: poplatky by zjedli {cost_share * 100:.0f} % "
                              f"rizika (limit {max_cost * 100:.0f} %)")
        else:
            if account.open_positions >= L.max_open_positions:
                vetoes.append(f"max. počet otvorených pozícií ({account.open_positions})")
            aligned = account.open_long if signal.direction == "LONG" else account.open_short
            if aligned >= L.max_aligned_positions:
                vetoes.append(
                    f"už {aligned} pozícií v smere {signal.direction} - krypto sa hýbe "
                    f"spolu, ďalšia by len zväčšila tú istú stávku")
        if account.trades_today >= L.max_daily_trades:
            vetoes.append(f"max. počet obchodov za deň ({account.trades_today})")
        if spread_pct is not None and spread_pct > L.max_spread_pct:
            vetoes.append(f"spread {spread_pct:.3f}% nad limitom {L.max_spread_pct}%")

        capital = self.capital(account)
        if capital <= 0:
            vetoes.append("nulový obchodovateľný kapitál")

        risk_pct = L.effective_risk_pct() * max(0.0, min(1.0, signal.risk_scale))
        if account.open_risk_pct + risk_pct > L.daily_max_loss_pct:
            vetoes.append(
                f"otvorené riziko {account.open_risk_pct:.2f}% + nové {risk_pct:.2f}% "
                f"by prekročilo denný limit {L.daily_max_loss_pct}%")

        lev = max(1.0, min(L.leverage, L.max_leverage))

        # futures: the estimated liquidation price must lie far BEHIND the
        # stop-loss, so the stop always fires first (isolated margin)
        liq_price: float | None = None
        if lev > 1.0 or L.maintenance_margin_pct > 0:
            move = (1.0 / lev - L.maintenance_margin_pct / 100.0)
            if move <= 0:
                vetoes.append("páka je nad limitom burzy")
            else:
                sgn = 1.0 if signal.direction == "LONG" else -1.0
                liq_price = signal.entry * (1.0 - sgn * move)
                stop_dist = abs(signal.entry - signal.stop_loss)
                liq_dist = abs(signal.entry - liq_price)
                if stop_dist > 0.7 * liq_dist:
                    vetoes.append(
                        f"stop-loss je príliš blízko likvidačnej ceny pri páke {lev:g}x "
                        f"(stop {stop_dist / signal.entry * 100:.1f}% vs likvidácia "
                        f"{liq_dist / signal.entry * 100:.1f}%) - zníž páku alebo preskoč")

        # budget left for a new position (budget binds the MARGIN, i.e. the
        # money actually blocked; with leverage the notional may be larger)
        budget_left = None
        if L.budget_eur is not None:
            budget_left = min(L.budget_eur, account.equity) - account.open_notional
            if budget_left <= 0:
                vetoes.append("rozpočet bota je celý použitý v otvorených pozíciách")

        if vetoes:
            return RiskVerdict(approved=False, vetoes=vetoes, net_rr=net_rr,
                               leverage=lev, liq_price=liq_price)

        # size so that a stop-out INCLUDING costs loses exactly the risk amount
        risk_amount = capital * risk_pct / 100.0
        qty = risk_amount / net_risk
        caps = [capital * L.max_position_notional_pct / 100.0 * lev]
        if L.max_position_eur is not None:
            caps.append(L.max_position_eur * lev)      # per-position cap on margin
        if budget_left is not None:
            caps.append(budget_left * lev)
        max_notional = min(caps)
        if qty * signal.entry > max_notional:
            qty = max_notional / signal.entry
            risk_amount = qty * net_risk
        if qty <= 0:
            return RiskVerdict(approved=False, vetoes=["vypočítané množstvo je nulové"])

        notional = qty * signal.entry
        return RiskVerdict(approved=True, quantity=qty, risk_amount=risk_amount,
                           risk_pct=risk_pct, notional=notional,
                           margin=notional / lev, leverage=lev, liq_price=liq_price,
                           cost_estimate=notional * L.round_trip_cost_pct / 100.0,
                           net_rr=net_rr)
