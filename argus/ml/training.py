"""Training pipeline with walk-forward validation and promotion gates.

Anti-overfitting discipline (§9D):
  * chronological walk-forward folds - training data strictly precedes
    validation data, with an embargo gap so an open trade's outcome can
    never leak across the boundary
  * validation criteria measured ONLY on out-of-sample folds
  * a candidate is registered, never auto-activated; it must beat the
    gates below, and (phase 8) survive a paper-test window
  * minimum sample sizes enforced; no training on "a few recent trades"
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import numpy as np
from sklearn.metrics import roc_auc_score

from ..backtest.engine import Backtester
from ..backtest.history import ensure_history
from ..backtest.metrics import signal_outcome_stats
from ..db.database import Database
from .dataset import Dataset, build_dataset
from .model import SetupScorer
from .registry import ModelRegistry

log = logging.getLogger(__name__)

EMBARGO_SECONDS = 8 * 3600  # > max_hold window: no outcome overlap across folds


@dataclass(slots=True)
class ValidationGates:
    min_samples: int = 300
    min_folds: int = 4
    min_mean_auc: float = 0.55
    min_fold_auc: float = 0.50     # no fold may be worse than random


@dataclass(slots=True)
class WalkForwardReport:
    n_samples: int = 0
    base_win_rate: float = 0.0
    fold_aucs: list[float] = field(default_factory=list)
    mean_auc: float = 0.0
    top_decile_win_rate: float = 0.0   # win rate of highest-p validation signals
    passed: bool = False
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"n_samples": self.n_samples,
                "base_win_rate": round(self.base_win_rate, 4),
                "fold_aucs": [round(a, 4) for a in self.fold_aucs],
                "mean_auc": round(self.mean_auc, 4),
                "top_decile_win_rate": round(self.top_decile_win_rate, 4),
                "passed": self.passed, "reasons": self.reasons}


def walk_forward_splits(ts: np.ndarray, n_folds: int,
                        embargo_s: int = EMBARGO_SECONDS):
    """Yield (train_idx, val_idx): chronological, embargoed, growing window."""
    n = len(ts)
    fold_edges = np.linspace(0, n, n_folds + 2, dtype=int)  # first chunk = initial train
    for k in range(1, n_folds + 1):
        val_lo, val_hi = fold_edges[k], fold_edges[k + 1]
        if val_hi - val_lo < 10:
            continue
        cutoff_ts = ts[val_lo] - embargo_s
        train_idx = np.where(ts[:val_lo] <= cutoff_ts)[0]
        if len(train_idx) < 50:
            continue
        yield train_idx, np.arange(val_lo, val_hi)


def walk_forward_validate(ds: Dataset, gates: ValidationGates | None = None
                          ) -> WalkForwardReport:
    gates = gates or ValidationGates()
    rep = WalkForwardReport(n_samples=len(ds))
    if len(ds) == 0:
        rep.reasons.append("empty dataset")
        return rep
    rep.base_win_rate = float(ds.y.mean())
    if len(ds) < gates.min_samples:
        rep.reasons.append(f"only {len(ds)} samples < {gates.min_samples}")

    top_probs: list[float] = []
    top_wins: list[int] = []
    for train_idx, val_idx in walk_forward_splits(ds.ts, gates.min_folds):
        y_tr, y_va = ds.y[train_idx], ds.y[val_idx]
        if len(np.unique(y_tr)) < 2 or len(np.unique(y_va)) < 2:
            continue
        m = SetupScorer()
        m.fit(ds.X[train_idx], y_tr)
        p = m.predict_proba(ds.X[val_idx])
        rep.fold_aucs.append(float(roc_auc_score(y_va, p)))
        k = max(1, len(p) // 10)
        order = np.argsort(p)[::-1][:k]
        top_probs.extend(p[order])
        top_wins.extend(y_va[order])

    if rep.fold_aucs:
        rep.mean_auc = float(np.mean(rep.fold_aucs))
    if top_wins:
        rep.top_decile_win_rate = float(np.mean(top_wins))

    if len(rep.fold_aucs) < gates.min_folds:
        rep.reasons.append(f"only {len(rep.fold_aucs)} usable folds < {gates.min_folds}")
    if rep.mean_auc < gates.min_mean_auc:
        rep.reasons.append(f"mean AUC {rep.mean_auc:.3f} < {gates.min_mean_auc}")
    if rep.fold_aucs and min(rep.fold_aucs) < gates.min_fold_auc:
        rep.reasons.append(f"worst fold AUC {min(rep.fold_aucs):.3f} < {gates.min_fold_auc}")
    rep.passed = not rep.reasons
    return rep


async def train_from_history(db: Database, registry: ModelRegistry,
                             symbols: list[str], days: int = 60,
                             gates: ValidationGates | None = None,
                             extra_records: list | None = None,
                             fee_venue: str = "binance", slippage_pct: float = 0.02,
                             allow_short: bool = False) -> dict:
    """Full pipeline: history -> backtest signals (+ lived trade records) ->
    dataset -> walk-forward -> register candidate (status = validation)."""
    from ..backtest.engine import realistic_backtester
    from ..fees import venue as venue_info
    bt = realistic_backtester(fee_venue, slippage_pct, allow_short)
    futures = venue_info(fee_venue).futures
    all_records = list(extra_records or [])
    per_symbol = {}
    profiles: dict = {}
    btc_h1 = None
    if futures:
        from ..backtest.history import ensure_history_futures
        try:
            btc_h1 = await ensure_history_futures(db, "BTC/USD", days)
        except Exception as exc:
            log.warning("BTC history for the trend filter failed: %s", exc)
    for sym in symbols:
        if futures:
            from ..backtest.history import ensure_history_futures
            bars = await ensure_history_futures(db, sym, days)
            result = bt.run_htf(sym, bars, btc_h1=btc_h1)
        else:
            bars = await ensure_history(db, sym, days)
            result = bt.run(sym, bars)
        all_records.extend(result.signals)
        per_symbol[sym] = {"bars": result.bars, **signal_outcome_stats(result)}
        profiles[sym] = calibration_profile(
            result.signals, bt.engine.risk.limits.round_trip_cost_pct)
        log.info("backtest %s: %d bars, %d signals (%d labelled)",
                 sym, result.bars, len(result.signals), len(result.dataset))

    ds = build_dataset(all_records)
    report = walk_forward_validate(ds, gates)

    out = {"per_symbol": per_symbol, "validation": report.as_dict(), "version": None,
           "profiles": profiles}
    save_profiles(db, profiles)
    if len(ds) == 0:
        db.audit("ml", "Training skipped: no labelled signals", "warning", out)
        return out

    final = SetupScorer()
    final.fit(ds.X, ds.y)
    status = "validated" if report.passed else "rejected"
    version = registry.register(final, task="P(TP before SL)",
                                metrics=report.as_dict(), status=status)
    out["version"] = version
    out["status"] = status
    db.audit("ml", f"Training finished: setup-scorer v{version} ({status})",
             "info" if report.passed else "warning", out["validation"])
    return out


def calibration_profile(signals, round_trip_cost_pct: float) -> dict:
    """Per-strategy result of every backtest signal for one coin, in R
    multiples NET of the round-trip cost (fees + slippage)."""
    from collections import defaultdict
    acc: dict = defaultdict(lambda: {"n": 0, "wins": 0, "pos": 0.0, "neg": 0.0, "sum": 0.0})
    for rec in signals:
        if rec.outcome not in ("win", "loss", "timeout"):
            continue
        risk = rec.signal.risk_per_unit
        if risk <= 0:
            continue
        cost_r = rec.signal.entry * round_trip_cost_pct / 100.0 / risk
        net = rec.pl_r - cost_r
        a = acc[rec.signal.strategy]
        a["n"] += 1
        a["sum"] += net
        if net > 0:
            a["wins"] += 1
            a["pos"] += net
        else:
            a["neg"] += -net
    out = {}
    for strat, a in acc.items():
        out[strat] = {"n": a["n"],
                      "win_rate": round(a["wins"] / a["n"], 3) if a["n"] else 0.0,
                      "profit_factor_r": round(a["pos"] / a["neg"], 3) if a["neg"] > 0 else None,
                      "expectancy_r": round(a["sum"] / a["n"], 3) if a["n"] else 0.0}
    return out


PROFILES_KEY = "symbol_profiles"


def save_profiles(db: Database, profiles: dict) -> None:
    from ..db.schema import AppSetting
    with db.session() as s:
        row = s.get(AppSetting, PROFILES_KEY)
        merged = dict(row.value) if row is not None else {}
        merged.update(profiles)
        if row is None:
            s.add(AppSetting(key=PROFILES_KEY, value=merged))
        else:
            row.value = merged
        s.commit()


def load_profiles(db: Database) -> dict:
    from ..db.schema import AppSetting
    with db.session() as s:
        row = s.get(AppSetting, PROFILES_KEY)
        return dict(row.value) if row is not None else {}
