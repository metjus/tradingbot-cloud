"""Fees, runtime settings, budget sizing, adaptive exits, ticks, manual close."""

import asyncio
import math

import httpx
import pytest

from argus.analysis.regime import Regime
from argus.core.events import EventBus
from argus.db.database import Database
from argus.fees import round_trip_cost_pct, venue
from argus.marketdata.model import Candle, Tick, Timeframe
from argus.monitoring.health import HealthMonitor
from argus.risk import AccountState, RiskLimits, RiskSupervisor, net_reward_risk
from argus.runtime_settings import RuntimeSettings
from argus.strategy.base import Signal
from argus.trading.broker import BracketOrder, PaperBroker
from argus.trading.executor import ExecutionService
from argus.trading.exits import ExitParams, evaluate_exit
from argus.trading.portfolio import OpenPosition, Portfolio
from datetime import datetime, timezone


# --- fees -------------------------------------------------------------------
def test_verified_fee_table():
    assert venue("kraken").taker_pct == 0.80 and venue("kraken").maker_pct == 0.40
    assert venue("binance").taker_pct == 0.10
    assert venue("binance_bnb").taker_pct == 0.075
    # round trip: entry + exit taker + 2x slippage
    assert math.isclose(round_trip_cost_pct("kraken", slippage_pct=0.02), 1.64)
    assert math.isclose(round_trip_cost_pct("binance", slippage_pct=0.02), 0.24)
    assert venue("unknown").key == "kraken_futures"


def sig(entry=100.0, sl=99.0, tp=102.0, direction="LONG"):
    return Signal("t", "BTC/USD", direction, entry, sl, tp, 0.8, ["r"], Timeframe.M5)


def acct(**kw):
    base = dict(equity=10_000.0, day_start_equity=10_000.0)
    base.update(kw)
    return AccountState(**base)


def test_net_rr_includes_costs():
    _, _, rr0 = net_reward_risk(sig(), 0.0)
    _, _, rr = net_reward_risk(sig(), 0.24)   # cost 0.24 per unit
    assert math.isclose(rr0, 2.0)
    assert math.isclose(rr, (2.0 - 0.24) / (1.0 + 0.24))


def test_tp_that_does_not_cover_fees_is_vetoed():
    sup = RiskSupervisor(RiskLimits(round_trip_cost_pct=1.64))   # Kraken taker
    v = sup.check(sig(entry=100, sl=99.5, tp=101.2), acct())    # +1.2 gross < 1.64 cost
    assert not v.approved
    assert any("nepokryje poplatky" in x for x in v.vetoes)


def test_low_net_rr_vetoed_but_gross_ok():
    # gross RR 1.5 passes, but after Kraken costs net RR is far below 1.3
    sup = RiskSupervisor(RiskLimits(round_trip_cost_pct=1.64, min_rr=1.3))
    v = sup.check(sig(entry=100, sl=98, tp=103), acct())
    assert not v.approved
    assert any("po poplatkoch" in x for x in v.vetoes)


def test_sizing_risk_includes_costs():
    sup = RiskSupervisor(RiskLimits(risk_per_trade_pct=0.5, round_trip_cost_pct=0.24,
                                    max_position_notional_pct=100.0, min_rr=1.0))
    v = sup.check(sig(entry=100, sl=99, tp=103), acct())
    assert v.approved
    # 50 EUR risk / (1.0 stop + 0.24 cost) per unit
    assert math.isclose(v.quantity, 50.0 / 1.24)
    assert math.isclose(v.cost_estimate, v.notional * 0.0024)


def test_short_blocked_on_spot():
    sup = RiskSupervisor(RiskLimits(allow_short=False))
    v = sup.check(sig(entry=100, sl=101, tp=97, direction="SHORT"), acct())
    assert not v.approved and any("short" in x for x in v.vetoes)


def test_budget_and_position_caps():
    sup = RiskSupervisor(RiskLimits(budget_eur=1_000.0, max_position_eur=300.0,
                                    max_position_notional_pct=100.0, min_rr=1.0))
    v = sup.check(sig(entry=100, sl=99.9, tp=100.5), acct())
    assert v.approved and v.notional <= 300.0 + 1e-9
    # risk is a % of the BUDGET, not of the whole equity
    assert sup.capital(acct()) == 1_000.0
    # budget fully used -> veto
    v2 = sup.check(sig(), acct(open_notional=1_000.0))
    assert not v2.approved and any("rozpočet" in x for x in v2.vetoes)
    # only the remaining budget is available
    v3 = sup.check(sig(entry=100, sl=99.9, tp=100.5), acct(open_notional=900.0))
    assert v3.approved and v3.notional <= 100.0 + 1e-9


# --- runtime settings ---------------------------------------------------------
def test_runtime_settings_persist_validate_and_clamp(tmp_path):
    db = Database(tmp_path / "s.db")
    rs = RuntimeSettings(db)
    changed = rs.update({"budget_eur": 500, "max_position_eur": 900,
                         "risk_per_trade_pct": 5.0, "fee_venue": "kraken",
                         "symbols": "btc/usd, sol/usd"})
    assert rs["risk_per_trade_pct"] == 2.0            # hard ceiling
    assert rs["max_position_eur"] == 500               # cannot exceed budget
    assert rs["symbols"] == ["BTC/USD", "SOL/USD"]
    assert "fee_venue" in changed
    with pytest.raises(ValueError):
        rs.update({"fee_venue": "ftx"})
    with pytest.raises(ValueError):
        rs.update({"nonsense": 1})
    seen = []
    rs.on_change(seen.append)
    rs.update({"enter_threshold": 75})
    assert seen and seen[-1] == {"enter_threshold": 75.0}
    # reload from DB
    rs2 = RuntimeSettings(db)
    assert rs2["budget_eur"] == 500 and rs2["fee_venue"] == "kraken"


# --- adaptive exits -----------------------------------------------------------
def pos(direction="LONG", entry=100.0, sl=99.0, tp=103.0, sentiment=None):
    return OpenPosition(trade_id=1, client_order_id="c", symbol="BTC/USD",
                        direction=direction, quantity=1.0, entry_price=entry,
                        stop_loss=sl, take_profit=tp,
                        opened_at=datetime.now(timezone.utc), strategy="momentum",
                        risk_pct=0.5, initial_stop=sl, entry_sentiment=sentiment)


P = ExitParams(round_trip_cost_pct=0.24)


def test_risk_reduction_then_cost_aware_break_even():
    p = pos()
    acts = evaluate_exit(p, 100.5, P)        # +0.5R
    assert acts[0].kind == "stop" and math.isclose(acts[0].price, 99.5)
    acts = evaluate_exit(p, 100.8, P)        # +0.8R -> BE incl. costs (0.24) + 0.05R
    assert math.isclose(acts[0].price, 100.29)


def test_giveback_closes_profit_before_it_becomes_loss():
    p = pos()
    evaluate_exit(p, 101.5, P)               # +1.5R best
    acts = evaluate_exit(p, 100.5, P)        # back to +0.5R (gave back 67 %)
    assert acts and acts[0].kind == "close" and "ochrana zisku" in acts[0].reason


def test_sentiment_reversal():
    p = pos(sentiment=0.3)
    acts = evaluate_exit(p, 100.4, P, sentiment=-0.4)   # in profit -> close
    assert acts[0].kind == "close" and "sentiment" in acts[0].reason
    p2 = pos(sentiment=0.3)
    acts = evaluate_exit(p2, 99.8, P, sentiment=-0.4)   # in loss -> halve risk
    assert acts[0].kind == "stop" and math.isclose(acts[0].price, 99.5)


def test_regime_flip_against_position():
    p = pos()
    acts = evaluate_exit(p, 100.5, P, regime=Regime.STRONG_DOWNTREND)
    assert acts[0].kind == "close" and "režim" in acts[0].reason
    acts = evaluate_exit(pos(), 99.9, P, regime=Regime.STRONG_DOWNTREND)
    assert acts[0].kind == "stop"


def test_tp_extension_in_strong_aligned_trend():
    p = pos(tp=101.0)
    acts = evaluate_exit(p, 100.9, P, sentiment=0.4, regime=Regime.STRONG_UPTREND)
    tp = [a for a in acts if a.kind == "tp"]
    assert tp and math.isclose(tp[0].price, 101.5)


def test_short_mirror():
    p = pos(direction="SHORT", entry=100, sl=101, tp=97)
    acts = evaluate_exit(p, 99.2, P)          # +0.8R for a short
    assert acts[0].kind == "stop" and acts[0].price < 101


def test_disabled_exits_do_nothing():
    assert evaluate_exit(pos(), 102.0, ExitParams(enabled=False)) == []


# --- paper broker ticks + executor --------------------------------------------
def bar(sym, ts, o, h, lo, c):
    return Candle(symbol=sym, timeframe=Timeframe.M1, ts=ts, open=o, high=h,
                  low=lo, close=c, volume=1.0)


def test_paper_broker_fills_sl_tp_on_ticks():
    fills = []
    b = PaperBroker(fee_pct=0.0, slippage_pct=0.0, clock=lambda: 0.0)
    b.on_fill = fills.append
    b.on_tick("BTC/USD", 100.0)
    b.submit_bracket(BracketOrder("c1", "BTC/USD", "LONG", 1.0, 99.0, 102.0))
    b.on_tick("BTC/USD", 101.0)
    assert len(fills) == 1
    b.on_tick("BTC/USD", 102.1)
    assert fills[-1].kind == "take_profit" and fills[-1].price == 102.0
    assert b.update_take_profit("nope", 1.0) is False


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "e.db")
    bus = EventBus()
    broker = PaperBroker(fee_pct=0.0, slippage_pct=0.0, clock=lambda: 0.0)
    portfolio = Portfolio(db, "paper", "paper", 10_000.0)
    svc = ExecutionService(db, bus, HealthMonitor(bus), broker, portfolio, RiskSupervisor())
    return db, bus, broker, portfolio, svc


def enter(broker, svc, symbol="BTC/USD"):
    broker.on_tick(symbol, 100.0)
    svc._maybe_enter({"verdict": "enter", "regime": "strong_uptrend",
                      "best": {"score": 80, "factors": {},
                               "signal": {"symbol": symbol, "direction": "LONG",
                                          "entry": 100.0, "stop_loss": 99.0,
                                          "take_profit": 103.0, "strategy": "momentum",
                                          "reasons": ["r"]}},
                      "risk": {"approved": True, "quantity": 2.0, "risk_pct": 0.5}})


def test_manual_close_command(env):
    db, bus, broker, portfolio, svc = env
    enter(broker, svc)
    coid = next(iter(portfolio.positions))
    snap = portfolio.snapshot()["open_positions"][0]
    assert snap["invested"] == 200.0 and snap["client_order_id"] == coid
    assert portfolio.snapshot()["invested_total"] == 200.0
    out = svc.command("close_position", target=coid)
    assert out["ok"]
    assert portfolio.positions == {}
    assert portfolio.trade_history()[0]["exit_reason"] == "manual_close"
    assert svc.command("close_position", target=coid)["ok"] is False


def test_closing_one_position_leaves_the_others_open(env):
    db, bus, broker, portfolio, svc = env
    for sym in ("BTC/USD", "ETH/USD", "SOL/USD"):
        enter(broker, svc, sym)
    assert len(portfolio.positions) == 3
    target = next(c for c, p in portfolio.positions.items() if p.symbol == "ETH/USD")
    out = svc.command("close_position", target=target, command_id="cloud-1")
    assert out["ok"] and out["symbol"] == "ETH/USD" and out["closed"] == 1
    assert sorted(p.symbol for p in portfolio.positions.values()) == ["BTC/USD", "SOL/USD"]
    assert svc.state == "running"
    # the same cloud command delivered twice closes nothing more
    assert svc.command("close_position", target=target, command_id="cloud-1")["duplicate"]
    assert svc.command("close_position", target=None)["ok"] is False
    assert len(portfolio.positions) == 2


def test_emergency_stop_is_labelled_in_the_history(env):
    db, bus, broker, portfolio, svc = env
    for sym in ("BTC/USD", "ETH/USD"):
        enter(broker, svc, sym)
    out = svc.command("emergency_stop")
    assert out["closed"] == 2 and out["failed"] == 0
    assert portfolio.positions == {} and svc.state == "stopped"
    assert {t["exit_reason"] for t in portfolio.trade_history()} == {"emergency_stop"}


def test_executor_adaptive_close_on_giveback(env):
    db, bus, broker, portfolio, svc = env
    enter(broker, svc)
    svc._manage_stops("BTC/USD", 101.5)   # +1.5R peak, stop trails
    p = next(iter(portfolio.positions.values()))
    assert p.stop_loss > 100.0             # already locked in profit
    broker.on_tick("BTC/USD", 100.6)       # fell back below the trailed stop
    assert portfolio.positions == {}
    t = portfolio.trade_history()[0]
    assert t["pl"] > 0                     # a winner stayed a winner


async def test_tick_loop_marks_positions(env):
    db, bus, broker, portfolio, svc = env
    svc.start()
    enter(broker, svc)
    bus.publish("tick", Tick(symbol="BTC/USD", price=100.4, volume=0, ts=1.0))
    await asyncio.sleep(0.05)
    svc.stop()
    p = next(iter(portfolio.positions.values()))
    assert p.last_price == 100.4


# --- kraken ticker parsing ----------------------------------------------------
async def test_kraken_ticker_maps_pairs():
    from argus.marketdata.providers import KrakenTicker

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/AssetPairs"):
            return httpx.Response(200, json={"error": [], "result": {
                "XXBTZUSD": {"altname": "XBTUSD"}, "SOLUSD": {"altname": "SOLUSD"}}})
        return httpx.Response(200, json={"error": [], "result": {
            "XXBTZUSD": {"c": ["64000.5", "0.1"], "b": ["64000.0", "1"], "a": ["64001.0", "1"]},
            "SOLUSD": {"c": ["70.1", "3"], "b": ["70.0", "1"], "a": ["70.2", "1"]}}})

    tk = KrakenTicker(["BTC/USD", "SOL/USD"], lambda t: None, HealthMonitor(EventBus()))
    tk._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ticks = {t.symbol: t for t in await tk.poll_once()}
    await tk.stop()
    assert ticks["BTC/USD"].price == 64000.5 and ticks["SOL/USD"].bid == 70.0
    assert math.isclose(ticks["BTC/USD"].spread_pct, 1.0 / 64000.5 * 100, rel_tol=1e-3)


# --- forming candle -------------------------------------------------------------
def test_forming_bar_from_ticks(tmp_path):
    from argus.config import Settings
    from argus.engine import Engine
    eng = Engine(Settings(data_dir=tmp_path / "d", crypto_symbols=[], news_enabled=False,
                          ml_train_enabled=False))
    t0 = 1_800_000_000  # aligned to 5m
    for i, price in enumerate([100.0, 101.0, 99.5, 100.5]):
        eng._on_tick(Tick(symbol="X/USD", price=price, volume=0, ts=t0 + 60 + i))
    m1 = eng.forming_bar("X/USD", Timeframe.M1)
    assert (m1.open, m1.high, m1.low, m1.close) == (100.0, 101.0, 99.5, 100.5)
    assert m1.complete is False
    # higher TF merges the closed 1m bar of the same bucket with the live one
    eng.aggregator.add_m1(Candle(symbol="X/USD", timeframe=Timeframe.M1, ts=t0,
                                 open=98.0, high=102.5, low=97.0, close=100.0, volume=1))
    m5 = eng.forming_bar("X/USD", Timeframe.M5)
    assert m5.ts == t0 and m5.open == 98.0 and m5.high == 102.5
    assert m5.low == 97.0 and m5.close == 100.5


def test_open_paper_positions_survive_restart(tmp_path):
    """After a restart the paper broker must know the open positions again,
    otherwise SL/TP and manual close would silently stop working."""
    from argus.config import Settings
    from argus.engine import Engine
    cfg = dict(data_dir=tmp_path / "d", crypto_symbols=[], news_enabled=False,
               ml_train_enabled=False)
    e1 = Engine(Settings(**cfg))
    e1.broker.on_tick("BTC/USD", 100.0)
    e1.executor._maybe_enter({"verdict": "enter", "regime": "x", "best": {"score": 80,
        "signal": {"symbol": "BTC/USD", "direction": "LONG", "entry": 100.0,
                   "stop_loss": 99.0, "take_profit": 103.0, "strategy": "m", "reasons": []}},
        "risk": {"approved": True, "quantity": 1.0, "risk_pct": 0.5}})
    assert len(e1.portfolio.positions) == 1
    e2 = Engine(Settings(**cfg))                      # "restart"
    assert len(e2.broker.positions()) == 1
    e2.broker.on_tick("BTC/USD", 98.9)                # stop hit after restart
    assert e2.portfolio.positions == {}
    assert e2.portfolio.trade_history()[0]["exit_reason"] == "stop_loss"


def test_break_even_never_above_market_with_high_fees():
    """Kraken-level costs: cost-inclusive break-even is above the current
    price -> must NOT be placed (it would stop the trade out instantly)."""
    p = pos(entry=64000, sl=63500, tp=66000)
    params = ExitParams(round_trip_cost_pct=1.64)
    acts = evaluate_exit(p, 64550, params)          # +1.1R but below BE+costs
    stops = [a.price for a in acts if a.kind == "stop"]
    assert all(st < 64550 for st in stops)
    assert not any(a.kind == "close" for a in acts)


def test_stop_fills_at_market_after_jump():
    fills = []
    b = PaperBroker(fee_pct=0.0, slippage_pct=0.0, clock=lambda: 0.0)
    b.on_fill = fills.append
    b.on_tick("X/USD", 100.0)
    b.submit_bracket(BracketOrder("c", "X/USD", "LONG", 1.0, 99.0, 103.0))
    b.on_tick("X/USD", 98.2)                        # jumped through the stop
    assert fills[-1].kind == "stop_loss" and fills[-1].price == 98.2
    b2 = PaperBroker(fee_pct=0.0, slippage_pct=0.0, clock=lambda: 0.0)
    f2 = []
    b2.on_fill = f2.append
    b2.on_tick("X/USD", 100.0)
    b2.submit_bracket(BracketOrder("c", "X/USD", "LONG", 1.0, 99.0, 103.0))
    b2.on_candle(bar("X/USD", 60, 98.5, 99.5, 98.0, 99.2))   # gap open below stop
    assert f2[-1].price == 98.5


def test_pause_and_emergency_stop_survive_restart(tmp_path):
    from argus.config import Settings
    from argus.engine import Engine
    cfg = dict(data_dir=tmp_path / "d", crypto_symbols=[], news_enabled=False,
               ml_train_enabled=False)
    e1 = Engine(Settings(**cfg))
    assert e1.executor.command("pause")["state"] == "paused"
    e2 = Engine(Settings(**cfg))
    assert e2.executor.state == "paused"
    e2.executor.command("emergency_stop")
    e3 = Engine(Settings(**cfg))
    assert e3.executor.state == "stopped" and e3.risk.halted
    e3.executor.command("start")
    e4 = Engine(Settings(**cfg))
    assert e4.executor.state == "running" and not e4.risk.halted


def test_v11_settings_migrate_to_kraken_futures(tmp_path):
    from argus.db.database import Database
    from argus.db.schema import AppSetting
    from argus.runtime_settings import KEY, RuntimeSettings
    db = Database(tmp_path / "m.db")
    with db.session() as s:   # settings as saved by 1.1 (no leverage key)
        s.add(AppSetting(key=KEY, value={"fee_venue": "binance",
                                         "allow_short": False,
                                         "budget_eur": 500.0}))
        s.commit()
    rs = RuntimeSettings(db)
    assert rs["fee_venue"] == "kraken_futures"
    assert rs["allow_short"] is True
    assert rs["budget_eur"] == 500.0          # user's own values survive
    # the migration persisted: a reload does not repeat it
    rs2 = RuntimeSettings(db)
    assert rs2["fee_venue"] == "kraken_futures"
    with db.session() as s:
        stored = s.get(AppSetting, KEY).value
    assert "leverage" in stored


def test_v12_explicit_spot_choice_is_kept(tmp_path):
    from argus.db.database import Database
    from argus.runtime_settings import RuntimeSettings
    db = Database(tmp_path / "m2.db")
    rs = RuntimeSettings(db)
    rs.update({"fee_venue": "kraken"})        # deliberate spot choice on 1.2
    rs2 = RuntimeSettings(db)
    assert rs2["fee_venue"] == "kraken"


def test_v13_risk_bump_only_for_untouched_default(tmp_path):
    from argus.db.database import Database
    from argus.db.schema import AppSetting
    from argus.runtime_settings import KEY, RuntimeSettings
    # 1.2 store with the old default risk -> 1 %, then the focus profile -> 2 %
    db = Database(tmp_path / "a.db")
    with db.session() as s:
        s.add(AppSetting(key=KEY, value={"fee_venue": "kraken_futures",
                                         "leverage": 3.0,
                                         "risk_per_trade_pct": 0.5}))
        s.commit()
    assert RuntimeSettings(db)["risk_per_trade_pct"] == 2.0
    # a user-tuned value survives
    db2 = Database(tmp_path / "b.db")
    with db2.session() as s:
        s.add(AppSetting(key=KEY, value={"fee_venue": "kraken_futures",
                                         "leverage": 3.0,
                                         "risk_per_trade_pct": 0.7}))
        s.commit()
    assert RuntimeSettings(db2)["risk_per_trade_pct"] == 0.7


def test_ai_budget_default_and_migration(tmp_path):
    from argus.db.database import Database
    from argus.db.schema import AppSetting
    from argus.runtime_settings import KEY, RuntimeSettings
    assert RuntimeSettings(Database(tmp_path / "n.db"))["llm_daily_budget_usd"] == 2.0
    db = Database(tmp_path / "o.db")          # pre-AI-trader store, old default
    with db.session() as s:
        s.add(AppSetting(key=KEY, value={"leverage": 3.0, "entry_order": "limit",
                                         "llm_daily_budget_usd": 1.0}))
        s.commit()
    assert RuntimeSettings(db)["llm_daily_budget_usd"] == 2.0
    db2 = Database(tmp_path / "p.db")         # user-chosen value survives
    with db2.session() as s:
        s.add(AppSetting(key=KEY, value={"leverage": 3.0, "entry_order": "limit",
                                         "llm_daily_budget_usd": 0.5}))
        s.commit()
    assert RuntimeSettings(db2)["llm_daily_budget_usd"] == 0.5


def test_focus_profile_migration_only_touches_old_defaults(tmp_path):
    from argus.db.database import Database
    from argus.db.schema import AppSetting
    from argus.runtime_settings import KEY, RuntimeSettings
    db = Database(tmp_path / "f.db")
    with db.session() as s:
        s.add(AppSetting(key=KEY, value={"leverage": 3.0, "entry_order": "limit",
                                         "ai_trader_enabled": True,
                                         "max_symbols": 15, "max_open_positions": 4,
                                         "risk_per_trade_pct": 1.0}))
        s.commit()
    rs = RuntimeSettings(db)
    assert (rs["max_symbols"], rs["max_open_positions"], rs["risk_per_trade_pct"]) == (5, 1, 2.0)
    assert rs["risk_guard"] is True
    db2 = Database(tmp_path / "g.db")            # user's own values survive
    with db2.session() as s:
        s.add(AppSetting(key=KEY, value={"leverage": 3.0, "entry_order": "limit",
                                         "ai_trader_enabled": True,
                                         "max_symbols": 8, "max_open_positions": 2,
                                         "risk_per_trade_pct": 0.7}))
        s.commit()
    rs2 = RuntimeSettings(db2)
    assert (rs2["max_symbols"], rs2["max_open_positions"], rs2["risk_per_trade_pct"]) == (8, 2, 0.7)
    rs2.update({"risk_per_trade_pct": 1.5})      # no second migration later
    assert RuntimeSettings(db2)["risk_per_trade_pct"] == 1.5


def test_capital_is_set_from_the_ui_and_survives_restart(tmp_path):
    """Kapitál (paper equity) is editable: a deposit/withdrawal that keeps the
    trade history and total P/L; the budget follows when it covered it all."""
    from fastapi.testclient import TestClient

    from argus.api.server import create_app
    from argus.config import Settings
    from argus.engine import Engine
    cfg = dict(data_dir=tmp_path / "d", crypto_symbols=[], news_enabled=False,
               ml_train_enabled=False)
    e1 = Engine(Settings(**cfg))
    e1.broker.on_tick("BTC/USD", 100.0)
    e1.executor._maybe_enter({"verdict": "enter", "regime": "x", "best": {"score": 80,
        "signal": {"symbol": "BTC/USD", "direction": "LONG", "entry": 100.0,
                   "stop_loss": 99.0, "take_profit": 103.0, "strategy": "m", "reasons": []}},
        "risk": {"approved": True, "quantity": 1.0, "risk_pct": 0.5}})
    e1.broker.on_tick("BTC/USD", 98.9)                    # a small loss
    lost = e1.portfolio.equity - e1.portfolio.start_equity
    assert lost < 0
    app = create_app(e1.db, e1.bus, e1.health, e1.info(), executor=e1.executor,
                     portfolio=e1.portfolio, decision_engine=e1.decision_engine,
                     settings=e1.settings, engine=e1)
    c = TestClient(app)
    assert c.get("/api/settings").json()["values"]["paper_capital_eur"] == round(
        e1.portfolio.equity, 2)
    r = c.post("/api/settings", json={"paper_capital_eur": 25_000, "leverage": 5})
    assert r.status_code == 200 and r.json()["ok"], r.text
    assert r.json()["changed"]["paper_capital_eur"] == 25_000
    p = c.get("/api/portfolio").json()
    assert p["equity"] == 25_000 and p["total_pl"] == round(lost, 2)
    assert p["day_pl"] == round(lost, 2)                  # the deposit is not profit
    assert e1.runtime["budget_eur"] == 25_000            # budget covered everything
    assert e1.risk.limits.budget_eur == 25_000 and e1.risk.limits.leverage == 5
    assert e1.portfolio.account_state().equity == 25_000  # risk is sized from it
    assert c.post("/api/settings", json={"paper_capital_eur": 1}).status_code == 400
    assert e1.portfolio.equity == 25_000
    # a budget smaller than the capital is the user's choice and stays
    c.post("/api/settings", json={"budget_eur": 5_000})
    c.post("/api/settings", json={"paper_capital_eur": 30_000})
    assert e1.runtime["budget_eur"] == 5_000
    e2 = Engine(Settings(**cfg))                          # restart
    assert e2.portfolio.equity == 30_000
    assert round(e2.portfolio.snapshot()["total_pl"], 2) == round(lost, 2)
