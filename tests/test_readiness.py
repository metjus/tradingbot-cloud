from datetime import datetime, timedelta, timezone

from argus.db.database import Database
from argus.db.schema import Trade
from argus.readiness import Criteria, evaluate_readiness


def add_trade(db, i, pl, days_ago):
    ts = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with db.session() as s:
        s.add(Trade(client_order_id=f"t{i}", broker="paper", mode="paper",
                    symbol="BTC/USD", direction="LONG", strategy="momentum",
                    status="closed", pl=pl, entry_ts=ts, exit_ts=ts,
                    entry_price=100, exit_price=100 + pl, quantity=1.0,
                    stop_loss=99, take_profit=102))
        s.commit()


def test_empty_db_not_ready(tmp_path):
    db = Database(tmp_path / "r.db")
    rep = evaluate_readiness(db, 10_000.0)
    assert not rep.ready
    assert all(not c.passed for c in rep.checks if "kritické" not in c.name.lower())


def test_good_track_record_passes(tmp_path):
    db = Database(tmp_path / "g.db")
    # 120 trades over 20 days, PF 2.0 (60 wins +10, 60 losses -5)
    for i in range(120):
        add_trade(db, i, 10.0 if i % 2 == 0 else -5.0, days_ago=20 - (i % 20))
    rep = evaluate_readiness(db, 10_000.0, Criteria())
    assert rep.ready, [c.as_dict() for c in rep.checks if not c.passed]


def test_bad_pf_fails(tmp_path):
    db = Database(tmp_path / "b.db")
    for i in range(120):
        add_trade(db, i, 5.0 if i % 2 == 0 else -6.0, days_ago=20 - (i % 20))
    rep = evaluate_readiness(db, 10_000.0)
    assert not rep.ready
    failed = {c.name for c in rep.checks if not c.passed}
    assert any("Profit factor" in n for n in failed)


def test_critical_errors_block(tmp_path):
    db = Database(tmp_path / "c.db")
    for i in range(120):
        add_trade(db, i, 10.0 if i % 2 == 0 else -5.0, days_ago=20 - (i % 20))
    db.audit("system", "boom", severity="critical")
    rep = evaluate_readiness(db, 10_000.0)
    assert not rep.ready
    assert any("kritické" in c.name.lower() and not c.passed for c in rep.checks)
