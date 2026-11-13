# ARGUS — Autonomous Trading Intelligence

Autonómny AI day-trading systém: Windows aplikácia (trading engine), Cloudflare
mobilný dashboard a adaptívne machine learning jadro.

> Upozornenie: neexistuje garancia ziskovosti. Cieľom je kladná očakávaná
> hodnota pri prísne kontrolovanom riziku. Live trading je deaktivovaný,
> kým ho používateľ výslovne nepovolí.

## Stav projektu

| Fáza | Obsah | Stav |
|---|---|---|
| 1 | Technická analýza (brokeri, dáta, stack, architektúra) | Hotovo – `docs/FAZA-1-technicka-analyza.md` |
| 2 | Vizuálny koncept ARGUS (identita, dizajnový systém, návrhy UI) | Hotovo – `docs/FAZA-2-vizualny-koncept.md`, `design/tokens.json` |
| 3 | Základ enginu: market data, DB, monitoring, lokálne API | Hotovo – tento balík (`argus/`) |
| 4+ | Trading engine, ML, news, backtesting, paper/live, cloud, mobil | Nasleduje |

## Spustenie enginu (vývoj)

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m argus          # engine + lokálne API na 127.0.0.1:7421
```

Endpointy: `GET /api/health`, `GET /api/status`,
`GET /api/candles/{symbol}/{tf}` (tf: 1m/5m/15m/1h/4h), `GET /api/audit`,
`WS /ws` (stream udalostí pre UI).

Konfigurácia cez premenné prostredia s prefixom `ARGUS_`
(napr. `ARGUS_CRYPTO_SYMBOLS='["BTC/USD","ETH/USD"]'`, `ARGUS_ALPACA_KEY_ID=...`).
Dáta sa ukladajú do `./data` (Windows: `%APPDATA%/ARGUS`).

## Testy

```bash
.venv/bin/python -m pytest          # unit testy (offline)
.venv/bin/python -m pytest -m live  # live test proti Kraken public API
```

## Architektúra (fáza 3)

```
providers (Kraken REST / Alpaca REST)  ->  1m candles
    -> CandleAggregator (5m/15m/1h/4h, bez look-ahead)
    -> SQLite (WAL) cache + EventBus -> FastAPI (loopback) -> desktop UI
HealthMonitor: heartbeaty komponentov, stale detekcia, audit log
```

Detaily a rozhodnutia: `docs/FAZA-1-technicka-analyza.md`.
