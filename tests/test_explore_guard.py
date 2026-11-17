"""The trial sleeve switches itself off once it loses after fees."""

from datetime import datetime, timezone

from argus.config import Settings
from argus.db.schema import Trade
from argus.engine import Engine


def _engine(tmp_path):
    return Engine(Settings(data_dir=tmp_path / "d", crypto_symbols=[],
                           stock_symbols=[], news_enabled=False,
                           ml_train_enabled=False))


def _add_trades(engine, pls, fee=0.5, strategy="pullback_short_4h"):
    with engine.db.session() as s:
        for i, pl in enumerate(pls):
            s.add(Trade(client_order_id=f"x{i}-{pl}", broker="paper", mode="paper",
                        symbol="SOL/USD", direction="SHORT", strategy=strategy,
                        entry_ts=datetime.now(timezone.utc),
                        exit_ts=datetime.now(timezone.utc), entry_price=100.0,
                        exit_price=99.0, quantity=1.0, pl=pl, fees=fee,
                        status="closed"))
        s.commit()


def test_guard_keeps_a_winning_trial_running(tmp_path):
    e = _engine(tmp_path)
    _add_trades(e, [5.0, -2.0] * 12)          # 24 trades, PF 2.5 after fees
    e._check_explore()
    assert e.runtime["explore_enabled"] is True
    st = e.explore_status()
    assert st["trades"] == 24 and st["fees"] == 12.0
    assert st["gross_pl"] == st["net_pl"] + st["fees"]


def test_guard_turns_off_after_20_losing_trades(tmp_path):
    e = _engine(tmp_path)
    _add_trades(e, [1.0, -1.5] * 10)          # 20 trades, PF 0.67
    e._check_explore()
    assert e.runtime["explore_enabled"] is False
    assert "prerába" in e.explore_status()["disabled_reason"]
    assert e.decision_engine.explore_enabled is False   # applied live


def test_guard_hard_loss_limit_fires_early(tmp_path):
    e = _engine(tmp_path)
    e.runtime.update({"budget_eur": 1000.0})
    _add_trades(e, [-6.0, -5.0])              # -11 EUR > 1 % of 1000
    e._check_explore()
    assert e.runtime["explore_enabled"] is False
    assert "limit" in e.explore_status()["disabled_reason"]


def test_validated_trades_do_not_count(tmp_path):
    e = _engine(tmp_path)
    _add_trades(e, [-50.0] * 25, strategy="trend_rider_4h")
    e._check_explore()
    assert e.runtime["explore_enabled"] is True
    assert e.explore_status()["trades"] == 0


def test_ai_account_has_its_own_kill_switch(tmp_path):
    e = _engine(tmp_path)
    _add_trades(e, [1.0, -1.5] * 10, strategy="ai_trader")    # PF 0.67
    e._check_explore()
    assert e.runtime["ai_trader_enabled"] is False
    assert e.runtime["explore_enabled"] is True                 # untouched
    st = e.explore_status("ai")
    assert st["trades"] == 20 and "prerába" in st["disabled_reason"]
    assert e.explore_status("explore")["trades"] == 0           # separate books



def test_risk_guard_cuts_risk_when_live_results_disagree(tmp_path):
    e = _engine(tmp_path)
    e.runtime.update({"risk_per_trade_pct": 2.0})
    assert e.risk.limits.effective_risk_pct() == 2.0
    _add_trades(e, [10.0, -15.0] * 10, strategy="trend_rider_4h")   # PF 0.67 after 20
    assert e.check_risk_guard() is True
    assert e.risk.limits.effective_risk_pct() == 1.0
    st = e.risk_status()
    assert st["guard_active"] and st["risk_setting_pct"] == 2.0 and st["trades"] == 20
    # recovery: strong results lift the guard again
    _add_trades(e, [40.0] * 15, strategy="trend_rider_4h")
    assert e.check_risk_guard() is False
    assert e.risk.limits.effective_risk_pct() == 2.0


def test_risk_guard_ignores_trial_and_legacy_trades(tmp_path):
    e = _engine(tmp_path)
    _add_trades(e, [-30.0] * 30, strategy="momentum")            # old 5m spot strategy
    _add_trades(e, [-5.0] * 30, strategy="pullback_short_4h")    # trial account
    assert e.check_risk_guard() is False
    assert e.main_strategy_stats()["trades"] == 0


def test_risk_guard_drawdown_trigger(tmp_path):
    e = _engine(tmp_path)
    _add_trades(e, [-700.0, -700.0, -700.0], strategy="trend_rider_4h")  # 21 % DD of 10k
    assert e.check_risk_guard() is True
    assert "prepad" in e.risk_status()["guard_reason"]


def test_only_top_coins_may_trade_and_rerank(tmp_path):
    import time as _t
    from argus.marketdata.model import Candle, Timeframe
    e = _engine(tmp_path)
    e.crypto_symbols = ["BTC/USD", "ETH/USD", "SOL/USD", "DOGE/USD"]
    e.runtime.update({"max_symbols": 2})
    e.decision_engine.allowed_symbols = {"BTC/USD", "SOL/USD"}
    now = int(_t.time()) // 3600 * 3600
    vols = {"BTC/USD": 100.0, "ETH/USD": 90.0, "SOL/USD": 5.0, "DOGE/USD": 1.0}
    for sym, v in vols.items():
        e.db.upsert_candles([Candle(sym, Timeframe.H1, now - k * 3600, 10, 10, 10, 10, v)
                             for k in range(1, 24 * 25)])
    assert e.rerank_universe() == ["BTC/USD", "ETH/USD"]
    assert e.decision_engine.allowed_symbols == {"BTC/USD", "ETH/USD"}


def test_active_account_is_judged_on_loss_not_on_a_short_pf(tmp_path):
    e = _engine(tmp_path)
    e.runtime.update({"budget_eur": 1000.0})
    _add_trades(e, [1.0, -1.2] * 300, strategy="trend_st10_3_4h_f")   # 600 trades, PF 0.83
    e._check_explore()
    assert e.runtime["active_enabled"] is True          # clustered wins: too early
    _add_trades(e, [-200.0], strategy="trend_ema21_50_4h_f")         # net -260 < -25 %
    e._check_explore()
    assert e.runtime["active_enabled"] is False
    st = e.explore_status("active")
    assert st["min_trades"] == 3000 and "limit" in st["disabled_reason"]
    assert st["systems"] == 12 and e.runtime["explore_enabled"] is True


def test_market_vol_from_local_candles(tmp_path):
    import time as _t
    from argus.marketdata.model import Candle, Timeframe
    e = _engine(tmp_path)
    e.crypto_symbols = ["BTC/USD", "ETH/USD"]
    now = int(_t.time()) // 86400 * 86400
    for sym in e.crypto_symbols:
        bars, p = [], 100.0
        for k in range(24 * 31):
            ts = now - (24 * 31 - k) * 3600
            if ts % 86400 == 82800:
                p *= 1.07 if (ts // 86400) % 2 else 1 / 1.07    # +-7 % a day: wild
            bars.append(Candle(sym, Timeframe.H1, ts, p, p, p, p, 1.0))
        e.db.upsert_candles(bars)
    k = e.update_market_vol()
    assert 0.25 <= k < 0.6 and e.decision_engine.active_vol_k == k
