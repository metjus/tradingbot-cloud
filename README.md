# Trading Bot Cloud v0.4.4 — Cloudflare Workers + D1 + GitHub

**Výhradne simulácia, bez objednávok u brokera a bez reálnych peňazí.** Existujú dva oddelené účty: syntetický SIM účet 500 EUR a USD paper účet 500 USD, ktorý používa overené ceny z Twelve Data na virtuálne obchody. Trading 212 Demo integrácia slúži iba na ručné čítanie stavu účtu a nie je pripojená k obchodnému jadru. Cloudflare Cron beží približne raz za minútu pre syntetický režim a každých päť minút skúša trhový feed.

## Funkcie

- Prístup cez heslo; HTTP-only Secure SameSite cookie platná 12 hodín, POST vyžaduje rovnaký Origin.
- Cloudflare D1 uchováva účet, otvorené pozície, syntetické ceny, obchody a logy cez nasadenia/reštarty.
- GitHub Actions: testy → inicializácia D1 tabuľky → nasadenie Worker pri pushi na `main`.
- Worker `workers.dev`, dostupný na desktopoch aj mobile, bez zapnutého PC.
- Pauza: okamžite blokuje nové vstupy, existujúce obchody zostávajú do TP/SL (môže to trvať dlho).
- Núdzové STOP: okamžite uzavrie všetky **syntetické** pozície a zablokuje reštart do Resetu.
- Kapitál 500 €; maximálne 2 otvorené pozície; 25 % počiatočného kapitálu na pozíciu; modelové riziko 0,5 %/pozíciu; denný spúšťač 2 % blokuje nové vstupy, neobmedzuje skutočnú maximálnu stratu.
- Farebné P/L, investovaný kapitál, história obchodov, graf hodnoty účtu.

## Jednorazové nastavenie

### 1. GitHub

Vytvor **súkromný repozitár** napr. `tradingbot-cloud` na GitHub. Nahraj do jeho koreňa celý obsah tohto ZIP (vrátane `.github/workflows/deploy.yml`). Ak nemáš GitHub CLI, nahraj súbory cez GitHub webové rozhranie alebo Git.

### 2. Cloudflare a D1

Nainštaluj lokálne Node.js 22+ a v rozbalenom priečinku spusti:

```sh
npm install
npx wrangler login
npx wrangler d1 create tradingbot-paper-db
```

Posledný príkaz vypíše `database_id` (UUID). Nahraď `REPLACE_WITH_YOUR_D1_DATABASE_UUID` vo `wrangler.toml` a commitni túto zmenu na GitHub.

### 3. Heslo aplikácie

Nastav náhodné **unikátne heslo aspoň 16 znakov**, neukladaj ho do GitHubu ani do súborov ZIP:

```sh
npx wrangler secret put DASHBOARD_PASSWORD
```

**Poznámka:** `secret put` môže vytvoriť Worker už pred prvým GitHub deployom; vyžaduje správnu autorizáciu Cloudflare. Po nasadení over, že secret existuje vo Worker Settings → Variables and Secrets. Ak pri prvom nastavovaní ešte Worker neexistuje, urob najprv jednorazový deploy a následne nastav secret.

Odporúčané: navyše aktivuj **Cloudflare Access** a WAF rate-limiting pre `/api/login` (táto ukážková aplikácia nemá vlastný distribuovaný rate limiter). Nepoužívaj pre reálne peniaze bez ďalšieho bezpečnostného auditu.

### 4. GitHub Actions CI/CD

V GitHub repozitári otvor **Settings → Secrets and variables → Actions** a vlož dva repozitárové secrets:

- `CLOUDFLARE_ACCOUNT_ID` — account ID z Cloudflare dashboardu.
- `CLOUDFLARE_API_TOKEN` — Cloudflare API token s minimálnymi právami na úpravu Workers a D1 pre tento účet.

Workflow `.github/workflows/deploy.yml` sa spustí pri každom pushi do `main`. Prvý deploy vytvorí Worker `tradingbot-cloud-paper`; URL bude približne `https://tradingbot-cloud-paper.<tvoj-workers-subdomain>.workers.dev` (skutočný názov závisí od účtu). URL zistíš z úspešného GitHub Actions logu alebo Cloudflare dashboardu.

Ak chceš prvé nasadenie urobiť lokálne, spusti:

```sh
npx wrangler d1 execute tradingbot-paper-db --remote --file=./schema.sql
npx wrangler deploy
npx wrangler secret put DASHBOARD_PASSWORD
```

D1 tabuľka je inicializovaná automaticky aj cez workflow. Aplikácia si vie tabuľku vytvoriť aj pri prvom požiadavku, ak má D1 binding.

### 5. Prvé prihlásenie

Otvor workers.dev URL a zadaj heslo z kroku 3. Po prihlásení klikni na **Spustiť**. Cron beží približne raz za minútu a zapisuje do D1. Pri vypnutom PC pokračuje.

## Čo sledovať pri testovaní

1. **Aktualizácie:** po zmene v GitHub `main` sa má po zelenom GitHub Actions jobe zmeniť aplikácia na tej istej URL; účet v D1 sa nemá resetovať.
2. **Pauza:** pri otvorených pozíciách stav `ČAKÁ NA UZAVRETIE`; počet pozícií nesmie stúpať. Keď sa uzavrú cez TP/SL, stav `PAUZA`.
3. **STOP:** po stlačení `Núdzové STOP` je počet otvorených pozícií 0; bez `Resetu` nemožno obnoviť obchodovanie.
4. **Kapitál:** hotovosť nesmie klesnúť pod nulu; max. 2 pozície; každá vstupná alokácia max. 125 € pri kapitále 500 €.
5. **P/L:** zelená = zisk, červená = strata, 0 = neutrálna; hodnota účtu = voľná hotovosť + investované.
6. **Persistence:** po odhlásení, obnovení karty alebo nasadení ďalšej verzie zostávajú pozície, história aj kapitál.
7. **Cron:** sleduj `Tick` a `Posledný zápis`; v aktívnom stave sa majú meniť približne každú minútu. Worker cron nie je presný časovač.
8. **Mobil:** web má byť čitateľný aj na iPhone; nikto bez hesla nemá vidieť stav účtu.

## Bezpečnostné limity a obmedzenia

- Skutočné reálne obchodovanie, short selling, OpenAI API ani live market feed **nie sú implementované**.
- Ak cloudové cron udalosti prestanú bežať, syntetické stop-lossy sa nevyhodnocujú až do ďalšej udalosti. Tento prototyp preto **nie je vhodný na reálne obchody**.
- Hodnoty stop-loss sú iba cieľové úrovne, nie garantovaná cena uzavretia.
- Samotný denný limit straty po prekročení len blokuje nové nákupy; môže sa zvýšiť strata na otvorených pozíciách.
- Simulátor nemodeluje spread, burzové poplatky, slippage ani výpadky burzy.
- D1 optimistic concurrency používajú update s `revision`, aby paralelné požiadavky neprepísali navzájom stav.
- Cron 1 min zodpovedá hodinovým/denným testom aplikácie; nie je to platforma na sub-second scalping.
- Pre reálne nasadenie budú potrebné robustnejšie prístupové kontroly a plnohodnotný audit limitov, trhových dát a objednávok.

## v0.3.0 – read-only real market data
- Synthetic paper trading is intentionally unchanged (SIM-ALPHA / BETA / GAMMA).
- AAPL, MSFT, NVDA quote snapshots are fetched from Twelve Data only during the regular New York weekday window, approximately once every 5 minutes.
- US holidays, halts and provider delay are detected by quote age when possible; the session schedule itself does not include a US holidays calendar.
- These stocks are denominated in USD and are NOT used by the EUR paper engine. No live orders are possible.
- The quote panel reports source timestamps and marks stale data.
- Set the `TWELVEDATA_API_KEY` secret in Cloudflare Worker -> Settings -> Variables and Secrets -> Add -> Secret, then save/deploy. Never put the key in GitHub files or send it in chat.
- Data may be absent outside US regular market hours, when the free API limit is hit, or until provider API key is added.
- Twelve Data free-tier credits are limited. The current watchlist uses up to 3 requests per 5 minutes during the nominal US session (234 requests per 6.5-hour trading day).
- Synthetic strategy stats are calculated only from existing paper trades and are **not** real-market performance.
- Worker scheduled jobs are best effort; quote freshness is always assessed independently.

## v0.3.2 – equity chart and profit factor
- Added three local-time chart windows: 1H, 24H, 7D.
- Time-stamped equity samples are recorded every second active synthetic tick and retained up to 6,000 samples (~8 days at exactly 1 tick per minute). Time ranges use timestamps, not estimated tick intervals.
- Old equity points from earlier versions lack timestamps and deliberately do not appear in time-filtered views. Existing trades, balances, and previous paper equity samples are not deleted.
- Touch or click the SVG chart to inspect the closest sample timestamp and exact displayed equity.
- P/L chart line becomes green/red based on the first and last visible recorded equity, not the entire lifetime P/L.
- Profit Factor reads "Bez strát" if there are winning trades but no losing trades; this is not a numeric profit factor.
- If the worker is paused, no new snapshots are created; 1H charts can legitimately become empty.
- Simulation remains entirely synthetic; no Trading 212 orders, AI decisions, or real-market execution.

## v0.3.3 – market-data verification (read-only)
- No change to synthetic paper trading or broker connectivity.
- The Twelve Data market-watch panel exposes feed state, count of attempted/successful checks, source timestamps, and per-symbol errors.
- A quote is considered recent only if the provider explicitly supplies `last_quote_at`; daily `timestamp` alone is often a bar-open timestamp and must not be mistaken for a live trade.
- Trading-hours gate uses NY weekdays 09:30–16:00 ET, but does not yet implement the official exchange holiday and halt calendar.
- An unverified feed always displays a warning. Market-watch status cannot launch trades.
- The Twelve Data token stays server-side in Cloudflare Secrets and is sent in an Authorization header, never in the URL.
- Check the dashboard during a regular U.S. session and inspect provider error messages before adding a real-price paper engine.

## v0.4.0 – USD market-price paper preview (manual, safe by default)

The existing synthetic-paper account and its balances/trades are unchanged.
The new market-paper preview uses a separate D1 table and a separate **500 USD** test balance. It is not a EUR conversion or a Trading 212 account.

- Authenticated dashboard: **Overiť nový snapshot**. Manual only; no automatic orders or entries.
- Rejects missing, stale, incomplete, non-USD, non-session, and duplicate price snapshots. A successful snapshot emits HOLD-only diagnostics.
- Records snapshot timestamps and retains isolated state with revision checks.
- Risk and cost assumptions are explicit (spread 10 bps, simulated slippage 5 bps, fees 0 USD). They are placeholders, not assertions about broker execution.
- Contains groundwork for max-position and capital checks, but no new-position strategy is enabled. **Do not interpret the preview as a working trading strategy or a profit backtest.**
- No Trading 212 orders, real positions or real-money operations.

Monitor the GitHub Actions test/deploy result and check the new panel in the mobile dashboard. The quote provider, official market holidays, FX, fee schedules, and realistic slippage all require further validation before paper execution is enabled.

## v0.4.1 – opt-in market-price virtual trading
- Separate USD account remains independent of the original SIM account and any Trading 212 credentials.
- New USD-only controls: **Spustiť USD paper**, **Pauza USD**, **Núdzové STOP USD**. All apply only to the USD account; automatic entry is disabled until Start.
- Every successful 5-minute market refresh is eligible for one engine step. Three consecutively increasing price samples totaling at least 0.25% generate a BUY candidate; this is an unvalidated experimental rule, not an investment recommendation.
- No duplicate symbols, at most 2 positions, maximum 25% initial capital per position, 0.5% initial capital estimated risk per trade. Entries are blocked during the final 20 minutes of regular New York trading.
- Positions close based on verified quotes at stop loss (0.8%), take profit (0.6%), or 15 minutes. Prices can gap across limits; the engine does **not** guarantee stop executions at the threshold.
- Simulated costs: 10 bps total spread, 5 bps slippage per side, $0 additional fee. This is an illustrative model, **not** a verified broker price/fee schedule.
- Pause blocks new entries immediately and allows existing virtual positions to close on verified future quotes; STOP also locks new trading but cannot close a virtual position until the next valid quote. Never treat this as immediate broker liquidation.
- Open orders are never sent to Trading 212 or any financial service. AI is not used. Broker Demo remains read-only.
- The New York weekday/hour session check does not yet handle exchange holidays, early closes, quote licensing, corporate actions or benchmark calibration. Avoid real money.

## Audit v0.4.4 — kontrola pred ďalším testovaním

- UI, Worker, package a simulátor majú zjednotenú verziu. Force Update skúša načítať aplikáciu cez nový URL parameter a verzované `/client.js`.
- GitHub Actions nasadenia používajú jednu concurrency skupinu. Staršie push-nasadenie nemá prepisovať novší commit.
- Nový USD paper režim odmieta opakovane použité provider timestamps a prerušuje momentum históriu po veľkých dátových výpadkoch alebo na začiatku nového UTC dňa.
- Prehrávanie snapshotu či neúplné alebo zastarané cenové údaje nesmie vytvoriť nové obchody.
- Inicializačný SQL skript obsahuje všetky tri aktuálne D1 tabuľky; ich existujúce údaje sa nemenia.
- `npm test` musí prejsť pred `wrangler deploy`. Úspešný commit neznamená úspešný deploy, skontroluj GitHub Actions.
- Nevyžaduje sa žiadny reset existujúceho účtu.

**Zostávajúce obmedzenia:** Víkendy a bežná americká seansa sa kontrolujú, ale sviatky/early close/halts nie; chýba nezávislé overenie kvality burzového feedu, realistický order book, menový kurz a plný model poplatkov; demo konektor nebol overený s reálnymi prístupovými údajmi. Cloudflare prihlásenie potrebuje dodatočné WAF/rate-limit zabezpečenie pred použitím na peniaze. Pauza v USD režime vyžaduje ďalší overený trhový snapshot na uzatváranie a STOP nie je okamžitá likvidácia. Nič z toho nie je pripravené na live Trading 212 obchodovanie.

## v0.4.4 — cost-aware paper order checks and instrument scope
- Before every USD **paper** BUY, the engine checks net projected TP and SL after modeled entry/exit spread, slippage, and fees. If the profit target cannot cover modeled costs, reject the entry.
- For every USD **paper** exit, check modeled net realized P/L. Discretionary profit taking is rejected if net P/L is nonpositive; protective STOP_LOSS, TIME_EXIT, and EMERGENCY_EXIT must remain allowed even at a loss.
- The assessment displays target net P/L, stop net P/L, and mathematical break-even win rate on the USD mobile panel.
- The assessment explicitly reports `validatedEdge:false`, `expectedNetProfitUsd:null`, and `liveAllowed:false`. The current three-point momentum signal has **no demonstrated positive expectancy**. A profitable target is not evidence of a profitable strategy. The modeled values are not a guaranteed execution price.
- **Nothing is connected to a broker order submission endpoint**. Live orders stay disabled. USD paper account and existing trade histories are preserved.
- Instrument scope for exploratory paper phase: AAPL, MSFT, NVDA only. Evaluate adding liquid UCITS equity ETFs and a physical-gold ETC later, after fetching available symbols using the Trading 212 Demo `/equity/metadata/instruments` endpoint and checking currency, venue, liquidity and costs. Direct commodity futures are unavailable through the Invest Public API.
- Before live integration, add independent strategy validation (out-of-sample, fees, rejected/slipped fills), broker-state reconciliation, pending-order deduplication, allowed-instrument whitelist and separate operator confirmation.
