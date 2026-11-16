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
    leverage: float = 1.0     # futures: notional / margin
    entry: str = "market"     # market | limit (maker at limit_price)
    limit_price: float = 0.0
    expire_s: float = 0.0     # limit only: cancel if unfilled this long


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
    leverage: float = 1.0
    liq_price: float | None = None    # isolated-margin liquidation estimate
    funding_accrued: float = 0.0      # futures funding paid (+) / received (-)
    last_funding_ts: float = 0.0


class BrokerAdapter(ABC):
    name = "broker"
    #: True when SL/TP survive engine outages on the broker's side
    server_side_brackets = False

    def __init__(self) -> None:
        self.on_fill: Callable[[Fill], None] | None = None
        #: called with (client_order_id, reason) when a resting limit entry
        #: expires unfilled
        self.on_cancel: Callable[[str, str], None] | None = None

    @abstractmethod
    def submit_bracket(self, order: BracketOrder) -> None: ...

    @abstractmethod
    def close_position(self, client_order_id: str, reason: str = "manual_close") -> bool: ...

    @abstractmethod
    def positions(self) -> list[BrokerPosition]: ...

    def update_stop(self, client_order_id: str, new_stop: float) -> bool:
        """Move the protective stop (trailing / break-even). Optional."""
        return False

    def update_take_profit(self, client_order_id: str, new_tp: float) -> bool:
        """Move the take-profit (adaptive extension). Optional."""
        return False


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
                 clock: Callable[[], float] = time.time,
                 maintenance_margin_pct: float = 0.0,
                 maker_fee_pct: float | None = None) -> None:
        super().__init__()
        self.fee_pct = fee_pct
        self.maker_fee_pct = maker_fee_pct if maker_fee_pct is not None else fee_pct
        self.slippage_pct = slippage_pct
        self.maintenance_margin_pct = maintenance_margin_pct
        #: symbol -> funding rate per hour (fraction, long pays when > 0);
        #: set by the engine from the futures ticker. None = no funding.
        self.funding_provider: Callable[[str], float | None] | None = None
        self._clock = clock
        self._positions: dict[str, BrokerPosition] = {}
        self._pending: dict[str, BracketOrder] = {}   # resting limit entries
        self._pending_expiry: dict[str, float] = {}
        self._last_price: dict[str, float] = {}

    def _liq_price(self, direction: str, entry: float, leverage: float) -> float | None:
        if leverage <= 1.0 and self.maintenance_margin_pct <= 0:
            return None
        move = 1.0 / max(1.0, leverage) - self.maintenance_margin_pct / 100.0
        if move <= 0:
            return entry
        sgn = 1.0 if direction == "LONG" else -1.0
        return entry * (1.0 - sgn * move)

    def _accrue_funding(self, pos: BrokerPosition, ts: float) -> None:
        if self.funding_provider is None:
            return
        if pos.last_funding_ts <= 0:
            pos.last_funding_ts = ts
            return
        dt = ts - pos.last_funding_ts
        if dt < 300:                   # accrue in >= 5 min chunks
            return
        rate_h = self.funding_provider(pos.symbol)
        if rate_h:
            sgn = 1.0 if pos.direction == "LONG" else -1.0
            pos.funding_accrued += sgn * rate_h * (dt / 3600.0) *                 pos.entry_price * pos.quantity
        pos.last_funding_ts = ts

    def _open_position(self, order: BracketOrder, px: float, fee: float) -> None:
        now = self._clock()
        pos = BrokerPosition(order.client_order_id, order.symbol, order.direction,
                             order.quantity, px, order.stop_loss, order.take_profit,
                             opened_ts=now, strategy=order.strategy,
                             leverage=order.leverage,
                             liq_price=self._liq_price(order.direction, px, order.leverage),
                             last_funding_ts=now)
        self._positions[order.client_order_id] = pos
        if self.on_fill:
            self.on_fill(Fill(order.client_order_id, order.symbol, order.direction,
                              order.quantity, px, fee, now, "entry"))

    def _try_fill_pending(self, symbol: str, price: float,
                          low: float | None = None, high: float | None = None) -> None:
        lo = low if low is not None else price
        hi = high if high is not None else price
        now = self._clock()
        for coid, order in list(self._pending.items()):
            if order.symbol != symbol:
                continue
            if now >= self._pending_expiry.get(coid, 0):
                self._pending.pop(coid, None)
                self._pending_expiry.pop(coid, None)
                if self.on_cancel:
                    self.on_cancel(coid, "limit vstup nevyplnený, zrušený")
                continue
            hit = lo <= order.limit_price if order.direction == "LONG"                 else hi >= order.limit_price
            if hit:
                self._pending.pop(coid, None)
                self._pending_expiry.pop(coid, None)
                px = order.limit_price
                fee = px * order.quantity * self.maker_fee_pct / 100.0
                self._open_position(order, px, fee)

    def pending_orders(self) -> list[BracketOrder]:
        return list(self._pending.values())

    def cancel_pending(self, client_order_id: str) -> bool:
        if self._pending.pop(client_order_id, None) is not None:
            self._pending_expiry.pop(client_order_id, None)
            return True
        return False

    # --- market data in --------------------------------------------------
    def on_candle(self, bar: Candle) -> None:
        self._last_price[bar.symbol] = bar.close
        self._try_fill_pending(bar.symbol, bar.close, low=bar.low, high=bar.high)
        bar_close_ts = bar.ts + bar.timeframe.seconds
        for pos in list(self._positions.values()):
            # ignore candles that fully closed before the position opened
            if pos.symbol != bar.symbol or bar_close_ts <= pos.opened_ts:
                continue
            if pos.direction == "LONG":
                hit_sl, hit_tp = bar.low <= pos.stop_loss, bar.high >= pos.take_profit
                hit_liq = pos.liq_price is not None and bar.low <= pos.liq_price
            else:
                hit_sl, hit_tp = bar.high >= pos.stop_loss, bar.low <= pos.take_profit
                hit_liq = pos.liq_price is not None and bar.high >= pos.liq_price
            if hit_liq and not hit_sl:
                self._fill_exit(pos, pos.liq_price, "liquidation", bar.ts)
            elif hit_sl:
                # gap through the stop at the open -> filled at the open
                gapped = (bar.open <= pos.stop_loss) if pos.direction == "LONG" \
                    else (bar.open >= pos.stop_loss)
                self._fill_exit(pos, bar.open if gapped else pos.stop_loss,
                                "stop_loss", bar.ts)
            elif hit_tp:
                self._fill_exit(pos, pos.take_profit, "take_profit", bar.ts)

    def on_tick(self, symbol: str, price: float, ts: float | None = None) -> None:
        """Real-time price: fill SL/TP the moment the price crosses them
        (exactly what resting orders at the broker would do)."""
        self._last_price[symbol] = price
        self._try_fill_pending(symbol, price)
        for pos in list(self._positions.values()):
            if pos.symbol != symbol:
                continue
            self._accrue_funding(pos, ts or self._clock())
            if pos.direction == "LONG":
                hit_sl, hit_tp = price <= pos.stop_loss, price >= pos.take_profit
                hit_liq = pos.liq_price is not None and price <= pos.liq_price
            else:
                hit_sl, hit_tp = price >= pos.stop_loss, price <= pos.take_profit
                hit_liq = pos.liq_price is not None and price >= pos.liq_price
            if hit_liq and not hit_sl:
                self._fill_exit(pos, price, "liquidation", ts or self._clock())
            elif hit_sl:
                # a stop becomes a market order: it fills at the market price,
                # which after a jump is WORSE than the stop level
                self._fill_exit(pos, price, "stop_loss", ts or self._clock())
            elif hit_tp:
                self._fill_exit(pos, pos.take_profit, "take_profit", ts or self._clock())

    def last_price(self, symbol: str) -> float | None:
        return self._last_price.get(symbol)

    # --- BrokerAdapter ----------------------------------------------------
    def submit_bracket(self, order: BracketOrder) -> None:
        if order.client_order_id in self._positions or                 order.client_order_id in self._pending:
            log.warning("duplicate client_order_id %s ignored", order.client_order_id)
            return
        ref = self._last_price.get(order.symbol)
        if ref is None:
            raise RuntimeError(f"no market price yet for {order.symbol}")
        if order.entry == "limit" and order.limit_price > 0:
            # a post-only maker order: rests in the book, fills when the
            # market trades through the limit, expires otherwise
            self._pending[order.client_order_id] = order
            self._pending_expiry[order.client_order_id] =                 self._clock() + (order.expire_s or 4 * 3600.0)
            self._try_fill_pending(order.symbol, ref)
            return
        slip = ref * self.slippage_pct / 100.0
        px = ref + slip if order.direction == "LONG" else ref - slip
        fee = px * order.quantity * self.fee_pct / 100.0
        self._open_position(order, px, fee)

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

    def update_take_profit(self, client_order_id: str, new_tp: float) -> bool:
        pos = self._positions.get(client_order_id)
        if pos is None:
            return False
        pos.take_profit = new_tp
        return True

    def restore(self, client_order_id: str, symbol: str, direction: str, quantity: float,
                entry_price: float, stop_loss: float, take_profit: float,
                opened_ts: float, strategy: str = "", leverage: float = 1.0) -> None:
        """Re-create a simulated position after an app restart (the paper
        book lives in memory; the portfolio DB is the source of truth)."""
        if client_order_id not in self._positions:
            self._positions[client_order_id] = BrokerPosition(
                client_order_id, symbol, direction, quantity, entry_price,
                stop_loss, take_profit, opened_ts, strategy, leverage=leverage,
                liq_price=self._liq_price(direction, entry_price, leverage))

    def update_stop(self, client_order_id: str, new_stop: float) -> bool:
        pos = self._positions.get(client_order_id)
        if pos is None:
            return False
        # a stop may only tighten, never widen
        if pos.direction == "LONG" and new_stop > pos.stop_loss:
            pos.stop_loss = new_stop
            return True
        if pos.direction == "SHORT" and new_stop < pos.stop_loss:
            pos.stop_loss = new_stop
            return True
        return False

    # --- internals ----------------------------------------------------------
    def _fill_exit(self, pos: BrokerPosition, price: float, kind: str, ts: float) -> None:
        self._positions.pop(pos.client_order_id, None)
        # a take-profit is a resting limit order -> maker fee; stops, manual
        # and adaptive closes are market orders -> taker fee. Accrued funding
        # is settled with the exit, as part of the exit fee.
        rate = self.maker_fee_pct if kind == "take_profit" else self.fee_pct
        fee = price * pos.quantity * rate / 100.0 + pos.funding_accrued
        exit_dir = "SELL" if pos.direction == "LONG" else "BUY"
        if self.on_fill:
            self.on_fill(Fill(pos.client_order_id, pos.symbol, exit_dir,
                              pos.quantity, price, fee, float(ts), kind))
