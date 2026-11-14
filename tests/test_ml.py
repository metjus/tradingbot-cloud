import numpy as np
import pytest

from argus.analysis.regime import Regime
from argus.analysis.selector import StrategySelector
from argus.backtest.engine import SignalRecord
from argus.db.database import Database
from argus.marketdata.model import Timeframe
from argus.ml.dataset import COLUMNS, Dataset, build_dataset, vectorize
from argus.ml.model import SetupScorer
from argus.ml.registry import ModelRegistry
from argus.ml.training import walk_forward_splits, walk_forward_validate, ValidationGates
from argus.strategy.base import Signal


def rec(ts=1000, outcome="win", direction="LONG", rsi=60.0, strategy="momentum"):
    s = Signal(strategy, "X/USD", direction, 100, 99, 102, 0.6, [], Timeframe.M5)
    feats = {"close": 100.0, "rsi14": rsi, "adx14": 30.0, "atr14": 1.0,
             "ema9": 99.5, "ema21": 99.0, "vwap": 99.2, "macd_hist": 0.1,
             "bb_upper": 101.5, "bb_lower": 98.5, "bb_width_pctile": 40.0,
             "vol_ratio": 1.4}
    return SignalRecord(signal=s, bar_index=0, ts=ts, regime=Regime.STRONG_UPTREND,
                        score=75, features=feats, outcome=outcome, bars_held=5,
                        pl_r=2.0 if outcome == "win" else -1.0)


def test_vectorize_shape_and_no_nan():
    v = vectorize(rec())
    assert v.shape == (len(COLUMNS),)
    assert not np.isnan(v).any()
    # one-hot strategy and regime set exactly once
    assert v[len(COLUMNS) - 15:].sum() == 2.0


def test_build_dataset_sorted_and_labelled():
    ds = build_dataset([rec(ts=300, outcome="loss"), rec(ts=100, outcome="win"),
                        rec(ts=200, outcome="timeout")])
    assert len(ds) == 2            # timeout excluded
    assert list(ds.ts) == [100, 300]
    assert list(ds.y) == [1, 0]


def test_walk_forward_no_leakage():
    n = 500
    ts = np.arange(n) * 3600  # 1h apart
    for train_idx, val_idx in walk_forward_splits(ts, 4, embargo_s=8 * 3600):
        assert train_idx.max() < val_idx.min()
        # embargo honoured
        assert ts[val_idx.min()] - ts[train_idx.max()] >= 8 * 3600
        assert len(np.intersect1d(train_idx, val_idx)) == 0


def test_walk_forward_finds_learnable_pattern():
    # synthetic: high RSI wins, low RSI loses -> the model must beat AUC 0.55
    rng = np.random.default_rng(0)
    records = []
    for i in range(800):
        high = bool(rng.integers(0, 2))
        win = rng.random() < (0.75 if high else 0.30)
        records.append(rec(ts=i * 3600 * 10, rsi=70.0 if high else 35.0,
                           outcome="win" if win else "loss"))
    ds = build_dataset(records)
    rep = walk_forward_validate(ds, ValidationGates(min_samples=300))
    assert rep.n_samples == 800
    assert rep.mean_auc > 0.6
    assert rep.passed, rep.reasons


def test_walk_forward_rejects_noise():
    rng = np.random.default_rng(1)
    records = [rec(ts=i * 36000, rsi=float(rng.uniform(30, 70)),
                   outcome="win" if rng.random() < 0.5 else "loss")
               for i in range(600)]
    ds = build_dataset(records)
    rep = walk_forward_validate(ds, ValidationGates(min_samples=300))
    assert not rep.passed  # pure noise must not validate


def test_registry_register_activate_rollback(tmp_path):
    db = Database(tmp_path / "r.db")
    reg = ModelRegistry(db, tmp_path / "models")
    ds = build_dataset([rec(ts=i * 1000, outcome="win" if i % 2 else "loss")
                        for i in range(200)])
    m1 = SetupScorer(); m1.fit(ds.X, ds.y)
    v1 = reg.register(m1, "P(TP before SL)", {"auc": 0.6})
    m2 = SetupScorer(); m2.fit(ds.X, ds.y)
    v2 = reg.register(m2, "P(TP before SL)", {"auc": 0.62})
    assert (v1, v2) == (1, 2)

    reg.activate("setup-scorer", 1)
    assert reg.load_active("setup-scorer").meta.version == 1
    reg.activate("setup-scorer", 2)
    assert reg.load_active("setup-scorer").meta.version == 2
    back = reg.rollback("setup-scorer")
    assert back == 1
    assert reg.load_active("setup-scorer").meta.version == 1
    names = {m["version"]: m["status"] for m in reg.list_versions("setup-scorer")}
    assert names[1] == "active" and names[2] == "archived"


def test_scorer_save_load_roundtrip(tmp_path):
    ds = build_dataset([rec(ts=i * 1000, rsi=70 if i % 2 else 35,
                            outcome="win" if i % 2 else "loss") for i in range(300)])
    m = SetupScorer(); m.fit(ds.X, ds.y)
    m.save(tmp_path / "v1")
    loaded = SetupScorer.load(tmp_path / "v1")
    p1 = m.predict_proba(ds.X[:5])
    p2 = loaded.predict_proba(ds.X[:5])
    assert np.allclose(p1, p2)
    assert loaded.score_signal(rec(rsi=70)) > loaded.score_signal(rec(rsi=35))


def test_selector_disables_bad_strategy():
    sel = StrategySelector(min_sample=30)
    good = [rec(strategy="momentum", outcome="win") for _ in range(20)] + \
           [rec(strategy="momentum", outcome="loss") for _ in range(12)]
    bad = [rec(strategy="mean_reversion", outcome="loss") for _ in range(28)] + \
          [rec(strategy="mean_reversion", outcome="win") for _ in range(4)]
    sel.ingest(good + bad)
    assert sel.is_enabled("momentum")
    assert not sel.is_enabled("mean_reversion")
    snap = sel.snapshot()
    assert "mean_reversion" in snap["disabled"]
    # recovery: a strong new sample flips it back
    sel.ingest([rec(strategy="mean_reversion", outcome="win") for _ in range(40)])
    assert sel.is_enabled("mean_reversion")


def test_ml_factor_moves_decision_score():
    from argus.decision import DecisionEngine
    from argus.risk import RiskSupervisor

    class FakeScorer:
        def __init__(self, p): self.p = p
        def score_live(self, signal, regime, features, ts): return self.p

    from tests.test_decision import uptrend_ctx
    from argus.risk import AccountState
    account = AccountState(equity=10_000, day_start_equity=10_000)

    hi = DecisionEngine(RiskSupervisor(), ml_scorer=FakeScorer(0.9)).evaluate(uptrend_ctx(), account)
    lo = DecisionEngine(RiskSupervisor(), ml_scorer=FakeScorer(0.1)).evaluate(uptrend_ctx(), account)
    if hi.best and lo.best:
        assert hi.best.factors["ml_probability"] == pytest.approx(16.0)
        assert lo.best.factors["ml_probability"] == pytest.approx(-16.0)
        assert hi.best.score > lo.best.score
