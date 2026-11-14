"""Setup scorer: P(TP before SL) for a proposed signal.

Gradient boosting via scikit-learn's HistGradientBoostingClassifier -
the same model family as XGBoost/LightGBM with no extra native
dependencies; the interface below allows swapping implementations later
without touching the decision engine.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier

from ..analysis.regime import Regime
from ..backtest.engine import SignalRecord
from ..strategy.base import Signal
from .dataset import COLUMNS, FEATURE_SCHEMA_VERSION, vectorize


@dataclass(slots=True)
class SetupScorerMeta:
    name: str = "setup-scorer"
    version: int = 0
    schema_version: int = FEATURE_SCHEMA_VERSION
    n_samples: int = 0
    metrics: dict | None = None


class SetupScorer:
    """Thin wrapper: train / predict / save / load."""

    def __init__(self, model: HistGradientBoostingClassifier | None = None,
                 meta: SetupScorerMeta | None = None) -> None:
        self.model = model or HistGradientBoostingClassifier(
            max_depth=4, max_iter=200, learning_rate=0.06,
            l2_regularization=1.0, min_samples_leaf=25,
            early_stopping=True, validation_fraction=0.15, random_state=42)
        self.meta = meta or SetupScorerMeta()

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        self.model.fit(X, y)
        self.meta.n_samples = len(y)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(X)[:, 1]

    def score_signal(self, rec: SignalRecord) -> float:
        return float(self.predict_proba(vectorize(rec)[None, :])[0])

    def score_live(self, signal: Signal, regime: Regime, features_m5: dict,
                   ts: int) -> float:
        """Score a live signal (builds a transient record)."""
        rec = SignalRecord(signal=signal, bar_index=-1, ts=ts, regime=regime,
                           score=0.0, features=features_m5)
        return self.score_signal(rec)

    # --- persistence --------------------------------------------------------
    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        joblib.dump(self.model, directory / "model.joblib")
        (directory / "meta.json").write_text(json.dumps({
            "name": self.meta.name, "version": self.meta.version,
            "schema_version": self.meta.schema_version,
            "n_samples": self.meta.n_samples, "metrics": self.meta.metrics,
            "columns": COLUMNS,
        }, indent=2))

    @classmethod
    def load(cls, directory: Path) -> "SetupScorer":
        meta_raw = json.loads((directory / "meta.json").read_text())
        if meta_raw.get("schema_version") != FEATURE_SCHEMA_VERSION:
            raise ValueError(
                f"model schema v{meta_raw.get('schema_version')} != code v{FEATURE_SCHEMA_VERSION}")
        model = joblib.load(directory / "model.joblib")
        meta = SetupScorerMeta(
            name=meta_raw["name"], version=meta_raw["version"],
            schema_version=meta_raw["schema_version"],
            n_samples=meta_raw["n_samples"], metrics=meta_raw.get("metrics"))
        return cls(model=model, meta=meta)
