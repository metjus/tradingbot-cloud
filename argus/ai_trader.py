"""AI trader: a language model trades BTC the way a discretionary scalper
does it - it reads the 15-minute chart and decides where to rest a LIMIT
order (long below the price, short above it), or to do nothing; with a
position open it decides whether to hold or close. Like a human, it also
decides WHEN to look again (15 min in a fast market or near a level, up to
4 h when it is quiet); the resting order lives exactly until then.

Why an LLM and not a rule: on 2 years of data, the mechanical version of
this method (limit at a better price after a forming breakout/dip) has no
edge - 0 of 84 parameter variants were profitable after fees. What made it
work for a human is judgement about WHICH levels and WHEN to stay out.
This is an experiment in whether a model's judgement can do the same, so
it runs as its own trial account:

  * risk is a fraction of the normal risk per trade (runtime setting)
  * own caps: max open positions, entries per day, max fee share of risk
  * maker entries and maker take-profits (cheapest fees)
  * the deterministic risk supervisor checks every order; the model can
    only propose price levels, never sizes, leverage or settings
  * the engine switches the account off automatically when it loses after
    fees (same rule as the exploration mode)

The model receives numbers only (prices, indicators) - no external text -
so there is no prompt-injection surface. Every decision and its reason is
kept for the UI and the audit log.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from .analysis.service import DECISION_EVENT
from .core.events import CANDLE, EventBus
from .db.database import Database
from .marketdata.model import Candle, Timeframe
from .monitoring.health import HealthMonitor, Status
from .risk import RiskSupervisor
from .strategy.base import Signal
from .ta import indicators as ind
from .ta.features import compute_features
from .ta.tv import tv_summary

log = logging.getLogger(__name__)

STRATEGY = "ai_trader"
CHECK_MIN, CHECK_MAX = 15, 240     # minutes the model may wait before looking again
CHECK_DEFAULT = 30

SYSTEM = (
    "You are a disciplined intraday crypto futures trader. You read the 15-minute "
    "chart of one coin and decide where to rest a LIMIT order, "
    "the way an experienced scalper does: when a breakout is forming you place a "
    "LONG limit somewhat BELOW the current price (buy the dip into it); when a "
    "drop is forming you place a SHORT limit somewhat ABOVE it (sell the bounce). "
    "You also get the states of popular TradingView indicators (Supertrend, UT "
    "Bot, PSAR, Hull, Ichimoku, Squeeze Momentum, WaveTrend, MACD); treat them "
    "as context to weigh, not as rules - none of them is reliable on its own. "
    "If the picture is unclear, choppy or the move already happened, answer "
    "'none' - staying out is a valid, often the best, decision. Fees are about "
    "0.04 % per round trip when both orders rest as limits and up to 0.14 % "
    "otherwise, so tiny targets are not worth it. Always give a stop-loss beyond "
    "the level that would prove you wrong and a realistic take-profit (at least "
    "1.5x the risk). When a position is open, decide 'hold' or 'close' instead. "
    "Also decide when you want to look again (next_check_minutes, 15-240): soon "
    "when the market moves fast or price is near your level, later when it is "
    "quiet. A resting order stays valid exactly until then. "
    "Answer only with the JSON object described by the schema; the reason must "
    "be at most 30 words."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string",
                   "enum": ["long_limit", "short_limit", "none", "hold", "close"]},
        "entry": {"type": "number"},
        "stop_loss": {"type": "number"},
        "take_profit": {"type": "number"},
        "confidence": {"type": "number"},
        "next_check_minutes": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["action", "entry", "stop_loss", "take_profit", "confidence",
                 "next_check_minutes", "reason"],
    "additionalProperties": False,
}

# hard bounds on what the model may propose (fractions of the price)
MAX_ENTRY_DIST = 0.03          # limit at most 3 % away from the price
MIN_STOP_DIST = 0.0025         # stop at least 0.25 % from the entry
MAX_STOP_DIST = 0.04           # ... and at most 4 %
MIN_RR = 1.5


def _fmt(v: float) -> str:
    return f"{v:.6g}"


def build_brief(symbol: str, m15: list[Candle], h1: list[Candle], h4: list[Candle],
                price: float, funding_h: float | None, position: dict | None,
                recent: list[dict]) -> str:
    """Compact numeric market brief (~2-3k tokens)."""
    f15 = compute_features(m15)
    f1 = compute_features(h1)
    f4 = compute_features(h4)
    lines = [f"COIN {symbol}  price {_fmt(price)}  time_utc "
             f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime())}"]
    if f15 is not None:
        lines.append(
            f"15m: EMA9 {_fmt(f15.ema9 or 0)} EMA21 {_fmt(f15.ema21 or 0)} "
            f"EMA50 {_fmt(f15.ema50 or 0)} RSI14 {f15.rsi14 or 0:.0f} "
            f"ATR14 {_fmt(f15.atr14 or 0)} ({(f15.atr14 or 0) / price * 100:.2f}%) "
            f"ADX {f15.adx14 or 0:.0f} 20bar_high {_fmt(f15.high_20 or 0)} "
            f"20bar_low {_fmt(f15.low_20 or 0)} vol_vs_avg {f15.vol_ratio or 0:.2f}")
    for name, f in (("1h", f1), ("4h", f4)):
        if f is not None and f.ema21 and f.ema50:
            trend = "up" if f.ema21 > f.ema50 else "down"
            lines.append(f"{name}: trend {trend} close {_fmt(f.close)} EMA21 {_fmt(f.ema21)} "
                         f"EMA50 {_fmt(f.ema50)} RSI {f.rsi14 or 0:.0f}")
    # popular TradingView indicators as extra context (not rules: on their
    # own none of them was profitable in our 2-year test)
    for name, bars in (("15m", m15), ("1h", h1)):
        tv = tv_summary(bars[-200:]) if bars else None
        if tv:
            lines.append(f"TradingView indicators {name}: {tv}")
    if funding_h is not None:
        lines.append(f"funding per hour {funding_h * 100:+.4f}% (positive = longs pay)")
    lines.append("last 48 x 15m candles (oldest first): time open high low close volume")
    for c in m15[-48:]:
        lines.append(f"{time.strftime('%H:%M', time.gmtime(c.ts))} {_fmt(c.open)} "
                     f"{_fmt(c.high)} {_fmt(c.low)} {_fmt(c.close)} {c.volume:.4g}")
    if position:
        lines.append(
            f"OPEN POSITION: {position['direction']} entry {_fmt(position['entry'])} "
            f"stop {_fmt(position['stop_loss'])} target {_fmt(position['take_profit'])} "
            f"unrealized {position.get('unrealized_pl', 0):+.2f} EUR -> answer hold or close")
    if recent:
        lines.append("your last trades: " + "; ".join(
            f"{r['direction']} {r['pl']:+.2f}EUR ({r['reason']})" for r in recent[-5:]))
    return "\n".join(lines)


def check_minutes(decision: dict) -> int:
    """Model's re-check interval, clamped to 15-240 min on the 15m grid."""
    try:
        m = int(decision.get("next_check_minutes") or CHECK_DEFAULT)
    except (TypeError, ValueError):
        m = CHECK_DEFAULT
    m = max(CHECK_MIN, min(CHECK_MAX, m))
    return max(CHECK_MIN, round(m / 15) * 15)


def validate(decision: dict, price: float, cost_pct: float) -> tuple[Signal | None, str]:
    """Turn the model's proposal into a Signal or explain the rejection.
    Purely deterministic - the model cannot bypass any of these bounds."""
    act = decision.get("action")
    if act not in ("long_limit", "short_limit"):
        return None, ""
    d = "LONG" if act == "long_limit" else "SHORT"
    try:
        e, sl, tp = (float(decision["entry"]), float(decision["stop_loss"]),
                     float(decision["take_profit"]))
    except (KeyError, TypeError, ValueError):
        return None, "neúplné ceny"
    if d == "LONG" and not (sl < e < tp and e <= price * 1.0005):
        return None, "long limit musí byť pod cenou, stop pod ním, cieľ nad ním"
    if d == "SHORT" and not (tp < e < sl and e >= price * 0.9995):
        return None, "short limit musí byť nad cenou, stop nad ním, cieľ pod ním"
    if abs(e - price) / price > MAX_ENTRY_DIST:
        return None, f"limit je ďalej ako {MAX_ENTRY_DIST * 100:.0f} % od ceny"
    risk = abs(e - sl) / e
    if not (MIN_STOP_DIST <= risk <= MAX_STOP_DIST):
        return None, f"stop {risk * 100:.2f} % mimo povoleného pásma 0,25-4 %"
    cost = cost_pct / 100.0
    rr = (abs(tp - e) / e - cost) / (risk + cost)
    if rr < MIN_RR:
        return None, f"zisk/riziko po poplatkoch {rr:.2f} < {MIN_RR}"
    conf = max(0.0, min(1.0, float(decision.get("confidence") or 0.5)))
    reason = str(decision.get("reason") or "")[:200]
    return Signal(STRATEGY, "", d, e, sl, tp, conf, [f"AI: {reason}"],
                  timeframe=Timeframe.M15, exploration=True, sleeve="ai",
                  expire_s=check_minutes(decision) * 60), ""


class AITrader:
    def __init__(self, db: Database, bus: EventBus, health: HealthMonitor, llm,
                 runtime, risk: RiskSupervisor, executor, portfolio,
                 price_provider, funding_provider=None,
                 account_provider=None) -> None:
        self._db = db
        self._bus = bus
        self._health = health
        self._llm = llm
        self._runtime = runtime
        self._risk = risk
        self._executor = executor
        self._portfolio = portfolio
        self._price = price_provider            # symbol -> float | None
        self._funding = funding_provider         # symbol -> float | None
        self._account = account_provider         # () -> AccountState
        self._last_slot: dict[str, int] = {}
        self._next_due: dict[str, int] = {}     # symbol -> when to look again
        self.decisions: deque = deque(maxlen=40)
        self._task: asyncio.Task | None = None
        health.register("ai_trader", stale_after=4 * 3600)

    # --- status for the UI ------------------------------------------------
    def status(self) -> dict:
        ok, why = self._llm.available()
        enabled = self._runtime["ai_trader_enabled"]
        return {"enabled": enabled, "ready": ok and enabled,
                "blocked_reason": None if ok else why,
                "symbols": self._symbols(),
                "risk_pct": round(self._runtime["risk_per_trade_pct"]
                                  * self._runtime["ai_risk_scale"], 3),
                "model": self._llm.trader_model(self._runtime["ai_model"]),
                "decisions": list(self.decisions)[::-1][:20],
                "next_decision_ts": min([self._next_due.get(s) or self._next_slot()
                                         for s in self._symbols()] or [self._next_slot()])}

    @staticmethod
    def _next_slot() -> int:
        return (int(time.time()) // 900 + 1) * 900

    def _symbols(self) -> list[str]:
        return list(self._runtime["ai_symbols"])

    # --- loop ----------------------------------------------------------------
    def start(self) -> None:
        q = self._bus.subscribe(CANDLE)
        self._task = asyncio.get_running_loop().create_task(self._run(q), name="ai-trader")

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def _run(self, q: asyncio.Queue) -> None:
        while True:
            ev = await q.get()
            bar: Candle = ev.data
            if bar.timeframe is not Timeframe.M15 or not bar.complete:
                continue
            end = bar.ts + Timeframe.M15.seconds
            if bar.symbol not in self._symbols():
                continue
            if self._last_slot.get(bar.symbol) == end or end < self._next_due.get(bar.symbol, 0):
                continue
            self._last_slot[bar.symbol] = end
            if not self._runtime["ai_trader_enabled"]:
                continue
            try:
                await self.decide(bar.symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("ai trader failed for %s: %s", bar.symbol, exc)
                self._health.set_status("ai_trader", Status.DEGRADED, str(exc)[:120])

    # --- one decision ----------------------------------------------------------
    async def decide(self, symbol: str) -> dict:
        ok, why = self._llm.available()
        price = self._price(symbol)
        if not ok or price is None:
            rec = {"ts": int(time.time()), "symbol": symbol, "action": "skip",
                   "reason": why or "chýba aktuálna cena"}
            self.decisions.append(rec)
            self._health.heartbeat("ai_trader", Status.OK, f"čaká: {rec['reason']}")
            return rec

        pos = next((p for p in self._portfolio.positions.values()
                    if p.symbol == symbol), None)
        position = None
        if pos is not None:
            if pos.strategy != STRATEGY:
                rec = {"ts": int(time.time()), "symbol": symbol, "action": "skip",
                       "reason": "na mince je otvorená pozícia hlavnej stratégie"}
                self.decisions.append(rec)
                return rec
            position = {"direction": pos.direction, "entry": pos.entry_price,
                        "stop_loss": pos.stop_loss, "take_profit": pos.take_profit,
                        "unrealized_pl": pos.unrealized_pl or 0.0}

        m15 = self._db.get_candles(symbol, Timeframe.M15, limit=200)
        h1 = self._db.get_candles(symbol, Timeframe.H1, limit=200)
        h4 = self._db.get_candles(symbol, Timeframe.H4, limit=200)
        if len(m15) < 60:
            rec = {"ts": int(time.time()), "symbol": symbol, "action": "skip",
                   "reason": "málo 15m histórie"}
            self.decisions.append(rec)
            return rec
        recent = [t for t in self._portfolio.trade_history(20)
                  if t["strategy"] == STRATEGY and t["symbol"] == symbol][:5]
        recent = [{"direction": t["direction"], "pl": t["pl"] or 0.0,
                   "reason": t["exit_reason"] or ""} for t in reversed(recent)]
        brief = build_brief(symbol, m15, h1, h4, price,
                            self._funding(symbol) if self._funding else None,
                            position, recent)
        model = self._llm.trader_model(self._runtime["ai_model"])
        data = await self._llm.structured(SYSTEM, SCHEMA, brief, name="trade_decision",
                                          model=model, effort="medium", max_tokens=3000)
        rec = {"ts": int(time.time()), "symbol": symbol, "price": price}
        if data is None:
            rec.update(action="error", reason="AI neodpovedala (chyba alebo rozpočet)")
            self.decisions.append(rec)
            return rec
        rec.update({k: data.get(k) for k in ("action", "entry", "stop_loss",
                                             "take_profit", "confidence", "reason")})
        minutes = check_minutes(data)
        rec["next_check_minutes"] = minutes
        self._next_due[symbol] = (int(time.time()) // 900) * 900 + minutes * 60

        # an open position: hold or close (the model cannot move stops)
        if position is not None:
            if data.get("action") == "close":
                self._executor.command("close_position", target=pos.client_order_id)
                rec["result"] = "pozícia zatvorená"
                self._db.audit("trade", f"{symbol}: AI trader zatvoril pozíciu - "
                               f"{rec.get('reason', '')}")
            else:
                rec["result"] = "držím"
            self.decisions.append(rec)
            self._health.heartbeat("ai_trader", Status.OK, f"{symbol}: {rec['result']}")
            return rec

        # a new view replaces the previous resting order (re-placement)
        replaced = self._executor.cancel_resting(symbol, STRATEGY)
        sig, why = validate(data, price, self._risk.limits.round_trip_cost_pct)
        if sig is None:
            rec["result"] = ("bez obchodu" if data.get("action") == "none"
                             else f"zamietnuté: {why}" if why else "bez obchodu")
            if replaced:
                rec["result"] += " (starý príkaz zrušený)"
            self.decisions.append(rec)
            self._health.heartbeat("ai_trader", Status.OK, f"{symbol}: {rec['result']}")
            return rec
        sig.symbol = symbol
        sig.risk_scale = self._runtime["ai_risk_scale"]
        account = self._account() if self._account else None
        verdict = self._risk.check(sig, account, None) if account is not None else None
        if verdict is None or not verdict.approved:
            rec["result"] = "veto rizika: " + "; ".join((verdict.vetoes if verdict else ["?"])[:2])
            self.decisions.append(rec)
            return rec
        decision = {"symbol": symbol, "verdict": "enter", "regime": "ai",
                    "best": {"signal": sig.as_dict(), "score": round(sig.confidence * 100, 1),
                             "factors": {"ai_confidence": sig.confidence}},
                    "risk": verdict.as_dict(), "source": "ai_trader"}
        self._bus.publish(DECISION_EVENT, decision)
        rec["result"] = (f"{sig.direction} limit {_fmt(sig.entry)} zadaný na {minutes} min"
                         + (" (nahradil starý)" if replaced else ""))
        self._db.audit("trade", f"{symbol}: AI trader - {rec['result']}; {rec.get('reason', '')}",
                       data={"stop": sig.stop_loss, "tp": sig.take_profit,
                             "confidence": sig.confidence})
        self.decisions.append(rec)
        self._health.heartbeat("ai_trader", Status.OK, f"{symbol}: {rec['result']}")
        return rec
