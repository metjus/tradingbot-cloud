"""Live smoke test against Kraken's public API (no key needed).

Marked ``live``: runs only when explicitly selected. CI/offline runs skip it.
"""

import pytest

from argus.core.events import EventBus
from argus.marketdata.model import Timeframe
from argus.marketdata.providers import KrakenProvider
from argus.monitoring.health import HealthMonitor

pytestmark = pytest.mark.live


async def test_fetch_m1_live():
    emitted = []
    bus = EventBus()
    prov = KrakenProvider(["BTC/USD"], emitted.append, HealthMonitor(bus))
    try:
        bars = await prov.fetch_m1("BTC/USD", None)
    finally:
        await prov.stop()
    assert len(bars) > 100
    assert all(b.timeframe is Timeframe.M1 and b.complete for b in bars)
    # ascending, aligned, sane OHLC
    ts = [b.ts for b in bars]
    assert ts == sorted(ts) and all(t % 60 == 0 for t in ts)
    for b in bars:
        assert b.low <= b.open <= b.high
        assert b.low <= b.close <= b.high
        assert b.low > 0
