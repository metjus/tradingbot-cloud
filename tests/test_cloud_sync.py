"""CloudSync integration test against an in-process mock Worker."""

import asyncio
import threading

import pytest
import uvicorn
from fastapi import FastAPI, Header, HTTPException

from argus.cloud.sync import CloudSync
from argus.core.events import EventBus
from argus.db.database import Database
from argus.monitoring.health import HealthMonitor

TOKEN = "test-token"
PORT = 7599


class MockWorker:
    def __init__(self):
        self.states = []
        self.notifications = []
        self.acks = []
        self.commands = [{"id": "cmd-1", "action": "pause", "ts": 10**12}]
        self.app = FastAPI()
        self._wire()

    def _auth(self, authorization: str | None):
        if authorization != f"Bearer {TOKEN}":
            raise HTTPException(401)

    def _wire(self):
        app = self.app

        @app.post("/ingest/state")
        def ingest_state(body: dict, authorization: str | None = Header(None)):
            self._auth(authorization)
            self.states.append(body)
            return {"ok": True}

        @app.post("/ingest/notification")
        def ingest_notification(body: dict, authorization: str | None = Header(None)):
            self._auth(authorization)
            self.notifications.append(body)
            return {"ok": True}

        @app.get("/commands/pending")
        def pending(authorization: str | None = Header(None)):
            self._auth(authorization)
            out, self.commands = self.commands, []
            return {"commands": out}

        @app.post("/commands/ack")
        def ack(body: dict, authorization: str | None = Header(None)):
            self._auth(authorization)
            self.acks.append(body)
            return {"ok": True}


class FakeExecutor:
    state = "running"

    def __init__(self):
        self.commands = []

    def command(self, action, command_id=None):
        self.commands.append((action, command_id))
        self.state = "paused" if action == "pause" else self.state
        return {"ok": True, "state": self.state}


class FakePortfolio:
    def snapshot(self):
        return {"equity": 10000.0, "open_positions": []}

    def trade_history(self, n):
        return []


@pytest.fixture
def mock_worker():
    mw = MockWorker()
    config = uvicorn.Config(mw.app, host="127.0.0.1", port=PORT, log_level="critical")
    server = uvicorn.Server(config)
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    import time
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.05)
    yield mw
    server.should_exit = True
    t.join(timeout=5)


async def test_sync_pushes_polls_and_acks(tmp_path, mock_worker):
    db = Database(tmp_path / "s.db")
    bus = EventBus()
    execu = FakeExecutor()
    sync = CloudSync(db, bus, HealthMonitor(bus), execu, FakePortfolio(), None,
                     f"http://127.0.0.1:{PORT}", TOKEN,
                     push_seconds=0.2, poll_seconds=0.2)
    sync.start()
    # publish a trade event -> notification + immediate state push
    bus.publish("trade", {"kind": "closed", "symbol": "BTC/USD", "pl": 5.0,
                          "reason": "take_profit", "equity": 10005.0})
    await asyncio.sleep(1.2)
    await sync.stop()

    assert mock_worker.states, "state snapshots must be pushed"
    snap = mock_worker.states[-1]
    assert snap["bot_state"] in ("running", "paused")
    assert snap["portfolio"]["equity"] == 10000.0

    assert mock_worker.notifications
    n = mock_worker.notifications[0]
    assert n["category"] == "trade_closed" and "BTC/USD" in n["title"]

    # the queued pause command was executed and acked
    assert ("pause", "cloud-cmd-1") in execu.commands
    assert mock_worker.acks and mock_worker.acks[0]["id"] == "cmd-1"
    assert mock_worker.acks[0]["result"]["ok"] is True


async def test_sync_survives_unreachable_cloud(tmp_path):
    db = Database(tmp_path / "s2.db")
    bus = EventBus()
    sync = CloudSync(db, bus, HealthMonitor(bus), FakeExecutor(), FakePortfolio(),
                     None, "http://127.0.0.1:9", TOKEN, push_seconds=0.1,
                     poll_seconds=0.1)
    sync.start()
    await asyncio.sleep(0.5)
    await sync.stop()  # no exception = engine keeps running without cloud
