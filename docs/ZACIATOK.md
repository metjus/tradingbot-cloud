# Prvé spustenie (Windows 11)

## Odporúčaný spôsob: inštalátor

1. Stiahni **ARGUS-setup.exe**:
   https://github.com/metjus/tradingbot-cloud/releases/latest
2. Spusti ho a preklikaj inštaláciu (môžeš zaškrtnúť odkaz na ploche
   a automatické spúšťanie po štarte Windows).
3. Spusti **ARGUS** z plochy alebo Štart menu. Otvorí sa okno terminálu;
   engine beží na pozadí (ikona v systémovej lište).

Nie je potrebný Python, Git ani žiadne príkazy. Windows SmartScreen môže
pri prvom spustení varovať pred neznámym vydavateľom — klikni
*Ďalšie informácie → Spustiť*.

Čo skontrolovať počas prvých ~15 minút:
- v hlavičke svieti **ENGINE OK** a **PAPER**
- graf BTC/USD má sviečky a indikátory
- obrazovka **System**: komponenty `ok` (news môže hlásiť 3/4 feedy – v poriadku)
- po uzávere 5m sviečky sa v **AI Analysis** objavia prvé vyhodnotenia

Ak niečo nejde: log je v `%APPDATA%\ARGUS\logs\argus.log` — pošli mi jeho
obsah a opravím to.

## Ďalšie kroky

- **Nechaj paper trading bežať** (PC zapnuté; krypto sa obchoduje 24/7).
  Pokrok k live režimu: **System → Pripravenosť na live trading**.
- **Mobil + push notifikácie** (voliteľné): `docs/MOBIL-NAVOD.md`.
- **Nastavenia** (rozpočet, max. na pozíciu, burza a poplatky, mince, AI):
  priamo v aplikácii v sekcii **Settings**; dáta sú v `%APPDATA%\ARGUS`.
- **Prvé spustenie**: približne 2 minúty po štarte sa na pozadí spustí kalibrácia
  mincí a prvý tréning modelu (niekoľko minút) - bot medzitým beží normálne.

## Alternatíva pre vývoj: beh zo zdrojákov

```bat
git clone https://github.com/metjus/tradingbot-cloud.git argus
cd argus
start-argus.bat test     (prvýkrát nainštaluje všetko; musí skončiť "100 passed")
start-argus.bat          (otvorí okno)
start-argus.bat engine   (iba engine + terminál na http://127.0.0.1:7421)
```
Vyžaduje Python 3.12 (python.org, zaškrtni *Add python.exe to PATH*) a Git.
Po `git pull` spusti `start-argus.bat` znova — závislosti sa dosynchronizujú samy.
