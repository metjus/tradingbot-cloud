# FÁZA 2 – Vizuálny koncept

Dátum: 2026-11-13
Stav: DOKONČENÉ – čaká na schválenie používateľom pred implementáciou UI

Vizuálne návrhy (interaktívny canvas s 5 artboardmi):
https://claude.ai/artifact/GKU12VkcRZwA9aPJGuKaJm

---

## 1. Názov produktu: ARGUS

**ARGUS — Autonomous Trading Intelligence**

Pomenovaný podľa Arga Panoptésa, strážcu so sto očami z gréckej mytológie – systém, ktorý nepretržite sleduje trhy, nikdy nespí, učí sa z vlastných obchodov a koná samostatne v rámci pevných rizikových pravidiel. Krátke, zapamätateľné, funguje v slovenčine aj angličtine, bez problematických konotácií.

## 2. Logo

Znak: **oko s candlestick zrenicou** – obrys oka (dve kvadratické krivky) a vo vnútri svieca s knôtom. Spája „dohľad/bdelosť" s obchodovaním. Kreslené ako vektor (SVG), funguje od 16 px (tray ikona) po veľké formáty. Gradient Argus Blue → Violet na tmavom grafitovom podklade, zaoblený štvorec ako badge.

Wordmark: ARGUS v Space Grotesk Bold s výrazným prestrkaním (letter-spacing).

## 3. Farebná paleta

| Token | Hex | Použitie |
|---|---|---|
| `graphite.950` | `#0B0E15` | Hlavné pozadie |
| `graphite.900` | `#11151F` | Panely, karty |
| `graphite.850` | `#141A28` | Vnorené plochy, chipy |
| `graphite.800` | `#171C2A` | Vyvýšené karty |
| `line.700` | `#242C40` | Okraje, oddeľovače (jemné: `#1B2130`) |
| `text.primary` | `#E8EDF7` | Primárny text |
| `text.secondary` | `#98A2B8` | Sekundárny text |
| `text.muted` | `#5E6880` | Popisky, labely |
| `accent.blue` | `#5B8CFF` | Primárny akcent, aktívne prvky |
| `accent.violet` | `#8F7BFF` | Sekundárny akcent; gradient blue→violet pre primárne akcie a AI prvky |
| `profit` | `#35D49B` | Zisk, LONG, pozitívny stav |
| `loss` | `#F6567A` | Strata, SHORT, chyby, emergency |
| `warning` | `#F0B13F` | Varovania, paper trading, pauza |

Pravidlá: zisk/strata vždy farba + znamienko (nikdy iba farba – prístupnosť); emergency akcie vždy loss-červená s potvrdením; AI/ML prvky nesú gradient.

## 4. Typografia

| Rodina | Použitie | Váhy |
|---|---|---|
| **Space Grotesk** | Wordmark, nadpisy sekcií | 500–700 |
| **Inter** | UI texty, tabuľky, navigácia | 400–700 |
| **JetBrains Mono** | Ceny, P/L, metriky, časy (tabular numerals) | 400–600 |

Sekčné labely: 11–12 px, uppercase, letter-spacing 1.5 px, farba `text.muted`.

## 5. Dizajnový systém

- **Radius:** 6–7 px (chipy), 9–12 px (tlačidlá, karty), 14–18 px (veľké panely), 999 px (status pilulky)
- **Spacing:** stupnica 4 / 8 / 12 / 16 / 24 / 32
- **Komponenty:** stat karta, status pill (s bodkou pre live stavy), gradientové primárne tlačidlo, outline sekundárne, danger tlačidlo (tónované pozadie + border), skóre bar (gradient), SL/vstup/TP range bar pre pozície, tabuľky s mono číslami, news riadok s časom a impact štítkom
- **Zákazy (podľa zadania):** žiadne emoji v UI, žiadne generic Bootstrap vzory, žiadne zbytočné animácie; prechody jemné (150–200 ms), tiene iba na vyvýšených vrstvách
- **Stavy systému:** ONLINE (profit zelená), PAPER (warning), LIVE (accent blue), OFFLINE/STOP (loss); režim obchodovania viditeľný v headri vždy

## 6. Windows UI (artboardy „Windows – Trading Dashboard", „Windows – AI Learning")

Rozloženie: horný stavový pruh (logo, ENGINE ONLINE, PAPER/LIVE, režim trhu, broker sync, čas, EMERGENCY STOP vpravo) · ľavý navigačný panel (Dashboard, Live Trading, Trade History, AI Analysis, News Intelligence, AI Learning, Strategy Manager, Risk Management, Settings + System mini-panel) · hlavná plocha (6 stat kariet, candlestick graf s EMA/VWAP a vyznačeným vstupom, tabuľka pozícií) · pravý stĺpec (AI Decision Engine so skóre a zdôvodneniami vrátane zamietnutých obchodov, Risk Supervisor s čerpaním limitov, News Intelligence).

AI Learning obrazovka: registry modelov s verziami, validáciou a stavmi (AKTÍVNY / PAPER TEST / ARCHÍV s rollbackom), výkonnosť stratégií (PF/WR/počet obchodov, automatické pozastavenie pod PF 1.0), učiaci cyklus, detekcia režimov, analýza poslednej straty formulovaná ako hypotéza na validáciu.

## 7. Mobile UI (artboardy „Mobil – Prehľad", „Mobil – Pozície a ovládanie")

Samostatný návrh pre ovládanie jednou rukou: spodná navigácia (Prehľad, Pozície, AI, Správy, Ovládanie), hero karta kapitálu s denným/celkovým P/L a equity sparkline, čas poslednej synchronizácie (žiadne fiktívne live dáta pri offline), kompaktné karty pozícií s LONG/SHORT štítkami, SL–vstup–TP range bar, ovládanie Spustiť/Pauza/EMERGENCY STOP s vysvetlením správania a potvrdzovaním príkazov Windows aplikáciou, AI analýza a news feed.

## 8. Ďalší krok

Po schválení konceptu: FÁZA 3 – základ Windows aplikácie (engine proces, lokálna databáza, Market Data Engine, system monitoring). Dizajnové tokeny sú strojovo čitateľné v `design/tokens.json`.
