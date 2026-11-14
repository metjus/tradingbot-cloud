"""LLM providers (mocked), secret store, knowledge export/import, per-coin
strategy selection and calibration."""

import json

import httpx
import numpy as np
import pytest

from argus.analysis.regime import Regime
from argus.analysis.selector import StrategySelector
from argus.backtest.engine import SignalRecord
from argus.db.database import Database
from argus.db.schema import Trade
from argus.marketdata.model import Timeframe
from argus.ml.dataset import build_dataset
from argus.ml.knowledge import export_bundle, import_bundle
from argus.ml.model import SetupScorer
from argus.ml.registry import ModelRegistry
from argus.ml.training import calibration_profile, load_profiles, save_profiles
from argus.news.llm import LLMRouter, estimate_monthly_usd
from argus.runtime_settings import RuntimeSettings
from argus.secrets import SecretStore
from argus.strategy.base import Signal


# --- secrets ----------------------------------------------------------------
def test_secret_store_roundtrip(tmp_path):
    s = SecretStore(tmp_path)
    assert s.get("openai_api_key") is None
    s.set("openai_api_key", "sk-test-123")
    assert s.get("openai_api_key") == "sk-test-123"
    assert "sk-test-123" not in s.path.read_text()        # never stored in clear text form
    assert s.configured() == {"openai_api_key": True}
    s.delete("openai_api_key")
    assert s.get("openai_api_key") is None


# --- LLM router -----------------------------------------------------------------
@pytest.fixture
def router(tmp_path):
    db = Database(tmp_path / "l.db")
    rs = RuntimeSettings(db)
    sec = SecretStore(tmp_path)
    return LLMRouter(rs, sec), rs, sec


async def test_router_off_by_default(router):
    r, rs, sec = router
    assert await r.analyze("t", "s", ["BTC/USD"]) is None


async def test_openai_provider(router):
    r, rs, sec = router
    rs.update({"llm_provider": "openai", "llm_model": "gpt-5-mini"})
    sec.set("openai_api_key", "sk-x")
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers["authorization"]
        body = json.loads(req.content)
        seen["format"] = body["response_format"]["type"]
        assert "<untrusted_article>" in body["messages"][1]["content"]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(
                {"sentiment": 2.5, "intensity": 0.7, "confidence": 0.9, "rationale": "ok"})}}],
            "usage": {"prompt_tokens": 700, "completion_tokens": 100}})

    r._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    out = await r.analyze("Bitcoin ETF approved", "big", ["BTC/USD"])
    assert seen == {"auth": "Bearer sk-x", "format": "json_schema"}
    assert out["sentiment"] == 1.0            # clamped
    assert out["provider"] == "openai"
    st = r.status()
    assert st["today"]["calls"] == 1
    assert st["today"]["usd"] == pytest.approx((700 * 0.25 + 100 * 2.0) / 1e6)


async def test_gemini_provider_and_budget(router):
    r, rs, sec = router
    rs.update({"llm_provider": "gemini", "llm_daily_budget_usd": 0.0})
    sec.set("gemini_api_key", "g-key")
    calls = []

    def handler(req):
        calls.append(req.headers["x-goog-api-key"])
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": '{"sentiment": -0.4, "intensity": 0.5, "confidence": 0.6, "rationale": "x"}'}]}}],
            "usageMetadata": {"promptTokenCount": 600, "candidatesTokenCount": 50}})

    r._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await r.analyze("a", "b", ["ETH/USD"]) is None   # budget 0 -> no call
    assert calls == []
    rs.update({"llm_daily_budget_usd": 1.0})
    out = await r.analyze("a", "b", ["ETH/USD"])
    assert calls == ["g-key"] and out["sentiment"] == -0.4


async def test_claude_provider_via_sdk(router):
    import anthropic
    r, rs, sec = router
    rs.update({"llm_provider": "claude", "llm_model": "claude-haiku-5-5"})
    sec.set("anthropic_api_key", "sk-ant-x")
    sent = {}

    def handler(req):
        body = json.loads(req.content)
        sent.update(model=body["model"], effort=body["output_config"]["effort"],
                    fmt=body["output_config"]["format"]["type"],
                    key="sk-ant-x" in " ".join(str(v) for v in req.headers.values()))
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": body["model"],
            "content": [{"type": "text", "text": '{"sentiment": 0.6, "intensity": 0.4, "confidence": 0.8, "rationale": "r"}'}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 650, "output_tokens": 120}})

    import httpx2   # anthropic SDK 2.x transport package

    def handler2(req):
        resp = handler(req)
        return httpx2.Response(resp.status_code, json=json.loads(resp.content))

    r._anthropic = anthropic.AsyncAnthropic(
        api_key="sk-ant-x",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler2)))
    r._anthropic_key = "sk-ant-x"
    out = await r.analyze("t", "s", ["BTC/USD"])
    assert sent == {"model": "claude-haiku-5-5", "effort": "low", "fmt": "json_schema",
                    "key": True}
    assert out["sentiment"] == 0.6


async def test_provider_error_falls_back(router):
    r, rs, sec = router
    rs.update({"llm_provider": "openai"})
    sec.set("openai_api_key", "k")
    r._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(500, json={"error": "boom"})))
    assert await r.analyze("t", "s", ["BTC/USD"]) is None
    assert r.status()["errors_today"] == 1


def test_cost_estimates():
    assert estimate_monthly_usd("claude-haiku-5-5") < estimate_monthly_usd("claude-opus-5-5")
    assert estimate_monthly_usd("unknown-model") is None


# --- per-coin strategy selection -------------------------------------------------
def rec(sym, strat, outcome, pl_r):
    s = Signal(strat, sym, "LONG", 100, 99, 102, 0.6, [], Timeframe.M5)
    return SignalRecord(signal=s, bar_index=0, ts=0, regime=Regime.SIDEWAYS, score=70,
                        features={}, outcome=outcome, pl_r=pl_r)


def test_per_coin_disable_from_lived_trades():
    sel = StrategySelector(min_sample=1000)   # global rule out of the way
    sel.ingest([rec("SOL/USD", "breakout", "loss", -1.0) for _ in range(12)] +
               [rec("SOL/USD", "breakout", "win", 2.0) for _ in range(4)] +
               [rec("BTC/USD", "breakout", "win", 2.0) for _ in range(10)] +
               [rec("BTC/USD", "breakout", "loss", -1.0) for _ in range(6)])
    assert not sel.is_enabled("breakout", "SOL/USD")     # PF 8/12 < 1
    assert sel.is_enabled("breakout", "BTC/USD")         # PF 20/6
    assert sel.is_enabled("breakout")                    # globally still on


def test_per_coin_prior_from_calibration():
    sel = StrategySelector()
    sel.set_priors({"XRP/USD": {"momentum": {"n": 40, "profit_factor_r": 0.7},
                                "pullback": {"n": 40, "profit_factor_r": 1.4},
                                "breakout": {"n": 5, "profit_factor_r": 0.2}}})
    assert not sel.is_enabled("momentum", "XRP/USD")
    assert sel.is_enabled("pullback", "XRP/USD")
    assert sel.is_enabled("breakout", "XRP/USD")          # too few samples to judge
    m = sel.symbol_matrix(["XRP/USD"], ["momentum"])
    assert m["XRP/USD"]["momentum"]["source"] == "kalibrácia"


def test_calibration_profile_is_net_of_costs():
    # every signal: +2R gross win; risk 1.0 at price 100, cost 0.24% => 0.24R
    recs = [rec("BTC/USD", "momentum", "win", 2.0) for _ in range(3)] + \
           [rec("BTC/USD", "momentum", "loss", -1.0) for _ in range(3)]
    prof = calibration_profile(recs, 0.24)["momentum"]
    assert prof["n"] == 6
    assert prof["expectancy_r"] == pytest.approx(((2 - 0.24) * 3 + (-1 - 0.24) * 3) / 6, abs=1e-3)
    assert prof["profit_factor_r"] == pytest.approx((1.76 * 3) / (1.24 * 3), abs=1e-3)


# --- knowledge export / import ----------------------------------------------------
def _seed(db, tmp_path):
    reg = ModelRegistry(db, tmp_path / "models")
    recs = [rec("BTC/USD", "momentum", "win" if i % 2 else "loss", 1.0) for i in range(120)]
    for i, r in enumerate(recs):
        r.ts = i * 1000
        r.features = {"close": 100.0, "rsi14": 70 if i % 2 else 35, "atr14": 1.0}
    ds = build_dataset(recs)
    m = SetupScorer(); m.fit(ds.X, ds.y)
    v = reg.register(m, "P(TP before SL)", {"mean_auc": 0.6}, status="validated")
    reg.activate("setup-scorer", v)
    with db.session() as s:
        s.add(Trade(client_order_id="t1", broker="paper", mode="paper", symbol="BTC/USD",
                    direction="LONG", strategy="momentum", status="closed", pl=5.0,
                    entry_price=100, exit_price=102, quantity=1, stop_loss=99,
                    take_profit=102, exit_reason="take_profit",
                    entry_context={"signal": {"entry": 100}}))
        s.commit()
    save_profiles(db, {"BTC/USD": {"momentum": {"n": 40, "profit_factor_r": 1.2}}})
    return reg


def test_knowledge_export_import_roundtrip(tmp_path):
    src_dir, dst_dir = tmp_path / "src", tmp_path / "dst"
    src_dir.mkdir(); dst_dir.mkdir()
    src_db = Database(src_dir / "a.db")
    _seed(src_db, src_dir)
    rs = RuntimeSettings(src_db)
    rs.update({"budget_eur": 777})
    data = export_bundle(src_db, src_dir / "models", rs)

    dst_db = Database(dst_dir / "b.db")
    dst_reg = ModelRegistry(dst_db, dst_dir / "models")
    dst_rs = RuntimeSettings(dst_db)
    rep = import_bundle(dst_db, dst_dir / "models", dst_reg, data, runtime=dst_rs,
                        apply_settings=False)
    assert rep["trades_added"] == 1 and rep["models_added"] == 1
    assert rep["model_activated"] == "setup-scorer v1"
    assert dst_reg.load_active("setup-scorer") is not None     # model really loads
    assert load_profiles(dst_db)["BTC/USD"]["momentum"]["n"] == 40
    assert dst_rs["budget_eur"] != 777                           # settings untouched
    # importing twice does not duplicate trades
    rep2 = import_bundle(dst_db, dst_dir / "models", dst_reg, data, runtime=dst_rs,
                         apply_settings=True)
    assert rep2["trades_added"] == 0 and rep2["trades_skipped"] == 1
    assert dst_rs["budget_eur"] == 777                           # applied on request


def test_import_rejects_garbage(tmp_path):
    db = Database(tmp_path / "c.db")
    with pytest.raises(ValueError):
        import_bundle(db, tmp_path / "m", ModelRegistry(db, tmp_path / "m"), b"not a zip")
