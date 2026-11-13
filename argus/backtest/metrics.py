"""Performance metrics for backtest and (later) paper/live trading."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .engine import BacktestResult, TradeRecord


@dataclass(slots=True)
class PerfStats:
    n_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0      # mean P/L per trade
    avg_win: float = 0.0
    avg_loss: float = 0.0
    total_pl: float = 0.0
    max_drawdown_pct: float = 0.0
    avg_bars_held: float = 0.0

    def as_dict(self) -> dict:
        return {k: round(getattr(self, k), 4) for k in (
            "n_trades", "wins", "losses", "win_rate", "profit_factor",
            "expectancy", "avg_win", "avg_loss", "total_pl",
            "max_drawdown_pct", "avg_bars_held")}


def compute_stats(trades: list[TradeRecord], start_equity: float) -> PerfStats:
    s = PerfStats()
    s.n_trades = len(trades)
    if not trades:
        return s
    wins = [t.pl for t in trades if t.pl > 0]
    losses = [t.pl for t in trades if t.pl <= 0]
    s.wins, s.losses = len(wins), len(losses)
    s.win_rate = s.wins / s.n_trades
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    s.profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    s.total_pl = gross_win - gross_loss
    s.expectancy = s.total_pl / s.n_trades
    s.avg_win = gross_win / s.wins if s.wins else 0.0
    s.avg_loss = -gross_loss / s.losses if s.losses else 0.0
    s.avg_bars_held = sum(t.signal.bars_held for t in trades) / s.n_trades

    equity = start_equity
    peak = equity
    max_dd = 0.0
    for t in trades:
        equity += t.pl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak * 100.0)
    s.max_drawdown_pct = max_dd
    return s


def per_strategy_stats(result: BacktestResult, start_equity: float) -> dict[str, PerfStats]:
    groups: dict[str, list[TradeRecord]] = defaultdict(list)
    for t in result.trades:
        groups[t.signal.signal.strategy].append(t)
    return {name: compute_stats(ts, start_equity) for name, ts in groups.items()}


def signal_outcome_stats(result: BacktestResult) -> dict:
    """Win rate of raw signals (TP-before-SL), overall and per strategy -
    the base rates the ML model must beat."""
    rows = result.dataset
    per: dict[str, list[int]] = defaultdict(list)
    for r in rows:
        per[r.signal.strategy].append(1 if r.outcome == "win" else 0)
    out = {
        "n_signals": len(rows),
        "base_win_rate": (sum(1 for r in rows if r.outcome == "win") / len(rows)) if rows else 0.0,
        "per_strategy": {k: {"n": len(v), "win_rate": sum(v) / len(v)} for k, v in per.items()},
    }
    return out
