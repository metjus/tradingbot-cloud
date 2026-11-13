"""Broker abstraction + paper broker (phase 8 execution environment).

BrokerAdapter is the single interface the executor talks to; Alpaca/Kraken
live adapters (phase 12) implement the same contract. Orders are bracket
orders: entry with mandatory stop-loss and take-profit held broker-side
(paper broker emulates that: SL/TP are evaluated against every incoming
candle, SL first - conservative).

Nothing is ever assumed filled: fills are reported through on_fill
callbacks and the executor updates state only from them.
"""

from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from ..marketdata.model import Candle

log = logging.getLogger(__name__)


def new_client_order_id() -> str:
    return f"argus-{int(time.time())}-{uuid.uuid4().hex[:10]}"


@dataclass(slots=True)
class BracketOrder:
    client_order_id: str
    symbol: str
    direction: str            # LONG | SHORT
    quantity: float
    stop_loss: float
    take_profit: float
    strategy: str = ""


@dataclass(slots=True)
class Fill:
    client_order_id: str
    symbol: str
    direction: str
    quantity: float
    price: float
    fee: float
    ts: float
    kind: str                  # entry | take_profit | stop_loss | manual_close | session_close


@dataclass(slots=True)
class BrokerPosition:
    client_order_id: str
    symbol: str
    direction: str
    quantity: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_ts: float
    strategy: str = ""


class BrokerAdapter(ABC):
    name = "broker"
    #: True when SL/TP survive engine outages on the broker's side
    server_side_brackets = False

    def __init__(self) -> None:
        self.on_fill: Callable[[Fill], None] | None = None

    @abstractmethod
    def submit_bracket(self, order: BracketOrder) -> None: ...

    @abstractmethod
    def close_position(self, client_order_id: str, reason: str = "manual_close") -> bool: ...

    @abstractmethod
    def positions(self) -> list[BrokerPosition]: ...


class PaperBroker(BrokerAdapter):
    """Simulated execution against live market data.

    Entry fills immediately at the current reference price +/- slippage
    (the engine acts on closed candles, so the reference is the latest
    close). SL/TP trigger on subsequent candles; if both lie inside one
    candle's range the STOP fills first. Fees are charged per side.
    """

    name = "paper"
    server_side_brackets = False  # emulated locally; live adapters set True

    def __init__(self, fee_pct: float = 0.1, slippage_pct: float = 0.02,
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__()
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self._clock = clock
        self._positions: dict[str, BrokerPosition] = {}
        self._last_price: dict[str, float] = {}

    # --- market data in --------------------------------------------------
    def on_candle(self, bar: Candle) -> None:
        self._last_price[bar.symbol] = bar.close
        bar_close_ts = bar.ts + bar.timeframe.seconds
        for pos in list(self._positions.values()):
            # ignore candles that fully closed before the position opened
            if pos.symbol != bar.symbol or bar_close_ts <= pos.opened_ts:
                continue
            if pos.direction == "LONG":
                hit_sl, hit_tp = bar.low <= pos.stop_loss, bar.high >= pos.take_profit
            else:
                hit_sl, hit_tp = bar.high >= pos.stop_loss, bar.low <= pos.take_profit
            if hit_sl:
                self._fill_exit(pos, pos.stop_loss, "stop_loss", bar.ts)
            elif hit_tp:
                self._fill_exit(pos, pos.take_profit, "take_profit", bar.ts)

    def last_price(self, symbol: str) -> float | None:
        return self._last_price.get(symbol)

    # --- BrokerAdapter ----------------------------------------------------
    def submit_bracket(self, order: BracketOrder) -> None:
        if order.client_order_id in self._positions:
            log.warning("duplicate client_order_id %s ignored", order.client_order_id)
            return
        ref = self._last_price.get(order.symbol)
        if ref is None:
            raise RuntimeError(f"no market price yet for {order.symbol}")
        slip = ref * self.slippage_pct / 100.0
        px = ref + slip if order.direction == "LONG" else ref - slip
        fee = px * order.quantity * self.fee_pct / 100.0
        now = self._clock()
        pos = BrokerPosition(order.client_order_id, order.symbol, order.direction,
                             order.quantity, px, order.stop_loss, order.take_profit,
                             opened_ts=now, strategy=order.strategy)
        self._positions[order.client_order_id] = pos
        if self.on_fill:
            self.on_fill(Fill(order.client_order_id, order.symbol, order.direction,
                              order.quantity, px, fee, now, "entry"))

    def close_position(self, client_order_id: str, reason: str = "manual_close") -> bool:
        pos = self._positions.get(client_order_id)
        if pos is None:
            return False
        ref = self._last_price.get(pos.symbol, pos.entry_price)
        slip = ref * self.slippage_pct / 100.0
        px = ref - slip if pos.direction == "LONG" else ref + slip
        self._fill_exit(pos, px, reason, self._clock())
        return True

    def positions(self) -> list[BrokerPosition]:
        return list(self._positions.values())

    # --- internals ----------------------------------------------------------
    def _fill_exit(self, pos: BrokerPosition, price: float, kind: str, ts: float) -> None:
        self._positions.pop(pos.client_order_id, None)
        fee = price * pos.quantity * self.fee_pct / 100.0
        exit_dir = "SELL" if pos.direction == "LONG" else "BUY"
        if self.on_fill:
            self.on_fill(Fill(pos.client_order_id, pos.symbol, exit_dir,
                              pos.quantity, price, fee, float(ts), kind))
