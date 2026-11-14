"""News service: polls feeds, dedupes, scores, feeds the sentiment tracker.

LLM refinement is optional and budget-capped (llm.py); the lexicon
baseline always runs, so a missing/failing LLM never degrades the engine
below baseline (§21). All feed text is data, never instructions.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy import select

from ..core.events import EventBus
from ..db.database import Database
from ..db.schema import NewsItem
from ..monitoring.health import HealthMonitor, Status
from .feeds import DEFAULT_FEEDS, FeedSource, parse_feed
from .sentiment import SentimentTracker, map_instruments, score_text

log = logging.getLogger(__name__)

NEWS_EVENT = "news"


class NewsService:
    def __init__(self, db: Database, bus: EventBus, health: HealthMonitor,
                 tracked_symbols: list[str], feeds: list[FeedSource] | None = None,
                 poll_seconds: float = 300.0, llm_analyzer=None) -> None:
        self._db = db
        self._bus = bus
        self._health = health
        self._tracked = tracked_symbols
        self._feeds = feeds if feeds is not None else DEFAULT_FEEDS
        self._poll = poll_seconds
        self._llm = llm_analyzer
        self.tracker = SentimentTracker()
        self._task: asyncio.Task | None = None
        self._client = httpx.AsyncClient(timeout=20.0, follow_redirects=True,
                                         headers={"User-Agent": "argus-engine/0.5 rss-reader"})
        health.register("news", stale_after=max(900.0, poll_seconds * 3))
        self._warm_start()

    def _warm_start(self) -> None:
        """Re-seed the tracker from recent stored items after restart."""
        with self._db.session() as s:
            rows = s.execute(select(NewsItem).order_by(NewsItem.id.desc()).limit(200)
                             ).scalars().all()
        for r in rows:
            if r.sentiment is not None and r.instruments:
                self.tracker.add(r.instruments, r.sentiment, r.intensity or 0.3,
                                 trust=1.0, ts=r.published_at.replace(
                                     tzinfo=timezone.utc).timestamp()
                                 if r.published_at.tzinfo is None
                                 else r.published_at.timestamp())

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._run(), name="news")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self._client.aclose()

    async def _run(self) -> None:
        while True:
            ok, failed = 0, []
            for feed in self._feeds:
                try:
                    n = await self._poll_feed(feed)
                    ok += 1
                    if n:
                        log.info("news: %s -> %d new items", feed.name, n)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failed.append(f"{feed.name}: {type(exc).__name__}")
                    log.debug("feed %s failed: %s", feed.name, exc)
            status = Status.OK if ok else Status.DEGRADED
            detail = f"{ok}/{len(self._feeds)} feeds ok"
            if failed:
                detail += f" (failed: {', '.join(failed[:3])})"
            self._health.heartbeat("news", status, detail)
            await asyncio.sleep(self._poll)

    async def _poll_feed(self, feed: FeedSource) -> int:
        r = await self._client.get(feed.url)
        r.raise_for_status()
        entries = parse_feed(r.text, feed.name)
        new = 0
        for e in entries:
            with self._db.session() as s:
                dup = s.execute(select(NewsItem.id).where(
                    NewsItem.dedupe_key == e.dedupe_key)).scalar_one_or_none()
                if dup is not None:
                    continue
                sentiment, intensity = score_text(e.title, e.summary)
                instruments = map_instruments(e.title, e.summary, self._tracked)
                analysis = {"method": "lexicon"}
                if self._llm is not None and instruments:
                    refined = await self._llm.analyze(e.title, e.summary, instruments)
                    if refined is not None:
                        sentiment = refined["sentiment"]
                        intensity = refined["intensity"]
                        analysis = {"method": "llm", **refined}
                item = NewsItem(dedupe_key=e.dedupe_key, source=e.source,
                                title=e.title[:500], summary=e.summary or None,
                                url=e.url or None, published_at=e.published_at,
                                instruments=instruments or None,
                                sentiment=sentiment, intensity=intensity,
                                analysis=analysis)
                s.add(item)
                s.commit()
            new += 1
            if instruments:
                self.tracker.add(instruments, sentiment, intensity, feed.trust,
                                 ts=e.published_at.timestamp())
            self._bus.publish(NEWS_EVENT, {
                "source": e.source, "title": e.title, "url": e.url,
                "published_at": e.published_at.isoformat(),
                "instruments": instruments, "sentiment": sentiment,
                "intensity": intensity})
        return new

    def recent(self, limit: int = 50) -> list[dict]:
        with self._db.session() as s:
            rows = s.execute(select(NewsItem).order_by(NewsItem.id.desc())
                             .limit(limit)).scalars().all()
        return [{"ts": r.published_at.isoformat(), "source": r.source,
                 "title": r.title, "url": r.url, "instruments": r.instruments,
                 "sentiment": r.sentiment, "intensity": r.intensity} for r in rows]
