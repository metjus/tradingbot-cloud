# Prvé spustenie – postup krok za krokom (Windows 11)

## Krok 1 – Inštalácia (raz, ~10 min)

1. Nainštaluj **Python 3.12** z python.org (pri inštalácii zaškrtni *Add python.exe to PATH*)
   a **Git for Windows** z git-scm.com.
2. Stiahni projekt:
   ```bat
   git clone -b claude/autonomous-ai-trading-system-eif1dz https://github.com/metjus/trash.git argus
   cd argus
   ```
3. Over, že všetko funguje:
   ```bat
   start-argus.bat test
   ```
   Prvé spustenie vytvorí prostredie a nainštaluje balíky. Na konci musí byť `100 passed`.

## Krok 2 – Prvé spustenie v paper režime (bez kľúčov, bez rizika)

```bat
start-argus.bat
```
Otvorí sa okno ARGUS a ikona v systémovej lište. Zatvorenie okna engine nezastaví,
úplné ukončenie je cez pravý klik na ikonu.

Čo skontrolovať počas prvých ~15 minút:
- v hlavičke svieti **ENGINE OK** a **PAPER**
- graf BTC/USD má sviečky a indikátory
- obrazovka **System**: všetky komponenty `ok` (news môže hlásiť 3/4 feedy – to je v poriadku)
- po uzávere 5m sviečky sa v **AI Analysis** objavia prvé vyhodnotenia

Ak sa niečo nezobrazuje: v okne **System → Audit log** a súbor
`%APPDATA%\ARGUS\logs\argus.log` ukážu prečo. Pošli mi ich obsah.

Bez okna (iba engine + terminál v prehliadači): `start-argus.bat engine`
a otvor http://127.0.0.1:7421

## Krok 3 – Nechaj bežať paper trading

Počítač musí byť zapnutý, kým má bot obchodovať. Krypto sa obchoduje 24/7.
Pokrok k live režimu sleduješ v **System → Pripravenosť na live trading**
(cieľ: ≥ 100 obchodov, ≥ 14 dní, PF ≥ 1.3, drawdown ≤ 6 %).

## Krok 4 – Voliteľne: mobil a push notifikácie

Podľa `docs/NASADENIE-cloud.md` (potrebuješ bezplatný Cloudflare účet).
Môžeš ho spraviť kedykoľvek, paper trading beží aj bez neho.

## Krok 5 – Voliteľne: inštalátor EXE

Podľa `desktop/BUILD-WINDOWS.md`. Pre testovanie nie je potrebný –
`start-argus.bat` stačí.

## Krok 6 – Voliteľne: ML tréning a backtest

```bat
.venv\Scripts\python -m argus.backtest --days 60
.venv\Scripts\python -m argus.ml --days 60
```
Nočný tréning beží automaticky o 03:00 UTC, ak je PC zapnuté.

## Konfigurácia

Vytvor súbor `.env` v priečinku projektu, napr.:
```
ARGUS_PAPER_EQUITY=10000
ARGUS_CRYPTO_SYMBOLS=["BTC/USD","ETH/USD","SOL/USD"]
```
Všetky voľby: `docs/INSTALACIA.md`. Po zmene reštartuj ARGUS.
