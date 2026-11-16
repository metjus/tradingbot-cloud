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
