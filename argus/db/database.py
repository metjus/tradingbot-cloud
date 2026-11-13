"""Database access: engine factory, migrations, candle store and audit log.

SQLite in WAL mode. Writes happen from the engine's single asyncio thread
via short synchronous transactions (SQLite is fast enough for our volume:
a few rows per second at peak). A simple version check prepares the ground
for real migrations when the schema evolves.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from ..marketdata.model import Candle, Timeframe
from .schema import SCHEMA_VERSION, AuditLog, Base, CandleRow, SchemaMeta

log = logging.getLogger(__name__)


def make_engine(db_path: Path | str) -> Engine:
    if isinstance(db_path, Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    eng = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return eng


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        meta = s.get(SchemaMeta, "schema_version")
        if meta is None:
            s.add(SchemaMeta(key="schema_version", value=str(SCHEMA_VERSION)))
            s.commit()
        elif int(meta.value) != SCHEMA_VERSION:
            # future migrations hook; v1 has nothing to migrate
            log.warning("schema version %s != code %s", meta.value, SCHEMA_VERSION)
            meta.value = str(SCHEMA_VERSION)
            s.commit()


class Database:
    def __init__(self, db_path: Path | str) -> None:
        self.engine = make_engine(db_path)
        init_db(self.engine)
        self._session_factory = sessionmaker(self.engine, expire_on_commit=False)

    def session(self) -> Session:
        return self._session_factory()

    # --- audit -----------------------------------------------------------
    def audit(self, category: str, message: str, severity: str = "info",
              data: dict | None = None) -> None:
        with self.session() as s:
            s.add(AuditLog(category=category, severity=severity, message=message, data=data))
            s.commit()

    def recent_audit(self, limit: int = 100) -> list[AuditLog]:
        with self.session() as s:
            rows = s.execute(
                select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
            ).scalars().all()
            return list(rows)

    # --- candles ---------------------------------------------------------
    def upsert_candles(self, candles: list[Candle]) -> int:
        if not candles:
            return 0
        rows = [
            dict(symbol=c.symbol, timeframe=c.timeframe.value, ts=c.ts,
                 open=c.open, high=c.high, low=c.low, close=c.close,
                 volume=c.volume, source=c.source)
            for c in candles if c.complete
        ]
        if not rows:
            return 0
        stmt = sqlite_insert(CandleRow).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=[CandleRow.symbol, CandleRow.timeframe, CandleRow.ts],
            set_={k: stmt.excluded[k] for k in ("open", "high", "low", "close", "volume", "source")},
        )
        with self.session() as s:
            s.execute(stmt)
            s.commit()
        return len(rows)

    def get_candles(self, symbol: str, timeframe: Timeframe, limit: int = 300,
                    before_ts: int | None = None) -> list[Candle]:
        with self.session() as s:
            q = select(CandleRow).where(
                CandleRow.symbol == symbol, CandleRow.timeframe == timeframe.value
            )
            if before_ts is not None:
                q = q.where(CandleRow.ts < before_ts)
            rows = s.execute(q.order_by(CandleRow.ts.desc()).limit(limit)).scalars().all()
        return [
            Candle(symbol=r.symbol, timeframe=Timeframe(r.timeframe), ts=r.ts,
                   open=r.open, high=r.high, low=r.low, close=r.close,
                   volume=r.volume, source=r.source, complete=True)
            for r in reversed(rows)
        ]

    def last_candle_ts(self, symbol: str, timeframe: Timeframe) -> int | None:
        with self.session() as s:
            row = s.execute(
                select(CandleRow.ts)
                .where(CandleRow.symbol == symbol, CandleRow.timeframe == timeframe.value)
                .order_by(CandleRow.ts.desc()).limit(1)
            ).scalar_one_or_none()
            return row
