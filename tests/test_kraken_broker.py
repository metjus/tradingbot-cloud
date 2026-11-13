"""KrakenBroker adapter against a stateful mocked API (no real account)."""

import asyncio
import base64
import hashlib
import hmac
import urllib.parse

import httpx
import pytest

from argus.marketdata.model import Candle, Timeframe
from argus.trading.broker import BracketOrder
from argus.trading.kraken import KrakenBroker, sign, to_pair

SECRET = base64.b64encode(b"super-secret-key").decode()


def test_live_lock():
    with pytest.raises(RuntimeError):
        KrakenBroker("k", SECRET, live_enabled=False)


def test_pair_mapping():
    assert to_pair("BTC/USD") == "XBTUSD"
    assert to_pair("ETH/EUR") == "ETHEUR"


def test_signature_matches_reference():
    data = {"nonce": 1616492376594, "ordertype": "limit", "pair": "XBTUSD",
            "price": 37500, "type": "buy", "volume": 1.25}
    path = "/0/private/AddOrder"
    # independent reference implementation of the documented scheme
    postdata = urllib.parse.urlencode(data)
    msg = path.encode() + hashlib.sha256((str(data["nonce"]) + postdata).encode()).digest()
    expected = base64.b64encode(
        hmac.new(base64.b64decode(SECRET), msg, hashlib.sha512).digest()).decode()
    assert sign(path, data, SECRET) == expected


class FakeKraken:
    def __init__(self):
        self.n = 0
        self.orders: dict[str, dict] = {}
        self.add_calls: list[dict] = []
        self.cancelled: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        if not request.headers.get("API-Sign"):
            return httpx.Response(200, json={"error": ["EAPI:Invalid signature"]})
        path = request.url.path
        if path == "/0/private/AddOrder":
            self.add_calls.append(body)
            self.n += 1
            tx = f"TX{self.n}"
            self.orders[tx] = {"status": "pending", "price": "0", "fee": "0"}
            return httpx.Response(200, json={"error": [], "result": {"txid": [tx]}})
        if path == "/0/private/QueryOrders":
            out = {t: self.orders[t] for t in body["txid"].split(",") if t in self.orders}
            return httpx.Response(200, json={"error": [], "result": out})
        if path == "/0/private/CancelOrder":
            self.cancelled.append(body["txid"])
            return httpx.Response(200, json={"error": [], "result": {"count": 1}})
        return httpx.Response(404)

    def fill(self, tx: str, price: float, fee: float = 0.1):
        self.orders[tx] = {"status": "closed", "price": str(price),
                           "fee": str(fee), "closetm": 1_700_000_000.0}


def bar(sym, ts, h, lo, c):
    return Candle(symbol=sym, timeframe=Timeframe.M1, ts=ts, open=c, high=h,
                  low=lo, close=c, volume=1.0, source="t")


@pytest.fixture
def broker():
    fake = FakeKraken()
    b = KrakenBroker("key", SECRET, live_enabled=True,
                     transport=httpx.MockTransport(fake.handler))
    fills = []
    b.on_fill = fills.append
    return b, fake, fills


def order(coid="k1"):
    return BracketOrder(coid, "BTC/USD", "LONG", 0.01, 68000.0, 72000.0, "momentum")


async def test_entry_carries_broker_side_stop(broker):
    b, fake, fills = broker
    b.submit_bracket(order())
    await asyncio.sleep(0)
    call = fake.add_calls[0]
    assert call["pair"] == "XBTUSD" and call["type"] == "buy"
    assert call["close[ordertype]"] == "stop-loss"       # SL at the broker
    assert call["close[price]"] == "68000.0"

    fake.fill("TX1", 69000.0)
    await b.poll_once()
    assert fills[0].kind == "entry" and fills[0].price == 69000.0
    assert len(b.positions()) == 1
    await b.stop()


async def test_local_tp_trigger_closes_at_market(broker):
    b, fake, fills = broker
    b.submit_bracket(order())
    await asyncio.sleep(0)
    fake.fill("TX1", 69000.0)
    await b.poll_once()

    b.on_candle(bar("BTC/USD", 60, 72100.0, 71000.0, 72050.0))  # TP touched
    await asyncio.sleep(0)
    close_call = fake.add_calls[-1]
    assert close_call["type"] == "sell" and close_call["ordertype"] == "market"

    fake.fill("TX2", 72010.0)
    await b.poll_once()
    assert fills[-1].kind == "take_profit" and fills[-1].price == 72010.0
    assert b.positions() == []
    await b.stop()


async def test_manual_close(broker):
    b, fake, fills = broker
    b.submit_bracket(order())
    await asyncio.sleep(0)
    fake.fill("TX1", 69000.0)
    await b.poll_once()
    assert b.close_position("k1", "manual_close")
    await asyncio.sleep(0)
    fake.fill("TX2", 68900.0)
    await b.poll_once()
    assert fills[-1].kind == "manual_close"
    await b.stop()
