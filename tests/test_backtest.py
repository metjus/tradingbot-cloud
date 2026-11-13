import math

from argus.backtest.engine import Backtester
from argus.backtest.history import resample
from argus.backtest.metrics import compute_stats
from argus.backtest.engine import SignalRecord, TradeRecord
from argus.analysis.regime import Regime
from argus.marketdata.model import Candle, Timeframe
from argus.strategy.base import Signal

M5 = Timeframe.M5


def mk(ts, o, h, lo, c, v=1.0, sym="X/USD"):
    return Candle(symbol=sym, timeframe=M5, ts=ts, open=o, high=h, low=lo,
                  close=c, volume=v, source="t")


def sig(direction="LONG", entry=100.0, sl=99.0, tp=102.0):
    return Signal("s", "X/USD", direction, entry, sl, tp, 0.5, [], M5)


def test_resample_5m_to_15m():
    base = 0
    bars = [mk(base + i * 300, 10 + i, 11 + i, 9 + i, 10.5 + i, v=1) for i in range(6)]
    out = resample(bars, Timeframe.M15)
    assert len(out) == 2
    a = out[0]
    assert (a.ts, a.open, a.high, a.low, a.close, a.volume) == (0, 10, 13, 9, 12.5, 3)
    assert all(c.complete for c in out)


def test_outcome_tp_first():
    bt = Backtester()
    future = [mk(300, 100, 101, 99.5, 100.8), mk(600, 100.8, 102.5, 100.5, 102.2)]
    outcome, held, pl_r = bt.simulate_outcome(sig(), future)
    assert outcome == "win" and held == 2 and math.isclose(pl_r, 2.0)


def test_outcome_sl_first():
    bt = Backtester()
    future = [mk(300, 100, 100.5, 98.8, 99.0)]
    outcome, held, pl_r = bt.simulate_outcome(sig(), future)
    assert outcome == "loss" and pl_r == -1.0


def test_same_bar_both_hit_counts_as_loss():
    bt = Backtester()
    future = [mk(300, 100, 103, 98, 101)]  # touches SL and TP in one bar
    outcome, _, pl_r = bt.simulate_outcome(sig(), future)
    assert outcome == "loss" and pl_r == -1.0


def test_short_outcome():
    bt = Backtester()
    s = sig(direction="SHORT", entry=100, sl=101, tp=98)
    future = [mk(300, 100, 100.6, 97.9, 98.2)]
    outcome, _, pl_r = bt.simulate_outcome(s, future)
    assert outcome == "win" and math.isclose(pl_r, 2.0)


def test_timeout_outcome():
    bt = Backtester(max_hold_bars=3)
    future = [mk(i * 300, 100, 100.4, 99.6, 100.2) for i in range(1, 5)]
    outcome, held, pl_r = bt.simulate_outcome(sig(), future)
    assert outcome == "timeout" and held == 3
    assert math.isclose(pl_r, 0.2)  # (100.2-100)/1


def _trade(pl, bars=5):
    rec = SignalRecord(signal=sig(), bar_index=0, ts=0, regime=Regime.SIDEWAYS,
                       score=70, features={}, outcome="win" if pl > 0 else "loss",
                       bars_held=bars, pl_r=pl / 50)
    return TradeRecord(signal=rec, quantity=1, pl=pl, fees=0, equity_after=0)


def test_metrics_hand_computed():
    trades = [_trade(100), _trade(-50), _trade(60), _trade(-50)]
    st = compute_stats(trades, start_equity=1000)
    assert st.n_trades == 4 and st.wins == 2 and st.losses == 2
    assert math.isclose(st.win_rate, 0.5)
    assert math.isclose(st.profit_factor, 160 / 100)
    assert math.isclose(st.expectancy, 15.0)
    assert math.isclose(st.avg_win, 80.0) and math.isclose(st.avg_loss, -50.0)
    # equity: 1100 (peak), 1050 (dd 50/1100), 1110 (peak), 1060 (dd 50/1110)
    assert math.isclose(st.max_drawdown_pct, 50 / 1100 * 100, rel_tol=1e-6)


def test_backtester_runs_on_synthetic_trend():
    import random
    rng = random.Random(1)
    bars = []
    price = 100.0
    for i in range(900):
        o = price
        c = price + 0.25 + rng.uniform(-0.15, 0.15)
        h = max(o, c) + rng.uniform(0, 0.1)
        lo = min(o, c) - rng.uniform(0, 0.1)
        vol = 5.0 + (8.0 if i % 25 == 0 else 0.0)
        bars.append(mk(i * 300, o, h, lo, c, v=vol))
        price = c
    res = Backtester(warmup_bars=260).run("X/USD", bars)
    assert res.bars == 900
    assert len(res.signals) > 0, "an uptrend must generate some signals"
    labelled = res.dataset
    assert all(r.outcome in ("win", "loss") for r in labelled)
    # no look-ahead: every signal's exit is at or after its entry bar
    for r in res.signals:
        assert r.exit_ts is None or r.exit_ts >= r.ts
