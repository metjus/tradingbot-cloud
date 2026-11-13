# FÁZA 1 – Technická analýza a návrh architektúry

Dátum: 2026-11-12
Stav: DOKONČENÉ (podklad pre schválenie pred implementáciou)

---

## 1. Analýza brokerov

### 1.1 Trading 212 (primárne skúmaný)

Overené z oficiálnej dokumentácie (docs.trading212.com, november 2026):

**Čo API poskytuje:**
- REST API v0 (`live.trading212.com/api/v0`, demo: `demo.trading212.com/api/v0`)
- Kategórie endpointov: Accounts, Instruments (metadáta + obchodné hodiny), Orders, Positions, Historical events
- Typy príkazov: MARKET, LIMIT, STOP, STOP_LIMIT (smer podľa znamienka quantity)
- Paper/Demo prostredie existuje a je oficiálne podporované
- API je dostupné **iba pre Invest a Stocks ISA účty** (nie CFD)

**Kritické obmedzenia pre autonómny day trading:**

| Obmedzenie | Dôsledok |
|---|---|
| Žiadne trhové dáta (ceny, OHLCV, streaming) | Dáta treba brať z iného zdroja → riziko cenovej nekonzistencie medzi analýzou a exekúciou |
| Žiadne bracket/OCO príkazy, SL/TP nemožno pripojiť k pozícii | Ochranné príkazy by musel riadiť lokálny engine → pri výpadku PC/internetu pozícia bez ochrany u brokera (porušuje požiadavku §3C zadania) |
| Invest účet = iba LONG, bez shortu, bez páky | Polovica day-trading stratégií nepoužiteľná |
| Prísne rate limity (market 50/min, stop/limit rádovo 1 req/2–5 s) | Pomalá reakcia, riziko duplicít pri retry (API samo varuje) |
| API stále označované ako beta, bez SLA | Nevhodné ako jediný pilier autonómneho systému |
| T+2/frakčné vysporiadanie, pattern-day-trading nevzniká (EU), ale intradenné otáčanie hotovosti je pomalé | Obmedzený počet obchodov denne pri malom kapitáli |

**Verdikt:** Trading 212 **nie je vhodný ako primárny exekučný broker** autonómneho day-trading bota. Ponecháme ho ako **voliteľný adaptér** (dlhodobejšie swingové LONG pozície na akciách), ale nie ako základ.

### 1.2 Odporúčaná alternatíva: Alpaca Markets (primárny broker)

Overené z docs.alpaca.markets:

- **Plnohodnotné Paper Trading** zadarmo (simulované plnenie proti reálnym kvótam) – presne to, čo vyžaduje FÁZA 8
- **Bracket orders (OCO/OTO)** – SL aj TP držané **na strane brokera** → spĺňa požiadavku, že ochranné príkazy bežia u brokera aj pri výpadku
- **Short selling** podporovaný (US equities)
- **WebSocket streaming** trhových dát; Free tier: IEX feed, do 30 symbolov súčasne, historické dáta 200 req/min (15 min delay pre SIP) – pre day trading na pár inštrumentoch postačuje; platený tier (Algo Trader Plus) odstráni limity, ak bude treba
- Krypto obchodovanie dostupné cez Alpaca (podľa jurisdikcie; v paper režime funguje vždy)
- Oficiálne SDK `alpaca-py`, stabilné API v2, bez poplatkov za US akcie
- Účty pre medzinárodných klientov (vrátane EÚ) pre US akcie

### 1.3 Krypto vetva (24/7 trh, ideálny pre vývoj a validáciu)

- **Kraken** (EÚ-friendly, WebSocket v2, conditional close = SL/TP na strane burzy) alebo **Binance** (spot OCO, najlikvidnejší, výborné dátové API, testnet)
- Krypto dáta sú zadarmo a bez licenčných obmedzení → hlavný zdroj pre backtesting a ML tréning v skorých fázach

### 1.4 Architektonický záver

Engine bude postavený na **abstraktnej vrstve `BrokerAdapter`** (jednotné rozhranie: submit/cancel/replace, bracket podpora ako capability flag, pozície, účet, fills). Implementácie:

1. `PaperBrokerAdapter` – interný simulátor (FÁZA 8)
2. `AlpacaAdapter` – paper aj live (primárny)
3. `KrakenAdapter` / `BinanceAdapter` – krypto (voliteľné, 2. vlna)
4. `Trading212Adapter` – voliteľný, obmedzený režim LONG-only, s lokálnou správou SL/TP a výrazným upozornením na riziko

Systém nikdy nepredpokladá vykonanie príkazu – stav sa potvrdzuje výhradne z fill-reportov/pollingu brokera; všetky príkazy majú klientské idempotentné ID.

---

## 2. Dátové zdroje

| Účel | Zdroj | Poznámka |
|---|---|---|
| Live ceny + OHLCV (akcie) | Alpaca WebSocket + REST (IEX free) | Rovnaký zdroj ako exekúcia → konzistencia |
| Live ceny + OHLCV (krypto) | Kraken/Binance WebSocket | Zadarmo, bez limitov podstatných pre nás |
| Historické dáta na backtest | Alpaca REST (akcie), Binance/Kraken REST (krypto) | Sťahované do lokálnej SQLite/Parquet cache |
| Ekonomický kalendár | Finnhub (free tier) / oficiálne kalendáre Fed, ECB, BLS (RSS/ICS) | Overí sa vo FÁZE 6 |
| Správy | Oficiálne RSS: ECB, Fed, CNBC, FT headlines, firemné IR; Finnhub news API | Iba legálne zdroje, bez scrapingu za paywall |
| Sentiment | Odvodený z news pipeline (lokálny FinBERT model + voliteľne LLM) | FÁZA 6 |
| TradingView | Voliteľné TradingView Alerts → webhook do Cloudflare Workera → príkazový kanál | Len ako doplnkový signál, nie zdroj dát (TV nemá verejné dátové API) |

Každý tick/bar nesie zdroj + timestamp; engine validuje čerstvosť (staleness watchdog), monotóniu časových pečiatok a rozdiel voči cene brokera pred vstupom.

---

## 3. Technologický stack (rozhodnutie)

### Windows aplikácia

**Python 3.12 engine + lokálne FastAPI jadro + desktop shell (PySide6/QWebEngine) s React UI.**

Dôvody:
- ML jadro (XGBoost, LightGBM, scikit-learn, voliteľne PyTorch) je v Pythone → jeden jazyk pre engine, backtest aj ML, žiadne IPC medzi C# a Pythonom
- Prémiové UI podľa požiadaviek §5 sa dosiahne web-technológiou (React + lightweight-charts od TradingView) vložené do natívneho okna (PySide6 QWebEngineView) s tray ikonou, autostartom a Windows integráciou
- Engine beží ako samostatný proces nezávislý od UI → zatvorenie/minimalizácia okna nezastaví obchodovanie
- Balenie: PyInstaller + Inno Setup → jeden inštalátor .exe

Alternatíva C#/.NET WPF bola zvážená a zamietnutá: dvojjazyčný stack (C# UI + Python ML) zvyšuje zložitosť údržby a krehkosť IPC bez prínosu pre výkon (latencie day-tradingu sú v sekundách, nie mikrosekundách).

### Lokálna databáza
- **SQLite (WAL mode)** cez SQLAlchemy – obchody, rozhodnutia, audit log, konfigurácia
- **Parquet** súbory pre historické OHLCV a ML feature store
- Verzie ML modelov: adresárový registry + metadáta v SQLite, rollback = prepnutie aktívnej verzie

### Cloud vrstva
- **Cloudflare Workers** (API + WebSocket cez Durable Objects), **D1** (stav, história pre mobil), **Pages** (PWA dashboard), **Web Push** (VAPID; iOS vyžaduje inštaláciu na plochu – bude v návode)
- Windows app drží **odchádzajúce** WebSocket/HTTPS spojenie (outbound-only, žiadne otvorené porty)
- Príkazy z mobilu: `{id (ULID), type, params, issued_at}` → Worker front → app ACK (prijaté) → RESULT (vykonané/zamietnuté); deduplikácia podľa id, expirácia príkazov

### AI vrstvy
- **Lokálne ML** (XGBoost/LightGBM + sklearn): režimy trhu, skórovanie setupov, P(TP pred SL), volatilita – beží offline, nulové API náklady
- **LLM API (Claude/OpenAI)** – výhradne news/sentiment interpretácia a vysvetlenia; s cache, rozpočtovým limitom, vypínačom a degradovaným režimom (engine plne funkčný bez LLM)
- Claude subscription ≠ API: runtime analýzy pôjdu cez samostatný API kľúč s vlastným účtovaním, alebo budú vypnuté

---

## 4. Architektúra systému

```
┌───────────────────────────── WINDOWS 11 PC ─────────────────────────────┐
│  ┌──────────── ARGUS ENGINE (headless proces, autostart, tray) ──────┐  │
│  │ MarketData ─► Features/TA ─► Regime ─► Strategies ─► Decision     │  │
│  │     │                                        │          │ score   │  │
│  │ News/Sentiment ──────────────────────────────┘          ▼         │  │
│  │                                   RiskSupervisor (tvrdé limity,   │  │
│  │                                   nezávislý od AI, právo veta)    │  │
│  │                                              │                    │  │
│  │ SQLite+Parquet ◄── TradeMemory ◄── Execution (BrokerAdapter) ─────┼──┼─► Broker
│  │        ▲                               ▲ fills/sync               │  │   (Alpaca…)
│  │   ML Training/Registry            Portfolio/PnL                   │  │
│  │        └── Scheduler, Watchdogs, AuditLog, CloudSync ─────────────┼──┼─► Cloudflare
│  └────────────────────────────────────────────────────────────────────┘ │        │
│  Desktop UI (PySide6 + React, localhost API/WS, tray)                   │        ▼
└──────────────────────────────────────────────────────────────────────────┘  Worker+DO+D1
                                                                               │   │
                                                                          PWA mobil + Push
```

Zásady:
- **Risk vrstva je kód, nie model** – pevné limity (0,5 %/obchod, max 1 %, denný DD 2 %, povinný SL…) vynucované deterministicky; AI ju nemôže obísť
- **Obchoduje sa lokálne**; Cloudflare je iba zrkadlo stavu + príkazový kanál
- Pri výpadku cloudu engine beží ďalej; SL/TP sú u brokera (bracket)
- Externé správy = nedôveryhodný vstup; nikdy nemenia konfiguráciu ani pravidlá (prompt-injection ochrana: news pipeline má výstup iba ako štruktúrovaný sentiment/impact objekt)

---

## 5. Riziká a obmedzenia (otvorene)

1. Trading 212 nemožno použiť tak, ako predpokladalo zadanie – navrhnutá náhrada Alpaca + krypto burzy (dôvody vyššie)
2. Alpaca free dáta = IEX feed (tenší objem než SIP); pre začiatok OK, pre škálovanie platený tier
3. Krypto u Alpacy nemusí byť v EÚ live dostupné – krypto live rieši Kraken/Binance adaptér
4. Garancia ziskovosti neexistuje; cieľom je kladná očakávaná hodnota po nákladoch, validovaná walk-forward testom a paper tradingom
5. iOS Web Push funguje len po pridaní PWA na plochu (iOS 16.4+) – súčasť návodu

---

## 6. Ďalší krok

FÁZA 2 – vizuálny koncept (názov, identita, dizajnový systém, návrhy Windows + mobile UI) → po schválení FÁZA 3.
