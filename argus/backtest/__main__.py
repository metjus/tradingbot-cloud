"""Backtest CLI: ``python -m argus.backtest --symbols BTC/USD --days 60``.

Reports in-sample / out-of-sample metrics (chronological 70/30 split),
per-strategy breakdown and an enter-threshold sensitivity sweep. Results
are printed and stored in the audit log.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from ..config import settings
from ..db.database import Database
from ..decision import DecisionEngine
from ..monitoring.logging_setup import setup_logging
from ..risk import RiskSupervisor
from .engine import Backtester
from .history import ensure_history
from .metrics import compute_stats, per_strategy_stats, signal_outcome_stats


def run_symbol(db: Database, symbol: str, days: int, thresholds: list[float]) -> dict:
    bars = asyncio.run(ensure_history(db, symbol, days))
    split = int(len(bars) * 0.7)
    out: dict = {"symbol": symbol, "bars": len(bars), "thresholds": {}}
    for th in thresholds:
        bt = Backtester(decision_engine=DecisionEngine(RiskSupervisor(), enter_threshold=th))
        res_is = bt.run(symbol, bars[:split])
        bt2 = Backtester(decision_engine=DecisionEngine(RiskSupervisor(), enter_threshold=th))
        res_oos = bt2.run(symbol, bars[split:])
        out["thresholds"][str(th)] = {
            "in_sample": compute_stats(res_is.trades, bt.start_equity).as_dict(),
            "out_of_sample": compute_stats(res_oos.trades, bt2.start_equity).as_dict(),
        }
        if th == thresholds[len(thresholds) // 2]:  # detail for the middle threshold
            out["per_strategy_oos"] = {k: v.as_dict() for k, v in
                                       per_strategy_stats(res_oos, bt2.start_equity).items()}
            out["signal_outcomes_oos"] = signal_outcome_stats(res_oos)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(prog="argus.backtest")
    ap.add_argument("--symbols", default="BTC/USD,ETH/USD")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--thresholds", default="60,70,80")
    args = ap.parse_args()

    settings.ensure_dirs()
    setup_logging(settings.data_dir, logging.WARNING)
    db = Database(settings.db_path)
    thresholds = [float(t) for t in args.thresholds.split(",")]
    report = {}
    for sym in [s.strip() for s in args.symbols.split(",") if s.strip()]:
        report[sym] = run_symbol(db, sym, args.days, thresholds)
    db.audit("system", "Backtest report", data=report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
