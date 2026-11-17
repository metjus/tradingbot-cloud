"""Active mode = trend portfolio: 12 flip systems on 2h-8h bars."""

import math

from argus.analysis.regime import Regime
from argus.core.events import EventBus
from argus.db.database import Database
from argus.decision import DecisionEngine
from argus.marketdata.model import Candle, Timeframe
from argus.monitoring.health import HealthMonitor
from argus.risk import AccountState, RiskLimits, RiskSupervisor
from argus.strategy.base import MarketContext, Signal, Strategy
from argus.strategy.futures import ACTIVE_STRATEGIES, FLIP_EXIT_NAMES, SLEEVE_OF
from argus.strategy.trend_portfolio import (FLIP_STATE, FlipSystem, market_vol_factor,
                                            tf_bars)
from argus.trading.broker import PaperBroker
from argus.trading.executor import ExecutionService
from argus.trading.portfolio import Portfolio

H1, H4 = Timeframe.H1, Timeframe.H4
T0 = 1_700_006_400            # 00:00 UTC (divisible by 8h)


def limits(**kw):
    base = dict(round_trip_cost_pct=0.09, allow_short=True, leverage=3.0,
                max_leverage=10.0, maintenance_margin_pct=5.0, min_rr=1.3)
    base.update(kw)
    return RiskLimits(**base)


def hourly(n_up=300, n_down=12, sym="SOL/USD"):
    out, p = [], 100.0
    for i in range(n_up + n_down):
        o = p
        c = p * 1.002 if i < n_up else p * 0.97
        out.append(Candle(sym, H1, T0 + i * 3600, o, max(o, c) * 1.001, min(o, c) * 0.999,
                          c, 10.0, complete=True))
        p = c
    return out


def ctx_at(bars, now, sym="SOL/USD"):
    c = MarketContext(symbol=sym, now_ts=now, candles={}, features={},
                      regime=Regime.UNCERTAIN, extras={})
    c.extras["h1_loader"] = lambda: [b for b in bars if b.ts + 3600 <= now]
    return c


def test_portfolio_is_the_researched_12_systems():
    assert len(ACTIVE_STRATEGIES) == 12
    assert {s.name for s in ACTIVE_STRATEGIES} == FLIP_EXIT_NAMES
    assert all(SLEEVE_OF[s.name] == "active" for s in ACTIVE_STRATEGIES)
    assert len({s.name for s in ACTIVE_STRATEGIES}) == 12      # unique names
    assert all(len(s.name) <= 48 for s in ACTIVE_STRATEGIES)   # DB column


def test_tf_bars_are_utc_aligned_and_complete_only():
    bars = hourly(20, 0)
    b = tf_bars(bars, 8, T0 + 20 * 3600)
    assert list(b["t"]) == [T0, T0 + 8 * 3600]        # 16..20h bucket incomplete
    assert b["c"][0] == bars[7].close and b["o"][1] == bars[8].open
    assert b["h"][0] == max(x.high for x in bars[:8])


def test_flip_system_signals_once_on_the_flip_bar():
    s = FlipSystem("ema", (3, 6), 2, 0)
    bars = hourly()
    sigs = []
    for k in range(200, len(bars)):
        now = bars[k].ts + 3600
        for _ in range(2):                              # every 5-min evaluation
            r = s.evaluate(ctx_at(bars, now))
            if r is not None:
                sigs.append((now, r))
    assert len(sigs) == 1
    now, sig = sigs[0]
    assert sig.direction == "SHORT" and sig.sleeve == "active" and sig.exploration
    assert now >= bars[300].ts and now % 7200 == 0
    assert sig.stop_loss > sig.entry > sig.take_profit
    assert FLIP_STATE[(s.name, "SOL/USD")] == -1


def test_stale_flip_after_restart_is_not_entered_but_state_is_known():
    s = FlipSystem("ema", (3, 6), 2, 0)
    bars = hourly()
    flip_now = None
    probe = FlipSystem("ema", (3, 6), 2, 0)
    for k in range(200, len(bars)):
        if probe.evaluate(ctx_at(bars, bars[k].ts + 3600)) is not None:
            flip_now = bars[k].ts + 3600
            break
    # restarted 90 minutes after the flip bar closed (still inside the next 2h bar)
    late = flip_now + 5400
    assert s.evaluate(ctx_at(bars, late)) is None
    assert FLIP_STATE[(s.name, "SOL/USD")] == -1


def test_ema200_filter_blocks_counter_trend_entry_but_tracks_state():
    s = FlipSystem("ema", (3, 6), 1, 1)                 # 1h bars, EMA200 filter
    bars = hourly(n_up=400, n_down=3)                   # a dip in a long uptrend
    got = [s.evaluate(ctx_at(bars, b.ts + 3600)) for b in bars[250:]]
    assert all(g is None or g.direction == "LONG" for g in got)
    assert FLIP_STATE[(s.name, "SOL/USD")] == -1        # it did flip short


def test_market_volatility_scales_the_risk():
    assert market_vol_factor(0.035) == 1.0
    assert market_vol_factor(0.07) == 0.5
    assert market_vol_factor(0.5) == 0.25 and market_vol_factor(0.001) == 1.5
    assert market_vol_factor(None) == 1.0


def _asig(name, direction="LONG", sym="SOL/USD"):
    stop = 94.0 if direction == "LONG" else 106.0
    tp = 200.0 if direction == "LONG" else 1.0
    return Signal(name, sym, direction, 100.0, stop, tp, 0.5, [],
                  timeframe=H4, exploration=True, risk_scale=0.01, sleeve="active")


class Fixed(Strategy):
    def __init__(self, name, s, exploration=False, any_coin=False):
        self.name, self._s = name, s
        self.exploration, self.any_coin = exploration, any_coin
        self.suited_regimes = ()

    def evaluate(self, c):
        return self._s


def _ctx():
    from argus.ta.features import compute_features
    bars = [Candle("SOL/USD", H4, T0 + i * 14400, 100 + i * 0.1, 101 + i * 0.1,
                   99 + i * 0.1, 100.5 + i * 0.1, 10.0, complete=True) for i in range(230)]
    return MarketContext(symbol="SOL/USD", now_ts=bars[-1].ts + 14400, candles={H4: bars},
                         features={H4: compute_features(bars)}, regime=Regime.UNCERTAIN,
                         extras={})


def test_several_systems_flipping_together_all_enter_outside_the_top():
    names = sorted(FLIP_EXIT_NAMES)[:3]
    strats = [Fixed(n, _asig(n), exploration=True, any_coin=True) for n in names]
    eng = DecisionEngine(RiskSupervisor(limits(risk_per_trade_pct=2.0)), strategies=strats,
                         enter_threshold=0)
    eng.allowed_symbols = {"BTC/USD"}                    # SOL outside the focused top
    acc = AccountState(equity=10_000.0, day_start_equity=10_000.0)
    assert eng.evaluate(_ctx(), acc).verdict == "no_signal"   # active mode off
    eng.active_enabled, eng.active_risk_pct, eng.active_vol_k = True, 0.02, 1.5
    res = eng.evaluate(_ctx(), acc)
    assert res.verdict == "enter" and len(res.extra) == 2
    assert math.isclose(res.risk.risk_pct, 0.03, rel_tol=1e-9)   # 0.02 % x 1.5


def test_active_positions_do_not_count_against_the_main_cap():
    rs = RiskSupervisor(limits(risk_per_trade_pct=2.0, max_open_positions=1))
    s = _asig(sorted(FLIP_EXIT_NAMES)[0])
    s.risk_scale = 0.01
    acc = AccountState(equity=10_000.0, day_start_equity=10_000.0, open_positions=1,
                       open_long=1, sleeve_open={"active": 150})
    assert rs.check(s, acc).approved


def _exec(tmp_path):
    db = Database(tmp_path / "a.db")
    bus = EventBus()
    broker = PaperBroker(fee_pct=0.05, slippage_pct=0.0, maker_fee_pct=0.02)
    pf = Portfolio(db, broker_name="paper", mode="paper", start_equity=10_000.0)
    ex = ExecutionService(db, bus, HealthMonitor(bus), broker, pf, RiskSupervisor(limits()))
    ex.entry_order = "limit"                             # active still enters at market
    ex.state = "running"
    return ex, broker, pf


def _decision(sig, qty=1.0):
    return {"best": {"signal": sig.as_dict()},
            "risk": {"approved": True, "quantity": qty, "leverage": 3.0, "risk_pct": 0.02}}


def test_each_system_holds_its_own_position_and_reverses_on_flip(tmp_path):
    ex, broker, pf = _exec(tmp_path)
    a, b = sorted(FLIP_EXIT_NAMES)[:2]
    broker.on_tick("SOL/USD", 100.0)
    ex._maybe_enter(_decision(_asig(a, "LONG")))
    ex._maybe_enter(_decision(_asig(b, "LONG")))
    assert sorted(p.strategy for p in pf.positions.values()) == [a, b]   # market fills
    ex._maybe_enter(_decision(_asig(a, "LONG")))         # same system: no second one
    assert len(pf.positions) == 2
    ex._maybe_enter(_decision(_asig(a, "SHORT")))        # system a flipped
    got = sorted((p.strategy, p.direction) for p in pf.positions.values())
    assert got == [(a, "SHORT"), (b, "LONG")]
    main = Signal("trend_rider_4h", "SOL/USD", "LONG", 100.0, 98.0, 140.0, 0.7, [],
                  timeframe=H4)
    ex._maybe_enter(_decision(main))
    assert len(pf.positions) == 3                        # main strategy unaffected


def test_position_closes_when_its_system_flips_even_without_new_entry(tmp_path):
    ex, broker, pf = _exec(tmp_path)
    a = sorted(FLIP_EXIT_NAMES)[0]
    broker.on_tick("SOL/USD", 100.0)
    ex._maybe_enter(_decision(_asig(a, "LONG")))
    FLIP_STATE[(a, "SOL/USD")] = 1
    ex._manage_stops("SOL/USD", 101.0)
    assert len(pf.positions) == 1
    FLIP_STATE[(a, "SOL/USD")] = -1                      # flipped, filter blocks short
    ex._manage_stops("SOL/USD", 101.0)
    assert not pf.positions
    FLIP_STATE.pop((a, "SOL/USD"), None)


def test_active_trades_do_not_use_up_the_main_daily_cap_or_open_risk():
    rs = RiskSupervisor(limits(risk_per_trade_pct=2.0, max_daily_trades=15,
                               daily_max_loss_pct=2.0))
    main = Signal("trend_rider_4h", "SOL/USD", "LONG", 100.0, 98.0, 140.0, 0.7, [],
                  timeframe=H4)
    busy = AccountState(equity=10_000.0, day_start_equity=10_000.0, trades_today=40,
                        sleeve_today={"active": 38}, open_risk_pct=3.0,
                        main_open_risk_pct=0.0, sleeve_risk={"active": 3.0})
    v = rs.check(main, busy)
    assert v.approved, v.vetoes                          # 2 main trades, 0 % main risk
    full_main = AccountState(equity=10_000.0, day_start_equity=10_000.0, trades_today=20,
                             sleeve_today={"active": 5})
    assert not rs.check(main, full_main).approved         # 15 main trades today


def test_active_open_risk_has_its_own_ceiling():
    rs = RiskSupervisor(limits(risk_per_trade_pct=2.0))
    s = _asig(sorted(FLIP_EXIT_NAMES)[0])
    s.risk_scale = 0.01                                   # 0.02 %
    ok = AccountState(equity=10_000.0, day_start_equity=10_000.0, sleeve_risk={"active": 7.0})
    assert rs.check(s, ok).approved
    full = AccountState(equity=10_000.0, day_start_equity=10_000.0,
                        sleeve_risk={"active": 7.99})
    v = rs.check(s, full)
    assert not v.approved and any("strop" in x for x in v.vetoes)
