"""Kraken spot broker adapter (prepared for phase 14 - LIVE only).

Kraken has no paper environment and spot API has no OCO brackets, so the
protection model differs from Alpaca and is stated honestly:

  * STOP-LOSS lives AT THE BROKER: the entry order carries a conditional
    close (close[ordertype]=stop-loss), so the position stays protected
    even if the engine/PC dies
  * TAKE-PROFIT is managed locally: when a candle touches TP the engine
    cancels the protective stop and closes at market

The adapter never assumes an order filled: state advances only from
QueryOrders results. Construction requires ``live_enabled=True`` which the
engine will only pass after the user explicitly unlocks live trading.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import time
import urllib.parse

import httpx

from ..marketdata.model import Candle
from .broker import BrokerAdapter, BracketOrder, BrokerPosition, Fill

log = logging.getLogger(__name__)

BASE = "https://api.kraken.com"
_PAIR_MAP = {"BTC": "XBT"}


def to_pair(symbol: str) -> str:
    base, quote = symbol.split("/")
    return f"{_PAIR_MAP.get(base, base)}{quote}"


def sign(path: str, data: dict, secret: str) -> str:
    """Kraken API-Sign: HMAC-SHA512(path + SHA256(nonce + postdata), b64secret)."""
    postdata = urllib.parse.urlencode(data)
    encoded = (str(data["nonce"]) + postdata).encode()
    message = path.encode() + hashlib.sha256(encoded).digest()
    mac = hmac.new(base64.b64decode(secret), message, hashlib.sha512)
    return base64.b64encode(mac.digest()).decode()


class KrakenBroker(BrokerAdapter):
    name = "kraken"
    server_side_brackets = False   # SL yes (conditional close), TP is local

    def __init__(self, api_key: str, api_secret: str, live_enabled: bool = False,
                 poll_seconds: float = 3.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        if not live_enabled:
            raise RuntimeError(
                "KrakenBroker is live-only; live trading is locked until "
                "phase 14 is explicitly enabled by the user")
        super().__init__()
        self._key = api_key
        self._secret = api_secret
        self._poll = poll_seconds
        self._client = httpx.AsyncClient(base_url=BASE, timeout=15.0,
                                         transport=transport)
        self._orders: dict[str, dict] = {}
        self._task: asyncio.Task | None = None

    # --- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(
            self._poll_loop(), name="kraken-fills")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self._client.aclose()

    # --- private API call -------------------------------------------------
    async def _private(self, method: str, data: dict | None = None) -> dict:
        path = f"/0/private/{method}"
        data = dict(data or {})
        data["nonce"] = int(time.time() * 1000)
        r = await self._client.post(path, data=data, headers={
            "API-Key": self._key, "API-Sign": sign(path, data, self._secret)})
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError(f"kraken: {payload['error']}")
        return payload.get("result", {})

    # --- BrokerAdapter ----------------------------------------------------
    def submit_bracket(self, order: BracketOrder) -> None:
        if order.client_order_id in self._orders:
            log.warning("duplicate client_order_id %s ignored", order.client_order_id)
            return
        self._orders[order.client_order_id] = {"order": order, "state": "submitting",
                                               "entry_reported": False}
        asyncio.get_running_loop().create_task(self._submit(order))

    async def _submit(self, order: BracketOrder) -> None:
        try:
            result = await self._private("AddOrder", {
                "pair": to_pair(order.symbol),
                "type": "buy" if order.direction == "LONG" else "sell",
                "ordertype": "market",
                "volume": f"{order.quantity:.8f}",
                "userref": abs(hash(order.client_order_id)) % 2**31,
                # protective stop lives at the broker:
                "close[ordertype]": "stop-loss",
                "close[price]": f"{order.stop_loss:.1f}",
            })
            txids = result.get("txid") or []
            if not txids:
                raise RuntimeError("no txid returned")
            rec = self._orders[order.client_order_id]
            rec["txid"] = txids[0]
            rec["state"] = "submitted"
        except Exception as exc:
            log.error("kraken submit failed %s: %s", order.client_order_id, exc)
            self._orders.pop(order.client_order_id, None)

    def close_position(self, client_order_id: str, reason: str = "manual_close") -> bool:
        rec = self._orders.get(client_order_id)
        if rec is None or rec["state"] not in ("open", "submitted"):
            return False
        rec["close_reason"] = reason
        asyncio.get_running_loop().create_task(self._close(client_order_id))
        return True

    async def _close(self, coid: str) -> None:
        rec = self._orders.get(coid)
        if rec is None:
            return
        o: BracketOrder = rec["order"]
        try:
            # cancel the conditional-close child if it exists as open order
            if rec.get("stop_txid"):
                try:
                    await self._private("CancelOrder", {"txid": rec["stop_txid"]})
                except Exception:
                    pass
            result = await self._private("AddOrder", {
                "pair": to_pair(o.symbol),
                "type": "sell" if o.direction == "LONG" else "buy",
                "ordertype": "market",
                "volume": f"{o.quantity:.8f}",
            })
            rec["close_txid"] = (result.get("txid") or [None])[0]
            rec["state"] = "closing"
        except Exception as exc:
            log.error("kraken close failed %s: %s", coid, exc)

    def positions(self) -> list[BrokerPosition]:
        out = []
        for coid, rec in self._orders.items():
            if rec.get("state") == "open":
                o: BracketOrder = rec["order"]
                out.append(BrokerPosition(coid, o.symbol, o.direction, o.quantity,
                                          rec.get("entry_price", 0.0), o.stop_loss,
                                          o.take_profit, rec.get("entry_ts", 0.0),
                                          o.strategy))
        return out

    # --- TP local trigger -------------------------------------------------
    def on_candle(self, bar: Candle) -> None:
        for coid, rec in list(self._orders.items()):
            if rec.get("state") != "open":
                continue
            o: BracketOrder = rec["order"]
            if o.symbol != bar.symbol:
                continue
            tp_hit = bar.high >= o.take_profit if o.direction == "LONG" \
                else bar.low <= o.take_profit
            if tp_hit and not rec.get("tp_closing"):
                rec["tp_closing"] = True
                rec["close_reason"] = "take_profit"
                asyncio.get_running_loop().create_task(self._close(coid))

    # --- fill polling -----------------------------------------------------
    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("kraken poll error: %s", exc)
            await asyncio.sleep(self._poll)

    async def poll_once(self) -> None:
        txids = [rec.get("txid") for rec in self._orders.values()
                 if rec.get("txid") and rec["state"] == "submitted"]
        txids += [rec.get("close_txid") for rec in self._orders.values()
                  if rec.get("close_txid") and rec["state"] == "closing"]
        if not txids:
            return
        result = await self._private("QueryOrders", {"txid": ",".join(txids)})
        for coid, rec in list(self._orders.items()):
            o: BracketOrder = rec["order"]
            if rec["state"] == "submitted" and rec.get("txid") in result:
                info = result[rec["txid"]]
                if info.get("status") == "closed" and not rec["entry_reported"]:
                    px = float(info.get("price") or 0.0)
                    fee = float(info.get("fee") or 0.0)
                    rec.update(state="open", entry_reported=True, entry_price=px,
                               entry_ts=float(info.get("closetm") or time.time()))
                    if self.on_fill:
                        self.on_fill(Fill(coid, o.symbol, o.direction, o.quantity,
                                          px, fee, rec["entry_ts"], "entry"))
                elif info.get("status") in ("canceled", "expired"):
                    self._orders.pop(coid, None)
            elif rec["state"] == "closing" and rec.get("close_txid") in result:
                info = result[rec["close_txid"]]
                if info.get("status") == "closed":
                    px = float(info.get("price") or 0.0)
                    fee = float(info.get("fee") or 0.0)
                    self._orders.pop(coid, None)
                    if self.on_fill:
                        self.on_fill(Fill(coid, o.symbol,
                                          "SELL" if o.direction == "LONG" else "BUY",
                                          o.quantity, px, fee, time.time(),
                                          rec.get("close_reason", "manual_close")))
