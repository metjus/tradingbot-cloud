"""Decision engine + strategies on synthetic market data."""

import math
import random

from argus.analysis.regime import Regime, detect_regime
from argus.decision import DecisionEngine
from argus.marketdata.model import Candle, Timeframe
from argus.risk import AccountState, RiskSupervisor
from argus.strategy.base import MarketContext
from argus.ta.features import compute_features

M5, M15, H1 = Timeframe.M5, Timeframe.M15, Timeframe.H1


def synth(tf: Timeframe, n: int, start: float = 100.0, drift: float = 0.0,
          wiggle: float = 0.2, vol: float = 5.0, last_vol: float | None = None,
          seed: int = 7) -> list[Candle]:
    """Deterministic synthetic candles with a given per-bar drift."""
    rng = random.Random(seed)
    out = []
    price = start
    for i in range(n):
        o = price
        c = price + drift + rng.uniform(-wiggle, wiggle)
        h = max(o, c) + rng.uniform(0, wiggle)
        lo = min(o, c) - rng.uniform(0, wiggle)
        v = vol if (last_vol is None or i < n - 1) else last_vol
        out.append(Candle(symbol="SYN/USD", timeframe=tf, ts=(i + 1) * tf.seconds,
                          open=o, high=h, low=lo, close=c, volume=v, source="syn"))
        price = c
    return out


def ctx_from(candles_by_tf: dict) -> MarketContext:
    feats = {tf: compute_features(bars) for tf, bars in candles_by_tf.items()}
    feats = {tf: f for tf, f in feats.items() if f is not None}
    return MarketContext(symbol="SYN/USD", now_ts=10**9, candles=candles_by_tf,
                         features=feats, regime=Regime.UNCERTAIN)


def uptrend_ctx(last_vol: float = 12.0) -> MarketContext:
    return ctx_from({
        M5: synth(M5, 300, drift=0.30, wiggle=0.10, last_vol=last_vol),
        M15: synth(M15, 250, drift=0.90, wiggle=0.25),
        H1: synth(H1, 250, drift=3.00, wiggle=0.8),
    })


def flat_ctx() -> MarketContext:
    return ctx_from({
        M5: synth(M5, 300, drift=0.0, wiggle=0.35),
        M15: synth(M15, 250, drift=0.0, wiggle=0.6),
        H1: synth(H1, 250, drift=0.0, wiggle=1.0),
    })


def test_regime_detection_uptrend_and_flat():
    up = uptrend_ctx()
    assert detect_regime(up.f(M15), up.f(H1)) is Regime.STRONG_UPTREND
    fl = flat_ctx()
    assert detect_regime(fl.f(M15), fl.f(H1)) in (
        Regime.SIDEWAYS, Regime.LOW_VOLATILITY, Regime.BREAKOUT_SETUP)


def test_uptrend_produces_long_entry():
    engine = DecisionEngine(RiskSupervisor(), enter_threshold=70.0)
    account = AccountState(equity=10_000, day_start_equity=10_000)
    res = engine.evaluate(uptrend_ctx(), account)
    assert res.regime is Regime.STRONG_UPTREND
    assert res.evaluated, "trend strategies should fire in a strong uptrend"
    assert res.best.signal.direction == "LONG"
    assert res.verdict == "enter"
    assert res.risk is not None and res.risk.approved
    assert res.risk.quantity > 0
    # explainability: factors and reasons present
    assert res.best.factors["regime_fit"] == 20.0
    assert res.best.signal.reasons


def test_scores_bounded_and_explainable():
    engine = DecisionEngine(RiskSupervisor())
    account = AccountState(equity=10_000, day_start_equity=10_000)
    for ctx in (uptrend_ctx(), flat_ctx()):
        res = engine.evaluate(ctx, account)
        for sc in res.evaluated:
            assert 0.0 <= sc.score <= 100.0
            assert math.isclose(
                sc.score, max(0.0, min(100.0, sum(sc.factors.values()))), abs_tol=1e-9)


def test_threshold_rejects():
    engine = DecisionEngine(RiskSupervisor(), enter_threshold=99.5)
    account = AccountState(equity=10_000, day_start_equity=10_000)
    res = engine.evaluate(uptrend_ctx(), account)
    assert res.verdict in ("reject", "no_signal")
    if res.verdict == "reject":
        assert any("below threshold" in n for n in res.notes)


def test_daily_loss_vetoes_even_great_setup():
    engine = DecisionEngine(RiskSupervisor(), enter_threshold=60.0)
    account = AccountState(equity=9_700, day_start_equity=10_000)  # -3%
    res = engine.evaluate(uptrend_ctx(), account)
    if res.evaluated and res.best.score >= 60.0:
        assert res.verdict == "veto"
        assert any("denný limit" in v for v in res.risk.vetoes)


def test_no_signal_without_enough_history():
    ctx = ctx_from({M5: synth(M5, 20), M15: synth(M15, 20), H1: synth(H1, 20)})
    engine = DecisionEngine(RiskSupervisor())
    account = AccountState(equity=10_000, day_start_equity=10_000)
    res = engine.evaluate(ctx, account)
    assert res.verdict == "no_signal"


def test_sentiment_shifts_score():
    engine = DecisionEngine(RiskSupervisor())
    ctx = uptrend_ctx()
    account = AccountState(equity=10_000, day_start_equity=10_000)
    res_neutral = engine.evaluate(ctx, account)
    ctx2 = uptrend_ctx()
    ctx2.sentiment = 1.0
    res_pos = engine.evaluate(ctx2, account)
    if res_neutral.best and res_pos.best and \
            res_neutral.best.signal.direction == res_pos.best.signal.direction == "LONG":
        assert res_pos.best.factors["sentiment"] > res_neutral.best.factors["sentiment"]
