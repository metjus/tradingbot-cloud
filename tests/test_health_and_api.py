import asyncio

import pytest
from fastapi.testclient import TestClient

from argus.api.server import create_app
from argus.core.events import EventBus
from argus.db.database import Database
from argus.marketdata.model import Candle, Timeframe
from argus.monitoring.health import HealthMonitor, Status


@pytest.fixture
def bus():
    return EventBus()


async def test_health_stale_detection(bus):
    mon = HealthMonitor(bus, check_interval=0.02)
    mon.register("feed", stale_after=0.05)
    mon.heartbeat("feed", Status.OK)
    assert mon.overall is Status.OK
    task = asyncio.get_running_loop().create_task(mon.run())
    await asyncio.sleep(0.15)
    task.cancel()
    assert mon.overall is Status.STALE


async def test_health_events_published(bus):
    q = bus.subscribe("health")
    mon = HealthMonitor(bus)
    mon.register("x")
    mon.heartbeat("x", Status.OK)
    ev = q.get_nowait()
    assert ev.data["name"] == "x" and ev.data["status"] == "ok"


def test_api_endpoints(tmp_path, bus):
    db = Database(tmp_path / "api.db")
    db.upsert_candles([Candle(symbol="BTC/USD", timeframe=Timeframe.M5, ts=300,
                              open=1, high=2, low=1, close=2, volume=3, source="t")])
    db.audit("system", "boot")
    mon = HealthMonitor(bus)
    mon.register("engine")
    mon.heartbeat("engine", Status.OK)
    app = create_app(db, bus, mon, {"mode": "paper", "state": "running",
                                    "symbols": ["BTC/USD"], "provider": "kraken"})
    client = TestClient(app)

    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["overall"] == "ok"

    r = client.get("/api/status")
    assert r.json()["mode"] == "paper"

    r = client.get("/api/candles/BTC/USD/5m")
    body = r.json()
    assert body["timeframe"] == "5m"
    assert len(body["candles"]) == 1
    assert body["candles"][0]["close"] == 2

    r = client.get("/api/candles/BTC/USD/7m")
    assert "error" in r.json()

    r = client.get("/api/audit")
    assert r.json()["entries"][0]["message"] == "boot"


def test_event_bus_drops_oldest_when_full():
    bus = EventBus(queue_size=2)
    q = bus.subscribe("t")
    bus.publish("t", 1)
    bus.publish("t", 2)
    bus.publish("t", 3)  # drops 1
    vals = [q.get_nowait().data for _ in range(2)]
    assert vals == [2, 3]
