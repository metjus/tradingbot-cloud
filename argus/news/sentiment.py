"""Instrument mapping and baseline sentiment.

Local lexicon scoring keeps the engine independent of any LLM API (§21):
it is deliberately simple and conservative (most headlines score near 0).
An optional LLM analyser (llm.py) can refine items when enabled; its
output is clamped into the same -1..1 / 0..1 ranges and NEVER anything
but numbers + short rationale - news text cannot change system behaviour.

Per-instrument aggregation: trust- and intensity-weighted mean with
exponential time decay (half-life 6h), clamped to -1..1. One headline can
never saturate the signal (per-item cap), satisfying "AI nesmie otvoriť
obchod len na základe jednej správy".
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

POSITIVE = {
    "beat", "beats", "growth", "gain", "gains", "rally", "record", "strong",
    "surge", "surges", "upgrade", "upgraded", "bullish", "profit", "profits",
    "expand", "expansion", "approval", "approved", "adoption", "partnership",
    "cuts rates", "rate cut", "stimulus", "easing", "soft landing", "cooling inflation",
}
NEGATIVE = {
    "miss", "misses", "loss", "losses", "fall", "falls", "plunge", "plunges",
    "crash", "selloff", "sell-off", "downgrade", "downgraded", "bearish",
    "lawsuit", "fraud", "hack", "hacked", "exploit", "bankruptcy", "default",
    "recession", "war", "sanction", "sanctions", "hike", "hikes", "inflation rises",
    "rate hike", "tightening", "layoffs", "probe", "investigation", "ban", "bans",
}
INTENSIFIERS = {"record", "massive", "sharp", "sharply", "historic", "unprecedented",
                "emergency", "crisis", "surge", "plunge", "crash"}

# keyword -> instruments it affects (extendable via config later)
INSTRUMENT_KEYWORDS: dict[str, list[str]] = {
    "bitcoin": ["BTC/USD"], "btc": ["BTC/USD"],
    "ethereum": ["ETH/USD"], "eth ": ["ETH/USD"], "ether ": ["ETH/USD"],
    "crypto": ["BTC/USD", "ETH/USD"], "cryptocurrency": ["BTC/USD", "ETH/USD"],
    "stablecoin": ["BTC/USD", "ETH/USD"],
    "nvidia": ["NVDA"], "nvda": ["NVDA"],
    "apple": ["AAPL"], "aapl": ["AAPL"],
    "tesla": ["TSLA"], "tsla": ["TSLA"],
    "s&p": ["SPY"], "s&p 500": ["SPY"], "wall street": ["SPY"], "nasdaq": ["QQQ"],
}
MACRO_KEYWORDS = ["fed", "federal reserve", "fomc", "ecb", "rate", "rates",
                  "inflation", "cpi", "jobs report", "nonfarm", "nfp", "gdp",
                  "treasury", "tariff", "tariffs"]
MACRO_INSTRUMENTS = ["BTC/USD", "ETH/USD", "SPY", "QQQ"]


def _count_hits(text: str, vocab: set[str]) -> int:
    return sum(1 for w in vocab if w in text)


def score_text(title: str, summary: str = "") -> tuple[float, float]:
    """Returns (sentiment -1..1, intensity 0..1). Conservative lexicon."""
    text = f"{title} {summary}".lower()
    pos = _count_hits(text, POSITIVE)
    neg = _count_hits(text, NEGATIVE)
    total = pos + neg
    if total == 0:
        return 0.0, 0.1
    sentiment = (pos - neg) / total
    intensity = min(1.0, 0.3 + 0.2 * total + (0.2 if _count_hits(text, INTENSIFIERS) else 0.0))
    return max(-1.0, min(1.0, sentiment)), intensity


def map_instruments(title: str, summary: str = "",
                    tracked: list[str] | None = None) -> list[str]:
    text = f" {title} {summary} ".lower()
    out: list[str] = []
    for kw, instruments in INSTRUMENT_KEYWORDS.items():
        if kw in text:
            for ins in instruments:
                if ins not in out:
                    out.append(ins)
    if not out and any(re.search(rf"\b{re.escape(kw)}\b", text) for kw in MACRO_KEYWORDS):
        out = list(MACRO_INSTRUMENTS)
    if tracked is not None:
        out = [s for s in out if s in tracked]
    return out


@dataclass(slots=True)
class _Item:
    ts: float
    sentiment: float
    weight: float      # trust * intensity, capped


@dataclass
class SentimentTracker:
    """Aggregates scored items per instrument with exponential decay."""

    half_life_s: float = 6 * 3600.0
    max_item_weight: float = 0.35      # one item can never dominate
    window_s: float = 24 * 3600.0
    _items: dict[str, list[_Item]] = field(default_factory=dict)

    def add(self, instruments: list[str], sentiment: float, intensity: float,
            trust: float, ts: float | None = None) -> None:
        ts = time.time() if ts is None else ts
        w = min(self.max_item_weight, max(0.0, trust) * max(0.0, intensity))
        if w <= 0:
            return
        for ins in instruments:
            self._items.setdefault(ins, []).append(_Item(ts, sentiment, w))

    def score_for(self, instrument: str, now: float | None = None) -> float | None:
        now = time.time() if now is None else now
        items = self._items.get(instrument)
        if not items:
            return None
        items[:] = [i for i in items if now - i.ts <= self.window_s]
        num = den = 0.0
        for i in items:
            decay = math.pow(0.5, (now - i.ts) / self.half_life_s)
            num += i.sentiment * i.weight * decay
            den += i.weight * decay
        if den < 0.05:
            return None
        return max(-1.0, min(1.0, num / den))

    def snapshot(self, now: float | None = None) -> dict[str, float]:
        return {k: s for k in list(self._items)
                if (s := self.score_for(k, now)) is not None}
