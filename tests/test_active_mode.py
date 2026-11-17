"""Active mode (aktívny režim): PSAR 4h flip on its own paper account."""

import math

from argus.analysis.regime import Regime
from argus.core.events import EventBus
from argus.db.database import Database
from argus.decision import DecisionEngine
from argus.marketdata.model import Candle, Timeframe
from argus.monitoring.health import HealthMonitor
from argus.risk import AccountState, RiskLimits, RiskSupervisor
from argus.strategy.base import MarketContext, Signal, Strategy
from argus.strategy.futures import FLIP_EXIT_NAMES, SLEEVE_OF, PsarFlip4h
from argus.ta.features import compute_features
from argus.trading.broker import PaperBroker
from argus.trading.executor import ExecutionService
from argus.trading.portfolio import Portfolio

H4 = Timeframe.H4


def limits(**kw):
    base = dict(round_trip_cost_pct=0.09, allow_short=True, leverage=3.0,
                max_leverage=10.0, maintenance_margin_pct=5.0, min_rr=1.3)
    base.update(kw)
    return RiskLimits(**base)


def bars_up_then_down(n_up=80, n_down=1, sym="SOL/USD"):
    out, p = [], 100.0
    for i in range(n_up + n_down):
        o = p
        c = p + 0.5 if i < n_up else p - 12.0          # one hard red bar
        h, lo = max(o, c) + 0.2, min(o, c) - 0.2
        out.append(Candle(sym, H4, 1_700_000_000 + i * 14400, o, h, lo, c, 100.0,
                          complete=True))
        p = c
    return out


def ctx(bars, sym="SOL/USD"):
    return MarketContext(symbol=sym, now_ts=bars[-1].ts + 14400, candles={H4: bars},
                         features={H4: compute_features(bars)}, regime=Regime.UNCERTAIN,
                         extras={})


def test_psar_signals_only_on_a_flip():
    s = PsarFlip4h()
    up = bars_up_then_down(n_up=80, n_down=0)
    assert s.evaluate(ctx(up)) is None                  # steady trend, no flip
    s2 = PsarFlip4h()
    flip = bars_up_then_down(n_up=80, n_down=1)
    sig = s2.evaluate(ctx(flip))
    assert sig is not None and sig.direction == "SHORT"
    assert sig.sleeve == "active" and sig.exploration and sig.stop_loss > sig.entry
    assert s2.evaluate(ctx(flip)) is None               # once per 4h bar
    assert SLEEVE_OF["psar_active_4h"] == "active" and "psar_active_4h" in FLIP_EXIT_NAMES


def _asig(direction="LONG", sym="SOL/USD"):
    stop = 94.0 if direction == "LONG" else 106.0
    tp = 200.0 if direction == "LONG" else 1.0
    return Signal("psar_active_4h", sym, direction, 100.0, stop, tp, 0.5, [],
                  timeframe=H4, exploration=True, risk_scale=0.1, sleeve="active")


class Fixed(Strategy):
    def __init__(self, name, s, exploration=False, any_coin=False):
        self.name, self._s = name, s
        self.exploration, self.any_coin = exploration, any_coin
        self.suited_regimes = ()

    def evaluate(self, c):
        return self._s


def test_active_mode_trades_coins_outside_the_top():
    main = Fixed("trend_rider_4h", Signal("trend_rider_4h", "SOL/USD", "LONG", 100.0,
                                          98.0, 140.0, 0.7, [], timeframe=H4))
    active = Fixed("psar_active_4h", _asig(), exploration=True, any_coin=True)
    eng = DecisionEngine(RiskSupervisor(limits()), strategies=[main, active],
                         enter_threshold=0)
    eng.allowed_symbols = {"BTC/USD"}                    # SOL is outside the top
    c = ctx(bars_up_then_down(230, 0))
    acc = AccountState(equity=10_000.0, day_start_equity=10_000.0)
    res = eng.evaluate(c, acc)
    assert res.verdict == "no_signal"                    # active mode off
    eng.active_enabled, eng.active_risk_scale = True, 0.1
    res = eng.evaluate(c, acc)
    assert res.verdict == "enter" and res.best.signal.strategy == "psar_active_4h"


def test_active_risk_is_scaled_and_has_own_caps():
    rs = RiskSupervisor(limits(risk_per_trade_pct=2.0))
    acc = AccountState(equity=10_000.0, day_start_equity=10_000.0, open_positions=1,
                       open_long=1)
    v = rs.check(_asig(), acc)
    assert v.approved                                    # main cap 1 does not block it
    assert math.isclose(v.risk_pct, 0.2, rel_tol=1e-9)


def _exec(tmp_path):
    db = Database(tmp_path / "a.db")
    bus = EventBus()
    broker = PaperBroker(fee_pct=0.05, slippage_pct=0.0, maker_fee_pct=0.02)
    pf = Portfolio(db, broker_name="paper", mode="paper", start_equity=10_000.0)
    ex = ExecutionService(db, bus, HealthMonitor(bus), broker, pf,
                          RiskSupervisor(limits()))
    ex.entry_order = "market"
    ex.state = "running"
    return ex, broker, pf


def _decision(sig, qty=1.0):
    return {"best": {"signal": sig.as_dict() if hasattr(sig, "as_dict") else sig},
            "risk": {"approved": True, "quantity": qty, "leverage": 3.0, "risk_pct": 0.2}}


def test_flip_reverses_the_active_position(tmp_path):
    ex, broker, pf = _exec(tmp_path)
    broker.on_tick("SOL/USD", 100.0)
    ex._maybe_enter(_decision(_asig("LONG")))
    assert [p.direction for p in pf.positions.values()] == ["LONG"]
    ex._maybe_enter(_decision(_asig("SHORT")))           # SAR flipped
    assert [p.direction for p in pf.positions.values()] == ["SHORT"]
    assert pf.stats()["closed_trades"] == 1              # the long was closed


def test_active_position_does_not_block_main_strategy(tmp_path):
    ex, broker, pf = _exec(tmp_path)
    broker.on_tick("SOL/USD", 100.0)
    ex._maybe_enter(_decision(_asig("LONG")))
    main = Signal("trend_rider_4h", "SOL/USD", "LONG", 100.0, 98.0, 140.0, 0.7, [],
                  timeframe=H4)
    ex._maybe_enter(_decision(main))
    assert sorted(p.strategy for p in pf.positions.values()) == \
        ["psar_active_4h", "trend_rider_4h"]
    ex._maybe_enter(_decision(main))                     # still one per coin and account
    assert len(pf.positions) == 2
