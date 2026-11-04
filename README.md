# Trading Bot Cloud v0.2.0 — Cloudflare Workers + D1 + GitHub

**Výhradne paper trading.** Nemá napojenie na Trading 212, AI ani reálne trhové dáta. Symboly `SIM-*` sú syntetické. Cron beží približne raz za minútu, nie každú sekundu. Aktuálny lokálny Python bot sa automaticky nemigruje — cloudová verzia používa nový demo účet 500 €.

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
