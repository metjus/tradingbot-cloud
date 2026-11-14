# ARGUS – inštalačný a konfiguračný návod

## 1. Windows aplikácia

### Vývojový beh
```bat
py -3.12 -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m argus          :: engine + UI na http://127.0.0.1:7421
```
Desktopové okno s tray ikonou: `pip install PySide6` a `python desktop\argus_shell.py`.
EXE + inštalátor: `desktop/BUILD-WINDOWS.md`.

### Konfigurácia (premenné prostredia alebo `.env`)

| Premenná | Default | Význam |
|---|---|---|
| `ARGUS_CRYPTO_SYMBOLS` | `["BTC/USD","ETH/USD"]` | krypto inštrumenty (Kraken dáta) |
| `ARGUS_STOCK_SYMBOLS` | `[]` | US akcie (vyžaduje Alpaca kľúče) |
| `ARGUS_ALPACA_KEY_ID` / `ARGUS_ALPACA_SECRET_KEY` | — | Alpaca kľúče (dáta + broker) |
| `ARGUS_PAPER_BROKER` | `paper` | `paper` = interný simulátor, `alpaca_paper` = Alpaca paper API (SL/TP u brokera) |
| `ARGUS_TRADING_MODE` | `paper` | `live` je zamknutý do FÁZY 14 |
| `ARGUS_PAPER_EQUITY` | `10000` | štartový kapitál paper účtu |
| `ARGUS_ENTER_THRESHOLD` | `70` | minimálne skóre pre vstup |
| `ARGUS_RISK_PER_TRADE_PCT` | `0.5` | riziko na obchod (strop 1 %) |
| `ARGUS_DAILY_MAX_LOSS_PCT` | `2.0` | denný limit straty |
| `ARGUS_MAX_OPEN_POSITIONS` / `ARGUS_MAX_DAILY_TRADES` | `4` / `15` | limity |
| `ARGUS_NEWS_ENABLED` | `true` | news & sentiment modul |
| `ARGUS_ANTHROPIC_API_KEY` | — | voliteľné LLM spresnenie správ (samostatné účtovanie!) |
| `ARGUS_LLM_DAILY_BUDGET_USD` | `1.0` | denný strop LLM nákladov |
| `ARGUS_CLOUD_URL` / `ARGUS_CLOUD_TOKEN` | — | Cloudflare sync (návod: `NASADENIE-cloud.md`) |

Dáta: `%APPDATA%\ARGUS` (DB, logy, modely). Kľúče sa nikdy neposielajú do cloudu.

## 2. ML a backtesting

```bash
python -m argus.backtest --symbols "BTC/USD,ETH/USD" --days 60   # metriky IS/OOS
python -m argus.ml --symbols "BTC/USD,ETH/USD" --days 60         # tréning + validácia
python -m argus.ml --list / --activate N / --rollback
```

## 3. Ekonomický kalendár

JSON súbor (pozri `docs/FAZA-5-machine-learning.md` formát v `calendar.py`)
importuješ v Python konzole alebo cez vlastný skript:
```python
from argus.config import settings; from argus.db.database import Database
from argus.news.calendar import EventCalendar
EventCalendar(Database(settings.db_path)).import_json(Path("events.json"))
```
Počas high-impact okna (−30/+15 min) engine neotvára nové pozície.

## 4. Mobil + push

`docs/NASADENIE-cloud.md` (Worker, D1, Pages, VAPID). iPhone: pridať na
plochu → povoliť notifikácie v Ovládaní.

## 5. Prechod na LIVE (FÁZA 14 – zatiaľ zamknuté)

Vyžaduje: dokončený Alpaca live adaptér prepnutý vedome používateľom,
splnené validačné kritériá paper tradingu a výslovný súhlas. `trading_mode=live`
dnes engine odmietne spustiť — je to zámer.
