"""AlpacaBroker adapter against a mocked HTTP transport (no real account)."""

import asyncio
import json

import httpx
import pytest

from argus.marketdata.model import Timeframe
from argus.trading.alpaca import AlpacaBroker
from argus.trading.broker import BracketOrder


class FakeAlpaca:
    """Minimal stateful mock of the Alpaca orders API."""

    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.posted: list[dict] = []
        self.deleted: list[str] = []
        self.n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/v2/orders":
            body = json.loads(request.content)
            self.posted.append(body)
            self.n += 1
            oid = f"aid-{self.n}"
            self.orders[oid] = {"id": oid, "status": "new", "legs": [],
                                "client_order_id": body.get("client_order_id"),
                                "type": body.get("type"), "filled_avg_price": None}
            return httpx.Response(200, json=self.orders[oid])
        if request.method == "GET" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[1]
            o = self.orders.get(oid)
            return httpx.Response(200, json=o) if o else httpx.Response(404, json={})
        if request.method == "DELETE" and path.startswith("/v2/orders/"):
            self.deleted.append(path.rsplit("/", 1)[1])
            return httpx.Response(204)
        return httpx.Response(404, json={"error": path})

    # test helpers to advance server-side state
    def fill_entry(self, oid: str, price: float):
        self.orders[oid].update(status="filled", filled_avg_price=str(price),
                                filled_at="2026-11-13T10:00:00Z")

    def fill_leg(self, oid: str, leg_type: str, price: float):
        self.orders[oid]["legs"] = [{"status": "filled", "type": leg_type,
                                     "filled_avg_price": str(price),
                                     "filled_at": "2026-11-13T11:00:00Z"}]


@pytest.fixture
def broker():
    fake = FakeAlpaca()
    b = AlpacaBroker("k", "s", paper=True,
                     transport=httpx.MockTransport(fake.handler))
    fills = []
    b.on_fill = fills.append
    return b, fake, fills


def order(coid="c1", direction="LONG"):
    return BracketOrder(coid, "NVDA", direction, 10.0, 180.0, 195.0, "momentum")


async def test_submit_entry_and_tp_fill_cycle(broker):
    b, fake, fills = broker
    b.submit_bracket(order())
    await asyncio.sleep(0)   # let the submit task run
    assert fake.posted[0]["order_class"] == "bracket"
    assert fake.posted[0]["client_order_id"] == "c1"
    assert fake.posted[0]["take_profit"]["limit_price"] == "195.0"

    await b.poll_once()      # not filled yet
    assert fills == []

    fake.fill_entry("aid-1", 188.5)
    await b.poll_once()
    assert fills[0].kind == "entry" and fills[0].price == 188.5
    assert len(b.positions()) == 1 and b.positions()[0].symbol == "NVDA"

    fake.fill_leg("aid-1", "limit", 195.0)
    await b.poll_once()
    assert fills[1].kind == "take_profit" and fills[1].price == 195.0
    assert b.positions() == []
    await b.stop()


async def test_stop_loss_leg(broker):
    b, fake, fills = broker
    b.submit_bracket(order())
    await asyncio.sleep(0)
    fake.fill_entry("aid-1", 188.0)
    await b.poll_once()
    fake.fill_leg("aid-1", "stop", 180.0)
    await b.poll_once()
    assert fills[-1].kind == "stop_loss" and fills[-1].price == 180.0
    await b.stop()


async def test_manual_close_cancels_bracket_then_liquidates(broker):
    b, fake, fills = broker
    b.submit_bracket(order())
    await asyncio.sleep(0)
    fake.fill_entry("aid-1", 188.0)
    await b.poll_once()

    assert b.close_position("c1", "manual_close")
    await asyncio.sleep(0)
    assert fake.deleted == ["aid-1"]            # bracket legs cancelled first
    close = fake.posted[-1]
    assert close["side"] == "sell" and close["client_order_id"] == "c1-close"

    fake.fill_entry("aid-2", 187.2)             # close order filled
    await b.poll_once()
    assert fills[-1].kind == "manual_close" and fills[-1].price == 187.2
    assert b.positions() == []
    await b.stop()


async def test_duplicate_submit_ignored_and_rejected_cleanup(broker):
    b, fake, fills = broker
    b.submit_bracket(order())
    b.submit_bracket(order())                   # duplicate coid
    await asyncio.sleep(0)
    assert len(fake.posted) == 1

    fake.orders["aid-1"]["status"] = "rejected"
    await b.poll_once()
    assert b.positions() == [] and "c1" not in b._orders
    await b.stop()
