"""Alpaca broker adapter (phase 12) - paper and live share one API.

Bracket orders are held SERVER-SIDE by Alpaca (SL/TP survive engine
outages). Fills are detected by polling order status; nothing is assumed
filled until Alpaca reports it. The engine refuses to construct this
adapter in live mode unless live trading is explicitly enabled (phase 14).

Notes:
  * crypto availability depends on jurisdiction/account; stocks are the
    primary target of this adapter
  * client_order_id is passed through, so retries can never duplicate
    an order on Alpaca's side
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from .broker import BrokerAdapter, BracketOrder, BrokerPosition, Fill

log = logging.getLogger(__name__)

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


class AlpacaBroker(BrokerAdapter):
    name = "alpaca"
    server_side_brackets = True

    def __init__(self, key_id: str, secret_key: str, paper: bool = True,
                 poll_seconds: float = 2.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self.paper = paper
        self._poll = poll_seconds
        self._client = httpx.AsyncClient(
            base_url=PAPER_URL if paper else LIVE_URL, timeout=15.0,
            headers={"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key},
            transport=transport)
        # client_order_id -> order bookkeeping
        self._orders: dict[str, dict] = {}
        self._task: asyncio.Task | None = None

    # --- lifecycle -------------------------------------------------------
    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(
            self._poll_loop(), name="alpaca-fills")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await self._client.aclose()

    # --- BrokerAdapter (sync wrappers schedule async work) ----------------
    def submit_bracket(self, order: BracketOrder) -> None:
        if order.client_order_id in self._orders:
            log.warning("duplicate client_order_id %s ignored", order.client_order_id)
            return
        self._orders[order.client_order_id] = {"order": order, "state": "submitting",
                                               "entry_reported": False}
        asyncio.get_running_loop().create_task(self._submit(order))

    def close_position(self, client_order_id: str, reason: str = "manual_close") -> bool:
        rec = self._orders.get(client_order_id)
        if rec is None:
            return False
        rec["close_reason"] = reason
        asyncio.get_running_loop().create_task(self._close(client_order_id))
        return True

    def update_stop(self, client_order_id: str, new_stop: float) -> bool:
        rec = self._orders.get(client_order_id)
        if rec is None or rec.get("state") != "open" or not rec.get("stop_leg_id"):
            return False
        o: BracketOrder = rec["order"]
        if o.direction == "LONG" and new_stop <= o.stop_loss:
            return False
        if o.direction == "SHORT" and new_stop >= o.stop_loss:
            return False
        o.stop_loss = new_stop
        asyncio.get_running_loop().create_task(self._patch_stop(rec, new_stop))
        return True

    async def _patch_stop(self, rec: dict, new_stop: float) -> None:
        try:
            r = await self._client.patch(f"/v2/orders/{rec['stop_leg_id']}",
                                         json={"stop_price": str(round(new_stop, 2))})
            r.raise_for_status()
            # a replace returns a NEW order id for the stop leg
            rec["stop_leg_id"] = r.json().get("id", rec["stop_leg_id"])
        except Exception as exc:
            log.error("alpaca stop replace failed: %s", exc)

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

    # --- REST calls ------------------------------------------------------
    async def _submit(self, order: BracketOrder) -> None:
        body = {
            "symbol": order.symbol,
            "qty": str(order.quantity),
            "side": "buy" if order.direction == "LONG" else "sell",
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "client_order_id": order.client_order_id,
            "take_profit": {"limit_price": str(round(order.take_profit, 2))},
            "stop_loss": {"stop_price": str(round(order.stop_loss, 2))},
        }
        try:
            r = await self._client.post("/v2/orders", json=body)
            r.raise_for_status()
            data = r.json()
            rec = self._orders[order.client_order_id]
            rec["state"] = "submitted"
            rec["alpaca_id"] = data["id"]
            log.info("alpaca order submitted %s (%s)", order.client_order_id, data["id"])
        except Exception as exc:
            log.error("alpaca submit failed %s: %s", order.client_order_id, exc)
            self._orders.pop(order.client_order_id, None)

    async def _close(self, coid: str) -> None:
        rec = self._orders.get(coid)
        if rec is None:
            return
        try:
            # cancel remaining bracket legs, then liquidate
            if rec.get("alpaca_id"):
                await self._client.delete(f"/v2/orders/{rec['alpaca_id']}")
            o: BracketOrder = rec["order"]
            side = "sell" if o.direction == "LONG" else "buy"
            r = await self._client.post("/v2/orders", json={
                "symbol": o.symbol, "qty": str(o.quantity), "side": side,
                "type": "market", "time_in_force": "day",
                "client_order_id": f"{coid}-close"})
            r.raise_for_status()
            rec["close_id"] = r.json()["id"]
            rec["state"] = "closing"
        except Exception as exc:
            log.error("alpaca close failed %s: %s", coid, exc)

    async def _poll_loop(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("alpaca poll error: %s", exc)
            await asyncio.sleep(self._poll)

    async def poll_once(self) -> None:
        """Check order states and emit fills. Public for tests."""
        for coid, rec in list(self._orders.items()):
            if rec["state"] == "submitted" or (rec["state"] == "open"
                                               and not rec.get("legs_done")):
                await self._check_order(coid, rec)
            if rec["state"] == "closing" and rec.get("close_id"):
                await self._check_close(coid, rec)

    async def _check_order(self, coid: str, rec: dict) -> None:
        r = await self._client.get(f"/v2/orders/{rec['alpaca_id']}",
                                   params={"nested": "true"})
        r.raise_for_status()
        data = r.json()
        o: BracketOrder = rec["order"]
        if data["status"] == "filled" and not rec["entry_reported"]:
            px = float(data.get("filled_avg_price") or 0.0)
            rec.update(state="open", entry_reported=True, entry_price=px,
                       entry_ts=_ts(data.get("filled_at")))
            if self.on_fill:
                self.on_fill(Fill(coid, o.symbol, o.direction, o.quantity, px,
                                  0.0, rec["entry_ts"], "entry"))
        if rec["state"] == "open":
            for leg in data.get("legs") or []:
                if leg.get("type") == "stop" and leg.get("id"):
                    rec["stop_leg_id"] = leg["id"]
                if leg.get("status") == "filled":
                    kind = "take_profit" if leg.get("type") == "limit" else "stop_loss"
                    px = float(leg.get("filled_avg_price") or 0.0)
                    rec["legs_done"] = True
                    self._orders.pop(coid, None)
                    if self.on_fill:
                        self.on_fill(Fill(coid, o.symbol,
                                          "SELL" if o.direction == "LONG" else "BUY",
                                          o.quantity, px, 0.0,
                                          _ts(leg.get("filled_at")), kind))
                    break
        elif data["status"] in ("canceled", "rejected", "expired"):
            log.warning("alpaca order %s ended as %s", coid, data["status"])
            self._orders.pop(coid, None)

    async def _check_close(self, coid: str, rec: dict) -> None:
        r = await self._client.get(f"/v2/orders/{rec['close_id']}")
        r.raise_for_status()
        data = r.json()
        if data["status"] == "filled":
            o: BracketOrder = rec["order"]
            px = float(data.get("filled_avg_price") or 0.0)
            self._orders.pop(coid, None)
            if self.on_fill:
                self.on_fill(Fill(coid, o.symbol,
                                  "SELL" if o.direction == "LONG" else "BUY",
                                  o.quantity, px, 0.0, _ts(data.get("filled_at")),
                                  rec.get("close_reason", "manual_close")))


def _ts(iso: str | None) -> float:
    if not iso:
        import time
        return time.time()
    from datetime import datetime
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
