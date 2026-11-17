"""AI trader: the model proposes levels, deterministic code decides."""

import asyncio
import time

from argus.ai_trader import STRATEGY, AITrader, build_brief, validate
from argus.analysis.service import DECISION_EVENT
from argus.core.events import EventBus
from argus.db.database import Database
from argus.marketdata.model import Candle, Timeframe
from argus.monitoring.health import HealthMonitor
from argus.risk import RiskLimits, RiskSupervisor
from argus.runtime_settings import RuntimeSettings
from argus.trading.broker import PaperBroker
from argus.trading.executor import ExecutionService
from argus.trading.portfolio import Portfolio

COST = 0.09   # maker entry + taker exit + slippage, % round trip


# --- validation: the model can only propose, never bypass ---------------------

def test_valid_long_limit_below_price():
    sig, why = validate({"action": "long_limit", "entry": 64800, "stop_loss": 64300,
                         "take_profit": 65900, "confidence": 0.7, "reason": "dip",
                         "next_check_minutes": 45}, 65500, COST)
    assert sig is not None, why
    assert sig.direction == "LONG" and sig.sleeve == "ai" and sig.exploration
    assert sig.expire_s == 45 * 60 and sig.strategy == STRATEGY


def test_model_chooses_when_to_look_again_within_bounds():
    from argus.ai_trader import check_minutes
    assert check_minutes({"next_check_minutes": 15}) == 15
    assert check_minutes({"next_check_minutes": 100}) == 105      # 15m grid
    assert check_minutes({"next_check_minutes": 1}) == 15         # floor
    assert check_minutes({"next_check_minutes": 10_000}) == 240   # ceiling
    assert check_minutes({}) == 30                                # default


def test_long_limit_above_price_is_rejected():
    sig, why = validate({"action": "long_limit", "entry": 66000, "stop_loss": 65500,
                         "take_profit": 67500, "confidence": 0.7, "reason": ""},
                        65500, COST)
    assert sig is None and "pod cenou" in why


def test_short_limit_must_be_above_price():
    ok, _ = validate({"action": "short_limit", "entry": 66600, "stop_loss": 67100,
                      "take_profit": 65500, "confidence": 0.6, "reason": ""}, 65500, COST)
    assert ok is not None and ok.direction == "SHORT"
    bad, why = validate({"action": "short_limit", "entry": 65000, "stop_loss": 65600,
                         "take_profit": 64000, "confidence": 0.6, "reason": ""}, 65500, COST)
    assert bad is None and "nad cenou" in why


def test_bounds_on_distance_stop_and_reward():
    far, why = validate({"action": "long_limit", "entry": 60000, "stop_loss": 59500,
                         "take_profit": 62000, "confidence": 1, "reason": ""}, 65500, COST)
    assert far is None and "3 %" in why
    tight, why = validate({"action": "long_limit", "entry": 65400, "stop_loss": 65350,
                           "take_profit": 65600, "confidence": 1, "reason": ""}, 65500, COST)
    assert tight is None and "stop" in why
    greedy_small, why = validate({"action": "long_limit", "entry": 65000, "stop_loss": 64500,
                                  "take_profit": 65300, "confidence": 1, "reason": ""},
                                 65500, COST)
    assert greedy_small is None and "zisk/riziko" in why


def test_none_is_not_an_order():
    sig, why = validate({"action": "none", "entry": 0, "stop_loss": 0, "take_profit": 0,
                         "confidence": 0, "reason": "chop"}, 65500, COST)
    assert sig is None and why == ""


def test_brief_is_numbers_only():
    bars = [Candle("BTC/USD", Timeframe.M15, 1_700_000_000 + i * 900,
                   100 + i * 0.1, 100.5 + i * 0.1, 99.5 + i * 0.1, 100.2 + i * 0.1, 10.0)
            for i in range(120)]
    txt = build_brief("BTC/USD", bars, [], [], 112.0, 0.00001, None, [])
    assert "COIN BTC/USD" in txt and "last 48 x 15m" in txt
    assert txt.count("\n") > 48


# --- end to end with a fake model -----------------------------------------------

class FakeLLM:
    def __init__(self, answer):
        self.answer = answer
        self.calls = 0

    def available(self):
        return True, ""

    def trader_model(self, override=""):
        return "fake"

    async def structured(self, system, schema, prompt, **kw):
        self.calls += 1
        return self.answer


def _setup(tmp_path, answer):
    db = Database(tmp_path / "a.db")
    bus = EventBus()
    health = HealthMonitor(bus)
    runtime = RuntimeSettings(db)
    risk = RiskSupervisor(RiskLimits(round_trip_cost_pct=COST, allow_short=True,
                                     leverage=3.0, max_leverage=10.0,
                                     maintenance_margin_pct=5.0))
    broker = PaperBroker(fee_pct=0.05, slippage_pct=0.0, maker_fee_pct=0.02)
    portfolio = Portfolio(db, broker_name="paper", mode="paper", start_equity=10_000.0)
    ex = ExecutionService(db, bus, health, broker, portfolio, risk)
    # 15m history in the DB
    now = int(time.time()) // 900 * 900
    bars = [Candle("BTC/USD", Timeframe.M15, now - (120 - i) * 900, 65000 + i, 65100 + i,
                   64900 + i, 65050 + i, 5.0) for i in range(120)]
    db.upsert_candles(bars)
    broker.on_tick("BTC/USD", 65500.0)
    llm = FakeLLM(answer)
    ai = AITrader(db, bus, health, llm, runtime, risk, ex, portfolio,
                  price_provider=lambda s: 65500.0,
                  account_provider=portfolio.account_state)
    return ai, ex, bus, broker, portfolio, llm


async def test_decision_publishes_a_limit_entry_and_replaces_old(tmp_path):
    ai, ex, bus, broker, portfolio, llm = _setup(tmp_path, {
        "action": "long_limit", "entry": 64800, "stop_loss": 64200,
        "take_profit": 66000, "confidence": 0.7, "reason": "breakout forming"})
    q = bus.subscribe(DECISION_EVENT)
    ex.start()
    rec = await ai.decide("BTC/USD")
    assert "limit" in rec["result"] and "30 min" in rec["result"]
    assert ai._next_due["BTC/USD"] > time.time()
    d = q.get_nowait().data
    assert d["source"] == "ai_trader" and d["risk"]["approved"]
    assert d["best"]["signal"]["sleeve"] == "ai"
    await asyncio.sleep(0.05)                       # executor consumes it
    assert len(broker.pending_orders()) == 1
    assert broker.pending_orders()[0].entry == "limit"
    # 30 min later a new view: the old resting order is cancelled first
    llm.answer = {"action": "none", "entry": 0, "stop_loss": 0, "take_profit": 0,
                  "confidence": 0.2, "reason": "chop"}
    rec = await ai.decide("BTC/USD")
    assert "zrušený" in rec["result"]
    assert broker.pending_orders() == []
    ex.stop()


async def test_open_ai_position_can_be_closed_by_the_model(tmp_path):
    ai, ex, bus, broker, portfolio, llm = _setup(tmp_path, {
        "action": "long_limit", "entry": 65500, "stop_loss": 65000,
        "take_profit": 66800, "confidence": 0.7, "reason": "x"})
    ex.start()
    await ai.decide("BTC/USD")
    await asyncio.sleep(0.05)
    broker.on_tick("BTC/USD", 65490.0)               # limit fills
    assert any(p.strategy == STRATEGY for p in portfolio.positions.values())
    llm.answer = {"action": "close", "entry": 0, "stop_loss": 0, "take_profit": 0,
                  "confidence": 0.8, "reason": "momentum faded"}
    rec = await ai.decide("BTC/USD")
    assert rec["result"] == "pozícia zatvorená"
    assert not portfolio.positions
    ex.stop()


async def test_without_key_it_waits_and_spends_nothing(tmp_path):
    ai, ex, bus, broker, portfolio, llm = _setup(tmp_path, None)
    llm.available = lambda: (False, "chýba API kľúč")
    rec = await ai.decide("BTC/USD")
    assert rec["action"] == "skip" and "kľúč" in rec["reason"]
    assert llm.calls == 0


def test_brief_includes_tradingview_indicator_states():
    import math
    bars = [Candle("BTC/USD", Timeframe.M15, 1_700_000_000 + i * 900,
                   100 + 5 * math.sin(i / 9), 101 + 5 * math.sin(i / 9),
                   99 + 5 * math.sin(i / 9), 100.3 + 5 * math.sin(i / 9), 10.0 + i % 7)
            for i in range(200)]
    txt = build_brief("BTC/USD", bars, bars, [], 100.0, None, None, [])
    line = next(l for l in txt.splitlines() if l.startswith("TradingView indicators 15m"))
    for name in ("Supertrend", "UT Bot", "PSAR", "Hull55", "Ichimoku", "Squeeze",
                 "WaveTrend", "MACD"):
        assert name in line


def test_tv_indicators_match_trend_direction():
    from argus.ta.tv import hull_slope, psar, supertrend, utbot
    import numpy as np
    n = 200
    c = np.linspace(100, 160, n)                 # clean uptrend
    h, l = c + 0.5, c - 0.5
    assert supertrend(h, l, c) == 1 and utbot(h, l, c) == 1
    assert psar(h, l) == 1 and hull_slope(c) == 1
    c2 = c[::-1].copy()                          # clean downtrend
    assert supertrend(c2 + 0.5, c2 - 0.5, c2) == -1 and psar(c2 + 0.5, c2 - 0.5) == -1
