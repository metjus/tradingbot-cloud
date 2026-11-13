"""Economic event calendar and trading blackout windows.

Events are stored locally (imported from a JSON file or added via API in
later phases; a Finnhub/API importer can plug in when a key exists). The
engine blocks NEW entries in a window around high-importance events -
existing positions keep their broker-side SL/TP.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from ..db.database import Database
from ..db.schema import EconomicEvent

# which currencies affect which symbols (quote/macro linkage)
SYMBOL_CURRENCIES = {
    "BTC/USD": ["USD"], "ETH/USD": ["USD"], "SPY": ["USD"], "QQQ": ["USD"],
    "NVDA": ["USD"], "AAPL": ["USD"], "TSLA": ["USD"],
    "BTC/EUR": ["EUR"], "ETH/EUR": ["EUR"],
}


class EventCalendar:
    def __init__(self, db: Database, before_min: int = 30, after_min: int = 15) -> None:
        self._db = db
        self.before = timedelta(minutes=before_min)
        self.after = timedelta(minutes=after_min)

    def import_json(self, path: Path) -> int:
        """[{"ts": "2026-11-14T13:30:00Z", "name": "US CPI", "importance":
        "high", "currencies": ["USD"]}, ...]"""
        rows = json.loads(path.read_text(encoding="utf-8"))
        n = 0
        with self._db.session() as s:
            for r in rows:
                ts = datetime.fromisoformat(str(r["ts"]).replace("Z", "+00:00"))
                exists = s.execute(select(EconomicEvent).where(
                    EconomicEvent.ts == ts, EconomicEvent.name == r["name"])).scalar_one_or_none()
                if exists:
                    continue
                s.add(EconomicEvent(ts=ts, name=str(r["name"])[:160],
                                    importance=r.get("importance", "high"),
                                    currencies=r.get("currencies") or ["USD"],
                                    source=r.get("source", "manual")))
                n += 1
            s.commit()
        if n:
            self._db.audit("data", f"Imported {n} economic events")
        return n

    def upcoming(self, hours: int = 48) -> list[EconomicEvent]:
        now = datetime.now(timezone.utc)
        with self._db.session() as s:
            rows = s.execute(select(EconomicEvent)
                             .where(EconomicEvent.ts >= now - self.after,
                                    EconomicEvent.ts <= now + timedelta(hours=hours))
                             .order_by(EconomicEvent.ts)).scalars().all()
        return list(rows)

    def blackout(self, symbol: str, now: datetime | None = None) -> str | None:
        """Returns the blocking event name when ``symbol`` is inside a
        high-importance event window, else None."""
        now = now or datetime.now(timezone.utc)
        currencies = set(SYMBOL_CURRENCIES.get(symbol, ["USD"]))
        with self._db.session() as s:
            rows = s.execute(select(EconomicEvent).where(
                EconomicEvent.importance == "high",
                EconomicEvent.ts >= now - self.after,
                EconomicEvent.ts <= now + self.before)).scalars().all()
        for ev in rows:
            ev_ccy = set(ev.currencies or ["USD"])
            if currencies & ev_ccy:
                return ev.name
        return None
