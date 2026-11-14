"""Phase 13: end-to-end integration test of the whole engine process.

Boots the real Engine (no external providers, news off), feeds synthetic
market data through the real pipeline (aggregator -> analysis -> decision
-> execution -> portfolio) and exercises the HTTP API including controls.
"""

import asyncio
import random
import time

import httpx
import pytest

from argus.config import Settings
from argus.engine import Engine
from argus.marketdata.model import Candle, Timeframe

PORT = 7622


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path / "data", api_port=PORT,
                    crypto_symbols=[], stock_symbols=[], news_enabled=False,
                    enter_threshold=60.0, paper_equity=10_000.0)


def synth_m1(symbol: str, n: int, start_ts: int, drift: float) -> list[Candle]:
    rng = random.Random(3)
    out, price = [], 100.0
    for i in range(n):
        o = price
        c = price + drift + rng.uniform(-0.05, 0.05)
        h = max(o, c) + rng.uniform(0, 0.04)
        lo = min(o, c) - rng.uniform(0, 0.04)
        vol = 5.0 + (10.0 if i % 30 == 0 else 0.0)
        out.append(Candle(symbol=symbol, timeframe=Timeframe.M1, ts=start_ts + i * 60,
                          open=o, high=h, low=lo, close=c, volume=vol, source="synthetic"))
        price = c
    return out


async def test_full_engine_boot_api_and_pipeline(settings):
    engine = Engine(settings)
    run_task = asyncio.create_task(engine.run())
    try:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT}",
                                     timeout=5.0) as client:
            for _ in range(60):
                try:
                    if (await client.get("/api/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.2)
            else:
                pytest.fail("engine API did not come up")

            # feed 30h of rising synthetic 1m bars through the real pipeline
            start = int(time.time()) - 30 * 3600
            for bar in synth_m1("SYN/USD", 30 * 60, start, drift=0.03):
                engine._on_m1(bar)
            await asyncio.sleep(1.0)  # let analysis/execution loops process

            # API surface
            r = await client.get("/api/candles/SYN/USD/5m?limit=10")
            assert len(r.json()["candles"]) == 10

            r = await client.get("/api/portfolio")
            p = r.json()
            assert p["available"] and p["bot_state"] == "running"

            r = await client.get("/api/decisions?limit=50")
            decisions = r.json()["decisions"]
            assert decisions, "pipeline must have evaluated the synthetic uptrend"

            # if the uptrend produced an entry, the trade went through the
            # whole broker/portfolio path
            if p["open_positions"]:
                pos = p["open_positions"][0]
                assert pos["stop_loss"] < pos["entry"] < pos["take_profit"]

            # controls over the API
            r = await client.post("/api/control", json={"action": "pause", "command_id": "it-1"})
            assert r.json()["ok"] and r.json()["state"] in ("paused", "pausing")
            r = await client.post("/api/control", json={"action": "start"})
            assert r.json()["state"] == "running"
            r = await client.post("/api/control", json={"action": "emergency_stop"})
            assert r.json()["state"] == "stopped"
            r = await client.get("/api/portfolio")
            assert r.json()["open_positions"] == []

            # audit trail recorded the lifecycle
            r = await client.get("/api/audit?limit=500")
            msgs = [e["message"] for e in r.json()["entries"]]
            assert any("Engine running" in m for m in msgs)
            assert any("Bot state -> stopped" in m for m in msgs)

            # every GET endpoint the terminal uses must answer 200 - one
            # failing endpoint used to blank whole UI sections
            for ep in ["/api/health", "/api/status", "/api/portfolio", "/api/trades",
                       "/api/decisions", "/api/news", "/api/events", "/api/models",
                       "/api/strategies", "/api/risk", "/api/config", "/api/readiness",
                       "/api/settings", "/api/llm", "/api/ml/status", "/api/audit",
                       "/api/candles/SYN/USD/5m", "/api/indicators/SYN/USD/5m",
                       "/api/knowledge/export"]:
                r = await client.get(ep)
                assert r.status_code == 200, f"{ep} -> {r.status_code}: {r.text[:300]}"

            # settings round-trip over the API
            r = await client.post("/api/settings", json={"budget_eur": 1234})
            assert r.json()["ok"] and r.json()["values"]["budget_eur"] == 1234
            r = await client.post("/api/settings", json={"fee_venue": "nope"})
            assert r.status_code == 400

            # UI shell is served
            r = await client.get("/app/")
            assert r.status_code == 200 and "ARGUS" in r.text
    finally:
        if engine._server is not None:
            engine._server.should_exit = True
        try:
            await asyncio.wait_for(run_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            run_task.cancel()


async def test_engine_refuses_live_mode(tmp_path):
    with pytest.raises(SystemExit):
        Engine(Settings(data_dir=tmp_path / "d", trading_mode="live",
                        crypto_symbols=[], news_enabled=False))
