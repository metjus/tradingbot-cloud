/**
 * ARGUS sync Worker: serves the mobile PWA, mirrors engine state, queues
 * commands, stores notifications and sends Web Push.
 *
 * Pairing (no secrets to copy by hand):
 *   1. the desktop app calls POST /setup/claim once (optionally with the
 *      PAIRING_CODE Worker secret, if the owner configured one)
 *   2. the Worker generates an engine token, a dashboard token and a VAPID
 *      key pair, stores only SHA-256 hashes of the tokens + the VAPID keys
 *      in D1, and returns the tokens to the desktop app (once)
 *   3. the desktop app shows a QR/link with the dashboard token for the phone
 *   A Worker that is already paired refuses further claims (409); the
 *   paired engine can reset (POST /setup/reset) or rotate the phone token.
 *
 * Security model: the Worker never holds broker keys and never trades;
 * commands are queued and the engine reports the result back.
 * Web Push is sent WITHOUT payload; the service worker fetches the latest
 * notification itself (no RFC 8291 payload encryption needed).
 */

const JSON_HEADERS = {
  "content-type": "application/json",
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, content-type, x-argus-device, x-argus-mode",
  "access-control-allow-methods": "GET, POST, OPTIONS",
  "cache-control": "no-store",
};
const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: JSON_HEADERS });
const now = () => Math.floor(Date.now() / 1000);

const b64u = (buf) =>
  btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

function randomToken(bytes = 32) {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return [...a].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function sha256hex(s) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(s));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
}

function bearer(request) {
  const h = request.headers.get("authorization") || "";
  return h.startsWith("Bearer ") ? h.slice(7) : "";
}

// ------------------------------------------------------------ config ---
async function cfgGet(env, key) {
  const r = await env.DB.prepare("SELECT value FROM config WHERE key = ?").bind(key).first();
  return r ? r.value : null;
}
async function cfgSet(env, key, value) {
  await env.DB.prepare("INSERT INTO config (key, value) VALUES (?, ?) " +
    "ON CONFLICT(key) DO UPDATE SET value = excluded.value").bind(key, value).run();
}

async function role(request, env) {
  const tok = bearer(request);
  if (!tok) return null;
  const h = await sha256hex(tok);
  const eng = await cfgGet(env, "engine_token_hash");
  if (eng && safeEqual(h, eng)) return "engine";
  const dash = await cfgGet(env, "dashboard_token_hash");
  if (dash && safeEqual(h, dash)) return "dashboard";
  // manual setup via Worker secrets is still supported
  if (env.ENGINE_TOKEN && safeEqual(tok, env.ENGINE_TOKEN)) return "engine";
  if (env.DASHBOARD_TOKEN && safeEqual(tok, env.DASHBOARD_TOKEN)) return "dashboard";
  return null;
}

// ------------------------------------------------------------ phones ---
// Remembers which phones use the dashboard token so the desktop app can
// confirm "phone connected". Only a coarse device type is stored.
function deviceName(request) {
  const ua = request.headers.get("user-agent") || "";
  let name = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad"
    : /Android/.test(ua) ? "Android" : /Macintosh|Windows|Linux/.test(ua) ? "Počítač" : "Zariadenie";
  const mode = request.headers.get("x-argus-mode");
  if (mode === "app") name += " · appka";
  else if (mode === "web") name += " · prehliadač";
  return name;
}

async function getPhones(env) {
  try { return JSON.parse((await cfgGet(env, "phones")) || "{}"); } catch (e) { return {}; }
}

async function touchPhone(request, env) {
  const id = String(request.headers.get("x-argus-device") || "").replace(/[^a-zA-Z0-9-]/g, "").slice(0, 40);
  if (!id) return;
  const phones = await getPhones(env);
  const t = now();
  const cur = phones[id];
  const name = deviceName(request);
  if (cur && t - cur.last < 60 && cur.device === name) return;   // limit D1 writes
  phones[id] = { device: name, first: cur ? cur.first : t, last: t };
  const ids = Object.keys(phones).sort((a, b) => phones[b].last - phones[a].last).slice(0, 10);
  await cfgSet(env, "phones", JSON.stringify(Object.fromEntries(ids.map((k) => [k, phones[k]]))));
}

async function phoneList(env) {
  const phones = await getPhones(env);
  return Object.values(phones)
    .sort((a, b) => b.last - a.last)
    .map((p) => ({ device: p.device, first_seen: p.first, last_seen: p.last }));
}

// ------------------------------------------------------------- VAPID ---
async function vapidHeaders(env, endpoint) {
  const jwk = await cfgGet(env, "vapid_private_jwk");
  const pub = await cfgGet(env, "vapid_public_key");
  const sub = (await cfgGet(env, "vapid_subject")) || "mailto:argus@example.com";
  if (!jwk || !pub) return null;
  const { origin } = new URL(endpoint);
  const key = await crypto.subtle.importKey("jwk", JSON.parse(jwk),
    { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const header = b64u(new TextEncoder().encode(JSON.stringify({ typ: "JWT", alg: "ES256" })));
  const payload = b64u(new TextEncoder().encode(JSON.stringify({
    aud: origin, exp: now() + 12 * 3600, sub })));
  const sig = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key,
    new TextEncoder().encode(`${header}.${payload}`));
  return { Authorization: `vapid t=${header}.${payload}.${b64u(sig)}, k=${pub}`, TTL: "300" };
}

async function sendPushes(env, category) {
  const subs = await env.DB.prepare(
    "SELECT endpoint, categories FROM push_subscriptions").all();
  for (const row of subs.results || []) {
    try {
      const cats = row.categories ? JSON.parse(row.categories) : null;
      if (cats && !cats.includes(category)) continue;
      const headers = await vapidHeaders(env, row.endpoint);
      if (!headers) return;
      const res = await fetch(row.endpoint, { method: "POST", headers });
      if (res.status === 404 || res.status === 410) {
        await env.DB.prepare("DELETE FROM push_subscriptions WHERE endpoint = ?")
          .bind(row.endpoint).run();
      }
    } catch (e) { /* one bad subscription must not break the rest */ }
  }
}

// ------------------------------------------------------------ candles ---
// Live chart for the phone: candles straight from Kraken Futures' public
// charts API (no load on the desktop app), cached for 20 s per symbol/tf.
const TF_SEC = { "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400 };
async function candles(url) {
  const sym = String(url.searchParams.get("symbol") || "").toUpperCase();
  const tf = String(url.searchParams.get("tf") || "15m");
  const m = /^([A-Z0-9]{1,15})\/USD$/.exec(sym);
  if (!m || !TF_SEC[tf]) return json({ error: "bad symbol or timeframe" }, 400);
  const base = m[1] === "BTC" ? "XBT" : m[1];
  const n = 240;
  const from = Math.floor(Date.now() / 1000) - n * TF_SEC[tf];
  const src = `https://futures.kraken.com/api/charts/v1/trade/PF_${base}USD/${tf}?from=${from}`;
  const cache = typeof caches !== "undefined" ? caches.default : null;
  const key = new Request(`https://argus-cache/candles/${base}/${tf}/${Math.floor(Date.now() / 20000)}`);
  if (cache) { const hit = await cache.match(key); if (hit) return hit; }
  const r = await fetch(src, { headers: { "User-Agent": "argus-sync" } });
  if (!r.ok) return json({ error: "kraken " + r.status }, 502);
  const d = await r.json();
  const rows = (d.candles || []).map((c) => ({
    t: Math.floor(Number(c.time) / 1000), o: +c.open, h: +c.high, l: +c.low, c: +c.close, v: +c.volume }));
  const out = new Response(JSON.stringify({ symbol: sym, tf, candles: rows.slice(-n) }),
    { headers: { ...JSON_HEADERS, "cache-control": "max-age=20" } });
  if (cache) await cache.put(key, out.clone());
  return out;
}

// ------------------------------------------------------------- setup ---
async function claim(request, env) {
  if (await cfgGet(env, "engine_token_hash"))
    return json({ error: "already_paired",
      message: "Tento Worker je už spárovaný. Zruš párovanie v pôvodnej aplikácii " +
               "alebo spusti nasadenie s voľbou reset_pairing." }, 409);
  let body = {};
  try { body = await request.json(); } catch (e) {}
  if (env.PAIRING_CODE && !safeEqual(String(body.pairing_code || ""), env.PAIRING_CODE))
    return json({ error: "bad_pairing_code", message: "Nesprávny párovací kód." }, 403);

  const engineToken = randomToken();
  const dashToken = randomToken(16);
  const kp = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" },
    true, ["sign", "verify"]);
  const privJwk = await crypto.subtle.exportKey("jwk", kp.privateKey);
  const pubRaw = await crypto.subtle.exportKey("raw", kp.publicKey);
  const origin = new URL(request.url).origin;

  await cfgSet(env, "engine_token_hash", await sha256hex(engineToken));
  await cfgSet(env, "dashboard_token_hash", await sha256hex(dashToken));
  await cfgSet(env, "vapid_private_jwk", JSON.stringify(privJwk));
  await cfgSet(env, "vapid_public_key", b64u(pubRaw));
  await cfgSet(env, "vapid_subject", origin);
  await cfgSet(env, "paired_ts", String(now()));
  return json({ ok: true, engine_token: engineToken, dashboard_token: dashToken,
                url: origin });
}

async function resetAll(env) {
  for (const t of ["config", "state", "commands", "notifications", "push_subscriptions"])
    await env.DB.prepare(`DELETE FROM ${t}`).run();
}

// --------------------------------------------------------------- app ---
export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: JSON_HEADERS });
    const url = new URL(request.url);
    const p = url.pathname;

    try {
      // ---- pairing ----
      if (p === "/setup/status" && request.method === "GET")
        return json({ paired: !!(await cfgGet(env, "engine_token_hash")),
                      pairing_code_required: !!env.PAIRING_CODE });
      if (p === "/setup/claim" && request.method === "POST") return claim(request, env);

      const who = await role(request, env);

      if (p === "/setup/reset" && request.method === "POST") {
        if (who !== "engine") return json({ error: "unauthorized" }, 401);
        await resetAll(env);
        return json({ ok: true });
      }
      // short-lived 6-digit PIN so a phone home-screen app can pair without
      // typing the long code (iOS keeps home-screen apps' storage separate)
      if (p === "/setup/pin" && request.method === "POST") {
        if (who !== "engine") return json({ error: "unauthorized" }, 401);
        const b = await request.json();
        const dashToken = String(b.dashboard_token || "");
        if (!dashToken || !safeEqual(await sha256hex(dashToken),
                                     (await cfgGet(env, "dashboard_token_hash")) || ""))
          return json({ error: "bad dashboard token" }, 400);
        const a = new Uint32Array(1); crypto.getRandomValues(a);
        const pin = String(a[0] % 1000000).padStart(6, "0");
        await cfgSet(env, "pair_pin", JSON.stringify(
          { pin_hash: await sha256hex(pin), token: dashToken, exp: now() + 600, attempts: 0 }));
        return json({ ok: true, pin, expires_in: 600 });
      }
      if (p === "/setup/redeem" && request.method === "POST") {
        const raw = await cfgGet(env, "pair_pin");
        if (!raw) return json({ error: "no_pin", message: "Kód neplatí - vygeneruj nový v počítači." }, 404);
        const rec = JSON.parse(raw);
        let b = {}; try { b = await request.json(); } catch (e) {}
        if (now() > rec.exp) {
          await env.DB.prepare("DELETE FROM config WHERE key = 'pair_pin'").run();
          return json({ error: "expired", message: "Kód vypršal - vygeneruj nový v počítači." }, 410);
        }
        if (!safeEqual(await sha256hex(String(b.pin || "")), rec.pin_hash)) {
          rec.attempts += 1;
          if (rec.attempts >= 5) await env.DB.prepare("DELETE FROM config WHERE key = 'pair_pin'").run();
          else await cfgSet(env, "pair_pin", JSON.stringify(rec));
          return json({ error: "bad_pin", message: "Nesprávny kód." }, 403);
        }
        await env.DB.prepare("DELETE FROM config WHERE key = 'pair_pin'").run();
        await cfgSet(env, "pin_redeemed_ts", String(now()));
        return json({ ok: true, dashboard_token: rec.token });
      }
      if (p === "/setup/rotate-dashboard" && request.method === "POST") {
        if (who !== "engine") return json({ error: "unauthorized" }, 401);
        const dashToken = randomToken(16);
        await cfgSet(env, "dashboard_token_hash", await sha256hex(dashToken));
        await cfgSet(env, "phones", "{}");   // old phones are disconnected
        return json({ ok: true, dashboard_token: dashToken });
      }

      // ---- engine endpoints ----
      if (p === "/ingest/state" && request.method === "POST") {
        if (who !== "engine") return json({ error: "unauthorized" }, 401);
        const body = await request.text();
        if (body.length > 512 * 1024) return json({ error: "too large" }, 413);
        await env.DB.prepare(
          "INSERT INTO state (id, ts, json) VALUES (1, ?, ?) " +
          "ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, json = excluded.json")
          .bind(now(), body).run();
        // tell the desktop app which phones are connected (pairing confirmation)
        return json({ ok: true, server_ts: now(), phones: await phoneList(env),
                      pin_redeemed_ts: Number((await cfgGet(env, "pin_redeemed_ts")) || 0) });
      }
      if (p === "/ingest/notification" && request.method === "POST") {
        if (who !== "engine") return json({ error: "unauthorized" }, 401);
        const n = await request.json();
        await env.DB.prepare(
          "INSERT INTO notifications (ts, category, title, body, data) VALUES (?,?,?,?,?)")
          .bind(n.ts || now(), String(n.category || "info"),
                String(n.title || "").slice(0, 200), String(n.body || "").slice(0, 500),
                JSON.stringify(n.data || {})).run();
        await env.DB.prepare(
          "DELETE FROM notifications WHERE id NOT IN " +
          "(SELECT id FROM notifications ORDER BY id DESC LIMIT 200)").run();
        await sendPushes(env, String(n.category || "info"));
        return json({ ok: true });
      }
      if (p === "/commands/pending" && request.method === "GET") {
        if (who !== "engine") return json({ error: "unauthorized" }, 401);
        const rows = await env.DB.prepare(
          "SELECT id, action, target, ts FROM commands WHERE status = 'pending' ORDER BY ts LIMIT 10")
          .all();
        return json({ commands: rows.results || [] });
      }
      if (p === "/commands/ack" && request.method === "POST") {
        if (who !== "engine") return json({ error: "unauthorized" }, 401);
        const b = await request.json();
        await env.DB.prepare(
          "UPDATE commands SET status = 'done', result = ?, acked_ts = ? WHERE id = ?")
          .bind(JSON.stringify(b.result || {}), b.ts || now(), String(b.id)).run();
        return json({ ok: true });
      }

      // ---- dashboard endpoints ----
      if (p === "/api/state" && request.method === "GET") {
        if (who !== "dashboard") return json({ error: "unauthorized" }, 401);
        await touchPhone(request, env);
        const row = await env.DB.prepare("SELECT ts, json FROM state WHERE id = 1").first();
        const notif = await env.DB.prepare(
          "SELECT ts, category, title, body FROM notifications ORDER BY id DESC LIMIT 20").all();
        return json({
          synced_ts: row ? row.ts : null,
          stale_s: row ? Math.max(0, now() - row.ts) : null,
          state: row ? JSON.parse(row.json) : null,
          notifications: notif.results || [],
        });
      }
      if (p === "/api/candles" && request.method === "GET") {
        if (who !== "dashboard") return json({ error: "unauthorized" }, 401);
        return candles(url);
      }
      if (p === "/api/command" && request.method === "POST") {
        if (who !== "dashboard") return json({ error: "unauthorized" }, 401);
        const b = await request.json();
        const action = String(b.action || "");
        if (!["start", "pause", "emergency_stop", "close_position"].includes(action))
          return json({ error: "unknown action" }, 400);
        const target = action === "close_position" ? String(b.target || "").slice(0, 80) : null;
        if (action === "close_position" && !target) return json({ error: "missing target" }, 400);
        const id = crypto.randomUUID();
        await env.DB.prepare(
          "INSERT INTO commands (id, action, target, ts, status) VALUES (?,?,?,?, 'pending')")
          .bind(id, action, target, now()).run();
        return json({ ok: true, id });
      }
      if (p.startsWith("/api/command/") && request.method === "GET") {
        if (who !== "dashboard") return json({ error: "unauthorized" }, 401);
        const row = await env.DB.prepare(
          "SELECT id, action, target, status, result, ts, acked_ts FROM commands WHERE id = ?")
          .bind(p.split("/").pop()).first();
        if (!row) return json({ error: "not found" }, 404);
        return json({ ...row, result: row.result ? JSON.parse(row.result) : null });
      }
      if (p === "/api/push/key" && request.method === "GET") {
        if (who !== "dashboard") return json({ error: "unauthorized" }, 401);
        return json({ key: await cfgGet(env, "vapid_public_key") });
      }
      if (p === "/api/push/subscribe" && request.method === "POST") {
        if (who !== "dashboard") return json({ error: "unauthorized" }, 401);
        const b = await request.json();
        const sub = b.subscription || {};
        if (!sub.endpoint) return json({ error: "missing endpoint" }, 400);
        await env.DB.prepare(
          "INSERT INTO push_subscriptions (endpoint, json, categories, created_ts) " +
          "VALUES (?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET json = excluded.json, " +
          "categories = excluded.categories")
          .bind(sub.endpoint, JSON.stringify(sub),
                b.categories ? JSON.stringify(b.categories) : null, now()).run();
        return json({ ok: true });
      }
      if (p === "/api/notifications/latest" && request.method === "GET") {
        if (who !== "dashboard") return json({ error: "unauthorized" }, 401);
        const row = await env.DB.prepare(
          "SELECT ts, category, title, body FROM notifications ORDER BY id DESC LIMIT 1").first();
        return json({ notification: row || null });
      }

      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: "internal", detail: String((e && e.message) || e) }, 500);
    }
  },
};
