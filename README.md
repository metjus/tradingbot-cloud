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
| 4 | Trading engine: indikátory, 8 stratégií, Decision Engine, Risk Supervisor (signal-only) | Hotovo |
| 5 | ML: backtest jadro, setup-scorer s walk-forward validáciou, registry modelov s rollbackom, adaptívny výber stratégií | Hotovo |
| 6 | News & sentiment intelligence, ekonomický kalendár s blackoutmi | Hotovo |
| 7 | Backtest CLI: IS/OOS split, citlivosť prahu, per-stratégia metriky | Hotovo |
| 8 | Paper trading: PaperBroker, portfólio, PAUSE/EMERGENCY STOP, obchodné hodiny | Hotovo |
| 9–11 | Cloudflare Worker + D1, mobilný PWA dashboard, web push | Hotovo (kód; nasadenie: `docs/NASADENIE-cloud.md`) |
| 12 | Alpaca broker adaptér (bracket u brokera), desktop terminál UI, Windows shell + balenie | Hotovo (EXE build na Windows: `desktop/BUILD-WINDOWS.md`) |
| 13 | Integračné testy celého enginu + API + ovládania | Hotovo |
| 14 | Live trading | Zamknuté – vyžaduje výslovné povolenie používateľa |

> Predchádzajúci Cloudflare/Trading 212 demo bot (v0.6.3) je zachovaný vo vetve `archiv/tradingbot-v0.6.3`.

**Začni tu:** `docs/ZACIATOK.md` (prvé spustenie krok za krokom). Kompletná konfigurácia: `docs/INSTALACIA.md`.

### ML pipeline

```bash
.venv/bin/python -m argus.ml --symbols "BTC/USD,ETH/USD" --days 60   # tréning + validácia
.venv/bin/python -m argus.ml --list                                   # registry modelov
.venv/bin/python -m argus.ml --activate 3                             # vedomá aktivácia verzie
.venv/bin/python -m argus.ml --rollback                               # návrat na predošlú verziu
```

Kandidát sa nikdy neaktivuje automaticky: musí prejsť walk-forward bránami
(min. vzorka, priemerné AUC, žiadny fold pod 0.5) a aktivácia je samostatný
krok. Engine načíta aktívny model pri štarte; bez modelu beží plnohodnotne
ďalej (ML faktor = 0).

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
