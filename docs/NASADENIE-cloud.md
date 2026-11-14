# Nasadenie cloudovej vrstvy

Tento postup bol nahradený automatickým nasadením a párovaním z aplikácie.

Aktuálny návod: **[MOBIL-NAVOD.md](MOBIL-NAVOD.md)**.

Technické detaily pre vývojára:
- Worker + mobilná PWA: `cloud/worker` (servíruje aj `cloud/dashboard` ako statické súbory)
- Databáza: Cloudflare D1 `argus-db`, schéma `cloud/worker/schema.sql`
- Nasadenie: `.github/workflows/deploy-cloud.yml` (potrebuje secrets
  `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`; voliteľne `ARGUS_PAIRING_CODE`)
- Párovanie: desktop volá `POST /setup/claim`, Worker vygeneruje token pre engine,
  token pre telefón a VAPID kľúče pre notifikácie; ukladá iba hashe tokenov
- Manuálne nasadenie: `cd cloud/worker && npx wrangler d1 create argus-db`,
  doplniť id do `wrangler.toml`, `npx wrangler d1 execute argus-db --remote --file schema.sql`,
  `npx wrangler deploy`
