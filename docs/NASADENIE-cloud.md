# Nasadenie cloudovej vrstvy (Cloudflare) a mobilného dashboardu

Cloud vrstva je zrkadlo stavu + fronta príkazov. Obchoduje sa výlučne lokálne;
pri výpadku cloudu engine beží ďalej a dashboard ukáže poslednú synchronizáciu.

## 1. Worker + D1

```bash
cd cloud/worker
npm i -g wrangler            # ak ešte nie je
wrangler login
wrangler d1 create argus     # skopíruj database_id do wrangler.toml
wrangler d1 execute argus --file=./schema.sql --remote
wrangler secret put ENGINE_TOKEN      # dlhý náhodný reťazec (openssl rand -hex 32)
wrangler secret put DASHBOARD_TOKEN   # iný dlhý náhodný reťazec
wrangler deploy
```

### Push notifikácie (voliteľné, odporúčané)

Vygeneruj VAPID pár (P-256) napr. v Node:

```bash
node -e "crypto.subtle.generateKey({name:'ECDSA',namedCurve:'P-256'},true,['sign','verify']).then(async k=>{const j=await crypto.subtle.exportKey('jwk',k.privateKey);const r=await crypto.subtle.exportKey('raw',k.publicKey);console.log('PRIVATE_JWK:',JSON.stringify(j));console.log('PUBLIC_KEY:',Buffer.from(r).toString('base64url'))})"
wrangler secret put VAPID_PRIVATE_JWK   # celý JSON z PRIVATE_JWK
wrangler secret put VAPID_PUBLIC_KEY    # base64url z PUBLIC_KEY
wrangler secret put VAPID_SUBJECT       # mailto:tvoj@email
```

Push sa posiela bez payloadu; service worker si po zobudení stiahne poslednú
notifikáciu z Workera (žiadne šifrovanie payloadu nie je potrebné).

## 2. Dashboard (Cloudflare Pages)

```bash
cd cloud/dashboard
wrangler pages deploy . --project-name argus-dashboard
```

Na mobile otvor URL Pages, v úvodnej obrazovke zadaj URL Workera
a DASHBOARD_TOKEN. iPhone: najprv Zdieľať → Pridať na plochu (iOS 16.4+),
potom v Ovládaní povoľ notifikácie.

## 3. Windows engine

```bash
set ARGUS_CLOUD_URL=https://argus-sync.<tvoj-subdomain>.workers.dev
set ARGUS_CLOUD_TOKEN=<ENGINE_TOKEN>
python -m argus
```

Engine drží iba odchádzajúce HTTPS spojenia (push stavu ~10 s, poll príkazov
~5 s). API kľúče brokera sa do cloudu nikdy neposielajú.

## Bezpečnostný model

- dva oddelené tokeny: ENGINE_TOKEN (iba engine) a DASHBOARD_TOKEN (iba mobil)
- príkazy majú id + timestamp; engine ich vykonáva idempotentne, staré (>5 min)
  odmietne; výsledok sa potvrdzuje späť (`/commands/ack`) a dashboard ho zobrazí
- Worker nikdy neobchoduje a nedrží API kľúče brokera
