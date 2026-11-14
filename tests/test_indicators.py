import math

from argus.marketdata.model import Candle, Timeframe
from argus.ta import indicators as ind
from argus.ta.features import compute_features


def mk(ts, o, h, lo, c, v=1.0):
    return Candle(symbol="X", timeframe=Timeframe.M5, ts=ts, open=o, high=h,
                  low=lo, close=c, volume=v)


def test_sma_exact():
    assert ind.sma([1, 2, 3, 4, 5], 3)[2:] == [2.0, 3.0, 4.0]
    assert ind.sma([1, 2], 3) == [None, None]


def test_ema_exact_seeded_with_sma():
    out = ind.ema([1, 2, 3, 4, 5], 3)
    assert out[:2] == [None, None]
    assert out[2] == 2.0           # seed = SMA(1,2,3)
    assert out[3] == 3.0           # 4*0.5 + 2*0.5
    assert out[4] == 4.0           # 5*0.5 + 3*0.5


def test_rsi_wilder_exact():
    # prices: 10,11,12,11,12 with period 2 (hand-computed Wilder values)
    out = ind.rsi([10, 11, 12, 11, 12], 2)
    assert out[0] is None and out[1] is None
    assert out[2] == 100.0
    assert math.isclose(out[3], 50.0)
    assert math.isclose(out[4], 75.0)
    assert all(v is None or 0 <= v <= 100 for v in out)


def test_macd_structure():
    closes = [float(i) for i in range(1, 80)]  # steady uptrend
    line, sig, hist = ind.macd(closes)
    assert line[-1] is not None and sig[-1] is not None
    assert line[-1] > 0            # uptrend -> positive macd
    assert math.isclose(hist[-1], line[-1] - sig[-1])


def test_atr_constant_range():
    candles = [mk(i * 300, 1.5, 2.0, 1.0, 1.5) for i in range(10)]
    out = ind.atr(candles, 3)
    assert out[1] is None
    for v in out[2:]:
        assert math.isclose(v, 1.0)


def test_bollinger_contains_sma_and_orders():
    vals = [1, 2, 3, 2, 1, 2, 3, 2, 1, 2] * 3
    up, mid, lo = ind.bollinger(vals, 5, 2.0)
    for u, m, l in zip(up, mid, lo):
        if u is not None:
            assert l <= m <= u


def test_vwap_weighted():
    candles = [
        mk(0, 10, 10, 10, 10, v=1.0),   # typical 10
        mk(300, 20, 20, 20, 20, v=3.0),  # typical 20
    ]
    out = ind.vwap(candles)
    assert math.isclose(out[0], 10.0)
    assert math.isclose(out[1], (10 * 1 + 20 * 3) / 4)


def test_adx_trend_vs_flat():
    trend = [mk(i * 300, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(60)]
    flat = [mk(i * 300, 100, 100.5 + 0.2 * (i % 2), 99.5 - 0.2 * (i % 2), 100) for i in range(60)]
    adx_t, pdi_t, mdi_t = ind.adx(trend, 14)
    adx_f, _, _ = ind.adx(flat, 14)
    assert adx_t[-1] is not None and adx_f[-1] is not None
    assert adx_t[-1] > 60 > adx_f[-1]
    assert pdi_t[-1] > mdi_t[-1]


def test_features_snapshot():
    candles = [mk(i * 300, 100 + i * 0.1, 100.4 + i * 0.1, 99.8 + i * 0.1,
                  100.2 + i * 0.1, v=5.0) for i in range(60)]
    fs = compute_features(candles)
    assert fs is not None
    assert fs.close == candles[-1].close
    assert fs.ema9 is not None and fs.rsi14 is not None and fs.atr14 is not None
    assert fs.high_20 is not None and fs.high_20 <= max(c.high for c in candles)
    assert fs.vol_ratio is not None and math.isclose(fs.vol_ratio, 1.0)
    assert compute_features(candles[:10]) is None
