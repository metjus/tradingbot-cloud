from argus.db.database import Database
from argus.marketdata.model import Candle, Timeframe


def make_db(tmp_path):
    return Database(tmp_path / "test.db")


def candle(ts: int, close: float = 100.0) -> Candle:
    return Candle(symbol="BTC/USD", timeframe=Timeframe.M1, ts=ts, open=99,
                  high=101, low=98, close=close, volume=1.5, source="test")


def test_candle_upsert_and_query(tmp_path):
    db = make_db(tmp_path)
    n = db.upsert_candles([candle(60), candle(120), candle(180)])
    assert n == 3
    # upsert same ts with new close -> updated, not duplicated
    db.upsert_candles([candle(120, close=555.0)])
    rows = db.get_candles("BTC/USD", Timeframe.M1, limit=10)
    assert [c.ts for c in rows] == [60, 120, 180]  # ascending
    assert rows[1].close == 555.0
    assert db.last_candle_ts("BTC/USD", Timeframe.M1) == 180


def test_incomplete_candles_not_stored(tmp_path):
    db = make_db(tmp_path)
    c = candle(60)
    c.complete = False
    assert db.upsert_candles([c]) == 0


def test_audit_log_roundtrip(tmp_path):
    db = make_db(tmp_path)
    db.audit("system", "hello", data={"a": 1})
    db.audit("risk", "limit hit", severity="warning")
    rows = db.recent_audit(limit=10)
    assert len(rows) == 2
    assert rows[0].category == "risk"  # newest first
    assert rows[1].data == {"a": 1}


def test_schema_version_written(tmp_path):
    from sqlalchemy import select
    from argus.db.schema import SchemaMeta
    db = make_db(tmp_path)
    with db.session() as s:
        v = s.execute(select(SchemaMeta).where(SchemaMeta.key == "schema_version")).scalar_one()
        assert v.value == "1"
