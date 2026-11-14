"""Backtest core: replays history through the real strategy/decision stack.

Two outputs per run:
  * signal records - EVERY signal any strategy produced (with features and
    TP-before-SL outcome): the ML training dataset, unbiased by thresholds
  * trade records  - signals that passed the decision threshold + risk
    checks, executed sequentially with costs: the performance metrics

Simulation rules (deliberately conservative, documented):
  * outcomes walk forward over CLOSED bars only (no look-ahead)
  * if SL and TP are both inside one bar's range -> counted as SL (worst case)
  * entry at signal bar close +/- slippage; fees charged on both sides
  * one open position per symbol in the equity simulation
  * timeout after ``max_hold_bars`` -> exit at close (outcome 'timeout')
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..analysis.regime import Regime, detect_regime
from ..decision import DecisionEngine
from ..marketdata.model import Candle, Timeframe
from ..risk import AccountState, RiskSupervisor
from ..strategy.base import MarketContext, Signal
from ..ta.features import FeatureSet, compute_features
from .history import resample

log = logging.getLogger(__name__)

M5, M15, H1 = Timeframe.M5, Timeframe.M15, Timeframe.H1


@dataclass(slots=True)
class SignalRecord:
    signal: Signal
    bar_index: int
    ts: int
    regime: Regime
    score: float
    features: dict            # M5 FeatureSet snapshot (ML input)
    outcome: str = "open"     # win | loss | timeout | open
    exit_ts: int | None = None
    bars_held: int = 0
    pl_r: float = 0.0         # result in R multiples (before costs)


@dataclass(slots=True)
class TradeRecord:
    signal: SignalRecord
    quantity: float
    pl: float                 # currency, after costs
    fees: float
    equity_after: float


@dataclass(slots=True)
class BacktestResult:
    symbol: str
    signals: list[SignalRecord] = field(default_factory=list)
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    bars: int = 0

    @property
    def dataset(self) -> list[SignalRecord]:
        """Signals with a definitive TP/SL outcome (ML training rows)."""
        return [s for s in self.signals if s.outcome in ("win", "loss")]


class Backtester:
    def __init__(self, decision_engine: DecisionEngine | None = None,
                 start_equity: float = 10_000.0, fee_pct: float = 0.1,
                 slippage_pct: float = 0.02, max_hold_bars: int = 96,
                 signal_cooldown_bars: int = 6, warmup_bars: int = 250,
                 m5_window: int = 300, htf_window: int = 120) -> None:
        self.engine = decision_engine or DecisionEngine(RiskSupervisor())
        self.start_equity = start_equity
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.max_hold_bars = max_hold_bars
        self.cooldown = signal_cooldown_bars
        self.warmup = warmup_bars
        self.m5_window = m5_window
        self.htf_window = htf_window

    # --- outcome simulation ------------------------------------------------
    def simulate_outcome(self, sig: Signal, future: list[Candle]) -> tuple[str, int, float]:
        """Walk closed future bars; return (outcome, bars_held, pl_in_R)."""
        risk = sig.risk_per_unit
        for i, bar in enumerate(future[: self.max_hold_bars], start=1):
            if sig.direction == "LONG":
                hit_sl = bar.low <= sig.stop_loss
                hit_tp = bar.high >= sig.take_profit
            else:
                hit_sl = bar.high >= sig.stop_loss
                hit_tp = bar.low <= sig.take_profit
            if hit_sl:            # SL wins ties: conservative
                return "loss", i, -1.0
            if hit_tp:
                return "win", i, sig.rr
        if len(future) < self.max_hold_bars:
            return "open", len(future), 0.0
        last = future[self.max_hold_bars - 1]
        diff = last.close - sig.entry if sig.direction == "LONG" else sig.entry - last.close
        return "timeout", self.max_hold_bars, diff / risk if risk > 0 else 0.0

    # --- main loop ----------------------------------------------------------
    def run(self, symbol: str, m5: list[Candle]) -> BacktestResult:
        res = BacktestResult(symbol=symbol, bars=len(m5))
        if len(m5) <= self.warmup:
            return res
        m15_all = resample(m5, M15)
        h1_all = resample(m5, H1)
        m15_idx = 0
        h1_idx = 0

        equity = self.start_equity
        day_start_equity = equity
        current_day: int | None = None
        pos_open_until = -1          # bar index until which a position is open
        trades_today = 0
        last_fired: dict[tuple[str, str], int] = {}

        for i in range(self.warmup, len(m5)):
            bar = m5[i]
            bar_end = bar.ts + M5.seconds

            day = bar.ts // 86400
            if day != current_day:
                current_day = day
                day_start_equity = equity
                trades_today = 0

            # advance closed higher-TF pointers (bars fully closed by bar_end)
            while m15_idx < len(m15_all) and m15_all[m15_idx].ts + M15.seconds <= bar_end:
                m15_idx += 1
            while h1_idx < len(h1_all) and h1_all[h1_idx].ts + H1.seconds <= bar_end:
                h1_idx += 1

            w5 = m5[max(0, i + 1 - self.m5_window): i + 1]
            w15 = m15_all[max(0, m15_idx - self.htf_window): m15_idx]
            w1h = h1_all[max(0, h1_idx - self.htf_window): h1_idx]

            features: dict[Timeframe, FeatureSet] = {}
            f5 = compute_features(w5)
            if f5 is None:
                continue
            features[M5] = f5
            f15 = compute_features(w15)
            if f15 is not None:
                features[M15] = f15
            f1h = compute_features(w1h)
            if f1h is not None:
                features[H1] = f1h

            regime = detect_regime(f15, f1h)
            ctx = MarketContext(symbol=symbol, now_ts=bar_end,
                                candles={M5: w5, M15: w15, H1: w1h},
                                features=features, regime=regime)

            account = AccountState(
                equity=equity, day_start_equity=day_start_equity,
                open_positions=1 if i <= pos_open_until else 0,
                trades_today=trades_today)
            decision = self.engine.evaluate(ctx, account)

            future = m5[i + 1:]
            for scored in decision.evaluated:
                sig = scored.signal
                key = (sig.strategy, sig.direction)
                if i - last_fired.get(key, -10**9) < self.cooldown:
                    continue
                last_fired[key] = i
                outcome, held, pl_r = self.simulate_outcome(sig, future)
                res.signals.append(SignalRecord(
                    signal=sig, bar_index=i, ts=bar.ts, regime=regime,
                    score=scored.score, features=f5.as_dict(),
                    outcome=outcome, bars_held=held, pl_r=pl_r,
                    exit_ts=m5[min(i + held, len(m5) - 1)].ts))

            # sequential equity simulation for entered trades
            if decision.verdict == "enter" and i > pos_open_until:
                best = decision.best.signal
                verdict = decision.risk
                outcome, held, pl_r = self.simulate_outcome(best, future)
                if outcome == "open":
                    continue
                qty = verdict.quantity
                entry_px = best.entry * (1 + self.slippage_pct / 100.0 *
                                         (1 if best.direction == "LONG" else -1))
                gross = pl_r * abs(best.entry - best.stop_loss) * qty
                notional = entry_px * qty
                fees = notional * self.fee_pct / 100.0 * 2
                slip_cost = abs(entry_px - best.entry) * qty
                pl = gross - fees - slip_cost
                equity += pl
                trades_today += 1
                pos_open_until = i + held
                rec = SignalRecord(
                    signal=best, bar_index=i, ts=bar.ts, regime=regime,
                    score=decision.best.score, features=f5.as_dict(),
                    outcome=outcome, bars_held=held, pl_r=pl_r,
                    exit_ts=m5[min(i + held, len(m5) - 1)].ts)
                res.trades.append(TradeRecord(
                    signal=rec, quantity=qty, pl=pl, fees=fees, equity_after=equity))
                res.equity_curve.append(equity)

        return res


def realistic_backtester(fee_venue: str = "binance", slippage_pct: float = 0.02,
                         allow_short: bool = False, **kw) -> Backtester:
    """Backtester with the SAME cost model and short rule as live/paper:
    venue taker fee per side, slippage, net-of-cost reward/risk checks."""
    from ..fees import round_trip_cost_pct, venue
    from ..risk import RiskLimits
    limits = RiskLimits(round_trip_cost_pct=round_trip_cost_pct(fee_venue, "taker", slippage_pct),
                        allow_short=allow_short, min_rr=1.3)
    return Backtester(decision_engine=DecisionEngine(RiskSupervisor(limits)),
                      fee_pct=venue(fee_venue).taker_pct, slippage_pct=slippage_pct, **kw)
