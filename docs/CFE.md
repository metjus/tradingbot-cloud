# ARGUS CFE – samostatná verzia so stratégiou CFE

Táto verzia je **záloha / samostatná vetva** `claude/argus-cfe-strategy`.
Hlavný ARGUS (vetva `main`, inštalátor `ARGUS-setup.exe`) ostáva nezmenený.

## Čo je pridané

- **`argus/cfe/`** – CFE engine prevzatý z výskumu (metjus/trash, vetva
  `claude/tradingview-futures-indicator-4qmlz9`): `cfe.py` (setup a skóre,
  `compute()`), `engine.py` (pivoty, BOS/CHoCH, sweepy, FVG/OB, likvidita,
  HTF trend bez pohľadu do budúcnosti), `indicators.py` (EMA/RMA/ATR/RSI/MACD
  /percentrank so sémantikou Pine), `sim.py` (simulátor). Súbory sú bez
  zmeny, okrem importu numba (`_jit.py`: numba ak je k dispozícii, inak čistý
  Python s rovnakými výsledkami).
- **`argus/cfe/adapter.py`** – sviečky Argusu do CFE: uzavreté 15m sviečky
  (graf) a dlhá 1h história (vyššie TF 1h a 4h, váhy 1,5 / 2).
- **`CfeStrategy`** (`argus/strategy/cfe_strategy.py`, `name = "cfe"`) – beží
  na uzavretých 15m sviečkach, signál len keď na poslednej uzavretej sviečke
  je `sig_l` / `sig_s`. `confidence = skóre / 100`.
- **Predvolené nastavenia:** zhoda HTF ≥ 1,0, minimálne skóre 70, session
  13–24 UTC, nákladový filter k = 4, váhy skóre trend 45 / štruktúra 15 /
  objem 10 / momentum 10 / likvidita 10 / volatilita 10.
- **Vykonanie ako v CFE:** limit na prelomenej úrovni platný 12 sviečok,
  plní sa až keď cena úroveň prebije (nie len dotkne), zruší sa ak cena skôr
  dosiahne TP1. TP1 / TP2 / TP3 = 1R / 2R / 3R po tretinách, po TP1 stop na
  vstup, po TP2 stop na TP1. Režim „market“ vstupuje na close signálnej
  sviečky. (Executor bol rozšírený o limit so zrušením a o skupinu troch
  častí - ostatné stratégie sa nezmenili.)
- **Vlastný paper účet** (štatistiky, riziko 0,5 % na setup, max. 5 setupov
  naraz) a automatické vypnutie: PF pod 1 po 150 častiach alebo strata nad
  10 % rozpočtu. Karta „CFE“ na Dashboarde ukazuje skóre mincí a výsledky.
- **Samostatná aplikácia „ARGUS CFE“:** vlastný priečinok programu, dáta
  v `%APPDATA%\ARGUS-CFE`, port 7422 – môže bežať vedľa hlavného ARGUS.
  Inštalátor `ARGUS-CFE-setup.exe` (vydanie `cfe-latest`).

## Testy

- `tests/test_cfe.py`: signály `CfeStrategy` v Arguse (rovnaké okno dát, aké
  vidí bot naživo) sa zhodujú s **nezmeneným** `backtest/cfe.py` na reálnych
  dátach Kraken Futures (ETH, SOL, DOGE: 32 z 32 signálov – smer, vstup, SL,
  TP, skóre); hodnoty minulých sviečok sa nemenia, keď pribudnú nové (bez
  pohľadu do budúcnosti); limit, zrušenie pri TP1, tretiny, break-even
  a posun stopu na TP1.
- Pri porovnávaní sa našla a opravila jemnosť: keď 15m sviečka otvára novú
  4h periódu, 4h trend sa musí brať z poslednej uzavretej 4h sviečky
  (adaptér doplní formujúcu sa hodinu).

## Backtest (Kraken Futures, 2 roky, 15 mincí, predvolené nastavenia)

`python -m argus.cfe.backtest --data-dir <priečinok>` alebo
`python -m argus.cfe.backtest --days 365` (stiahne dáta z Krakenu).

| | |
|---|---|
| obchody | 1 448 (~2 denne) |
| úspešnosť | 49 % |
| priemer pred poplatkami | +0,024 R |
| poplatky | 0,046 R na obchod |
| **priemer po poplatkoch** | **−0,022 R** |
| profit factor | 0,96 |
| prvý / druhý rok | −0,044 R / −0,001 R |
| pri 0,5 % riziku | −16,7 %, max. prepad −36,7 % |

Zodpovedá to validácii vo výskume (15m ≈ −0,02 R na obchod). **CFE nemá
preukázanú kladnú očakávanú hodnotu po poplatkoch – iba paper trading,
žiadne ostré obchodovanie bez výslovného súhlasu.**
