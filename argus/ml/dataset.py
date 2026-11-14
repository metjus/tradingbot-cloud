"""Feature vectorisation of signal records for the setup scorer.

Only scale-free features (ratios, oscillator values, ATR-normalised
distances) - never absolute prices - so one model generalises across
symbols. Column order is fixed and versioned: a model stores the schema
version it was trained with and refuses to score mismatched inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from ..analysis.regime import Regime
from ..backtest.engine import SignalRecord

FEATURE_SCHEMA_VERSION = 1

STRATEGIES = ["trend_following", "momentum", "breakout", "pullback",
              "vwap_reversal", "support_resistance", "volatility_breakout",
              "mean_reversion"]
REGIMES = [r.value for r in Regime]

BASE_COLUMNS = [
    "rsi14", "adx14", "atr_pct", "dist_ema9_atr", "dist_ema21_atr",
    "dist_vwap_atr", "macd_hist_atr", "bb_pos", "bb_width_pctile",
    "vol_ratio", "hour_sin", "hour_cos", "direction", "rr", "confidence",
]
COLUMNS = BASE_COLUMNS + [f"strat_{s}" for s in STRATEGIES] + [f"regime_{r}" for r in REGIMES]


def _nz(v, default: float = 0.0) -> float:
    return float(v) if v is not None else default


def vectorize(rec: SignalRecord) -> np.ndarray:
    f = rec.features
    atr = _nz(f.get("atr14"), 0.0)
    close = _nz(f.get("close"), 0.0)
    atr_pct = (atr / close * 100.0) if close else 0.0

    def dist(key: str) -> float:
        v = f.get(key)
        if v is None or atr <= 0:
            return 0.0
        return (close - v) / atr

    bb_up, bb_lo = f.get("bb_upper"), f.get("bb_lower")
    bb_pos = 0.0
    if bb_up is not None and bb_lo is not None and bb_up > bb_lo:
        bb_pos = (close - bb_lo) / (bb_up - bb_lo) * 2.0 - 1.0  # -1..1

    hour = datetime.fromtimestamp(rec.ts, tz=timezone.utc).hour
    row = [
        _nz(f.get("rsi14"), 50.0) / 100.0,
        _nz(f.get("adx14")) / 50.0,
        atr_pct,
        dist("ema9"), dist("ema21"), dist("vwap"),
        (_nz(f.get("macd_hist")) / atr) if atr > 0 else 0.0,
        bb_pos,
        _nz(f.get("bb_width_pctile"), 50.0) / 100.0,
        _nz(f.get("vol_ratio"), 1.0),
        np.sin(2 * np.pi * hour / 24.0), np.cos(2 * np.pi * hour / 24.0),
        1.0 if rec.signal.direction == "LONG" else -1.0,
        min(rec.signal.rr, 5.0),
        rec.signal.confidence,
    ]
    row += [1.0 if rec.signal.strategy == s else 0.0 for s in STRATEGIES]
    row += [1.0 if rec.regime.value == r else 0.0 for r in REGIMES]
    return np.asarray(row, dtype=np.float64)


@dataclass(slots=True)
class Dataset:
    X: np.ndarray                 # (n, len(COLUMNS))
    y: np.ndarray                 # (n,) 1 = TP before SL
    ts: np.ndarray                # (n,) epoch seconds, for chronological splits
    schema_version: int = FEATURE_SCHEMA_VERSION

    def __len__(self) -> int:
        return len(self.y)


def build_dataset(records: list[SignalRecord]) -> Dataset:
    rows = [r for r in records if r.outcome in ("win", "loss")]
    rows.sort(key=lambda r: r.ts)
    if not rows:
        return Dataset(X=np.empty((0, len(COLUMNS))), y=np.empty(0), ts=np.empty(0))
    X = np.stack([vectorize(r) for r in rows])
    y = np.asarray([1 if r.outcome == "win" else 0 for r in rows], dtype=np.int64)
    ts = np.asarray([r.ts for r in rows], dtype=np.int64)
    assert not np.isnan(X).any(), "NaN leaked into feature matrix"
    return Dataset(X=X, y=y, ts=ts)
