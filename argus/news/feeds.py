"""RSS/Atom feed parsing - stdlib only, defensive.

Feed content is UNTRUSTED input (§22): it is stored and scored as data,
never interpreted as instructions, and HTML is stripped before any
further processing. Feeds that fail (403, parse errors) are skipped and
reported via health - some publishers block cloud IPs but work from the
user's desktop.
"""

from __future__ import annotations

import hashlib
import html
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str) -> str:
    return html.unescape(_TAG_RE.sub(" ", text)).strip()


@dataclass(slots=True)
class FeedSource:
    name: str
    url: str
    trust: float = 1.0          # 0..1, weights sentiment aggregation


DEFAULT_FEEDS: list[FeedSource] = [
    FeedSource("ECB press", "https://www.ecb.europa.eu/rss/press.html", 1.0),
    FeedSource("Fed press", "https://www.federalreserve.gov/feeds/press_all.xml", 1.0),
    FeedSource("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", 0.7),
    FeedSource("CNBC markets", "https://www.cnbc.com/id/100003114/device/rss/rss.html", 0.8),
]


@dataclass(slots=True)
class ParsedEntry:
    source: str
    title: str
    summary: str
    url: str
    published_at: datetime
    dedupe_key: str


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _parse_date(raw: str) -> datetime | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _ns_strip(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_feed(xml_text: str, source_name: str) -> list[ParsedEntry]:
    """Parse RSS 2.0 or Atom. Malformed feeds return []."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries: list[ParsedEntry] = []
    items = [el for el in root.iter() if _ns_strip(el.tag) in ("item", "entry")]
    for it in items[:100]:
        fields: dict[str, str] = {}
        link = ""
        for child in it:
            tag = _ns_strip(child.tag)
            if tag == "link":
                link = child.get("href") or _text(child) or link
            else:
                fields.setdefault(tag, _text(child))
        title = strip_html(fields.get("title", ""))
        if not title:
            continue
        summary = strip_html(fields.get("description") or fields.get("summary") or "")[:2000]
        published = (_parse_date(fields.get("pubDate", ""))
                     or _parse_date(fields.get("published", ""))
                     or _parse_date(fields.get("updated", ""))
                     or datetime.now(timezone.utc))
        guid = fields.get("guid") or fields.get("id") or link or title
        key = hashlib.sha256(f"{source_name}|{guid}".encode()).hexdigest()[:48]
        entries.append(ParsedEntry(source=source_name, title=title, summary=summary,
                                   url=link[:512], published_at=published, dedupe_key=key))
    return entries
