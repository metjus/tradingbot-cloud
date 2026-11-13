"""SQLAlchemy schema.

Designed for the adaptive-learning requirements: every closed trade stores
full entry/exit context (§9A of the spec) so models can be trained later
without re-deriving state. Phase 3 creates the schema and uses candles,
audit_log and app_settings; trades/decisions/model_registry fill up in
later phases.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class SchemaMeta(Base):
    __tablename__ = "schema_meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(256))


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    category: Mapped[str] = mapped_column(String(48), index=True)   # system|data|trade|risk|ml|sync|security
    severity: Mapped[str] = mapped_column(String(16), default="info")  # info|warning|error|critical
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class CandleRow(Base):
    __tablename__ = "candles"
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    timeframe: Mapped[str] = mapped_column(String(8), primary_key=True)
    ts: Mapped[int] = mapped_column(Integer, primary_key=True)  # open time, epoch s
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(24), default="")


Index("ix_candles_lookup", CandleRow.symbol, CandleRow.timeframe, CandleRow.ts.desc())


class Trade(Base):
    """One executed trade with full learning context (filled from phase 4+)."""

    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    broker: Mapped[str] = mapped_column(String(24))
    mode: Mapped[str] = mapped_column(String(8))  # paper|live
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    direction: Mapped[str] = mapped_column(String(5))  # LONG|SHORT
    strategy: Mapped[str] = mapped_column(String(48), index=True)
    regime: Mapped[str | None] = mapped_column(String(32), nullable=True)

    entry_ts: Mapped[datetime | None] = mapped_column(nullable=True)
    exit_ts: Mapped[datetime | None] = mapped_column(nullable=True)
    entry_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    stop_loss: Mapped[float | None] = mapped_column(Float, nullable=True)
    take_profit: Mapped[float | None] = mapped_column(Float, nullable=True)
    pl: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    slippage: Mapped[float | None] = mapped_column(Float, nullable=True)
    risk_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    rr_planned: Mapped[float | None] = mapped_column(Float, nullable=True)

    entry_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|closed|cancelled|error

    # full snapshots for ML: indicators at entry/exit, sentiment, news refs,
    # volatility, spread, decision score breakdown ...
    entry_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    exit_context: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Decision(Base):
    """Every evaluated opportunity, including rejected ones (explainability)."""

    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    strategy: Mapped[str] = mapped_column(String(48))
    score: Mapped[float] = mapped_column(Float)
    verdict: Mapped[str] = mapped_column(String(16))  # enter|reject|veto
    vetoed_by: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reasons: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    trade_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class ModelVersion(Base):
    __tablename__ = "model_registry"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(48), index=True)
    version: Mapped[int] = mapped_column(Integer)
    task: Mapped[str] = mapped_column(String(64))
    path: Mapped[str] = mapped_column(String(256))
    trained_at: Mapped[datetime] = mapped_column(default=utcnow)
    n_samples: Mapped[int] = mapped_column(Integer, default=0)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="candidate", index=True)
    # candidate -> paper_test -> active -> archived
    active: Mapped[bool] = mapped_column(Boolean, default=False)
