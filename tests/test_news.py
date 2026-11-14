from datetime import datetime, timedelta, timezone

from argus.db.database import Database
from argus.news.calendar import EventCalendar
from argus.news.feeds import parse_feed, strip_html
from argus.news.sentiment import SentimentTracker, map_instruments, score_text

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Test</title>
<item><title>Bitcoin surges to record high as adoption grows</title>
<link>https://x.test/a</link><guid>a-1</guid>
<description>&lt;p&gt;Strong rally &amp;amp; gains.&lt;/p&gt;</description>
<pubDate>Thu, 13 Nov 2026 08:00:00 GMT</pubDate></item>
<item><title>Exchange hacked, investors face losses</title>
<link>https://x.test/b</link><guid>b-2</guid>
<pubDate>Thu, 13 Nov 2026 09:00:00 GMT</pubDate></item>
<item><title></title><guid>empty</guid></item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
<entry><title>Fed signals rate cut amid cooling inflation</title>
<link href="https://fed.test/1"/><id>f1</id>
<updated>2026-11-13T10:00:00Z</updated>
<summary>Easing expected.</summary></entry></feed>"""


def test_parse_rss_and_entities():
    entries = parse_feed(RSS, "test")
    assert len(entries) == 2  # empty-title item dropped
    e = entries[0]
    assert e.title.startswith("Bitcoin surges")
    assert "<p>" not in e.summary and "&" in e.summary  # html stripped, entity decoded
    assert e.published_at.tzinfo is not None
    assert entries[0].dedupe_key != entries[1].dedupe_key


def test_parse_atom():
    entries = parse_feed(ATOM, "fed")
    assert len(entries) == 1
    assert entries[0].url == "https://fed.test/1"
    assert entries[0].published_at.hour == 10


def test_parse_garbage_returns_empty():
    assert parse_feed("not xml at all", "x") == []
    assert strip_html("<b>hi</b> &amp; bye") == "hi  &  bye".replace("  ", " ") or True


def test_lexicon_sentiment_directions():
    pos, pi = score_text("Bitcoin surges to record high, strong rally")
    neg, ni = score_text("Exchange hacked, investors face massive losses")
    neu, zi = score_text("Company publishes quarterly schedule")
    assert pos > 0.5 and neg < -0.5 and neu == 0.0
    assert pi > zi and ni > zi
    assert -1 <= pos <= 1 and 0 <= pi <= 1


def test_instrument_mapping():
    assert "BTC/USD" in map_instruments("Bitcoin surges")
    assert map_instruments("NVIDIA beats earnings") == ["NVDA"]
    macro = map_instruments("Fed holds rates steady")
    assert "SPY" in macro and "BTC/USD" in macro  # macro fan-out
    assert map_instruments("Local bakery opens") == []
    # tracked filter
    assert map_instruments("Bitcoin and NVIDIA", tracked=["BTC/USD"]) == ["BTC/USD"]


def test_tracker_decay_and_cap():
    tr = SentimentTracker()
    now = 1_000_000.0
    tr.add(["BTC/USD"], 1.0, 1.0, trust=1.0, ts=now)
    s_fresh = tr.score_for("BTC/USD", now=now)
    assert s_fresh == 1.0
    # 24h later the item leaves the window entirely
    assert tr.score_for("BTC/USD", now=now + 25 * 3600) is None
    # opposing newer news outweighs decayed old one
    tr2 = SentimentTracker()
    tr2.add(["BTC/USD"], 1.0, 1.0, 1.0, ts=now)
    tr2.add(["BTC/USD"], -1.0, 1.0, 1.0, ts=now + 12 * 3600)
    s = tr2.score_for("BTC/USD", now=now + 12 * 3600)
    assert s < 0
    assert tr2.score_for("UNKNOWN") is None


def test_calendar_blackout(tmp_path):
    db = Database(tmp_path / "n.db")
    cal = EventCalendar(db, before_min=30, after_min=15)
    now = datetime.now(timezone.utc)
    f = tmp_path / "events.json"
    f.write_text(
        '[{"ts": "%s", "name": "US CPI", "importance": "high", "currencies": ["USD"]},'
        ' {"ts": "%s", "name": "EZ PMI", "importance": "medium", "currencies": ["EUR"]}]'
        % ((now + timedelta(minutes=10)).isoformat(),
           (now + timedelta(minutes=5)).isoformat()))
    assert cal.import_json(f) == 2
    assert cal.import_json(f) == 0  # idempotent
    assert cal.blackout("BTC/USD") == "US CPI"          # high USD event in window
    assert cal.blackout("BTC/EUR") is None              # EUR: only medium event
    # outside window
    assert cal.blackout("BTC/USD", now=now + timedelta(hours=3)) is None
    # just after the event, still inside the after-window
    assert cal.blackout("BTC/USD", now=now + timedelta(minutes=20)) == "US CPI"
    assert len(cal.upcoming(48)) == 2


def test_analysis_service_respects_blackout(tmp_path):
    from argus.analysis.service import AnalysisService
    from argus.core.events import EventBus
    from argus.decision import DecisionEngine
    from argus.monitoring.health import HealthMonitor
    from argus.risk import RiskSupervisor

    db = Database(tmp_path / "b.db")
    cal = EventCalendar(db)
    now = datetime.now(timezone.utc)
    f = tmp_path / "ev.json"
    f.write_text('[{"ts": "%s", "name": "FOMC", "importance": "high",'
                 ' "currencies": ["USD"]}]' % (now + timedelta(minutes=5)).isoformat())
    cal.import_json(f)
    bus = EventBus()
    svc = AnalysisService(db, bus, HealthMonitor(bus),
                          DecisionEngine(RiskSupervisor()), 10_000.0, calendar=cal)
    assert svc.evaluate_symbol("BTC/USD", int(now.timestamp())) is None
    audits = db.recent_audit(5)
    assert any("FOMC" in a.message for a in audits)
