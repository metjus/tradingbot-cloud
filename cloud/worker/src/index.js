/**
 * ARGUS sync Worker: state mirror, command queue, notifications, Web Push.
 *
 * Security model:
 *  - the Windows engine authenticates with ENGINE_TOKEN (outbound only)
 *  - the mobile dashboard authenticates with DASHBOARD_TOKEN (device pairing
 *    v1: a shared secret the user enters once in the PWA)
 *  - the Worker never holds broker API keys and never executes trades;
 *    commands are queued and the engine confirms execution results
 *  - Web Push: pushes are sent WITHOUT payload (no RFC8291 encryption
 *    needed); the service worker fetches the latest notification on wake.
 */

const JSON_HEADERS = {
  "content-type": "application/json",
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, content-type",
  "access-control-allow-methods": "GET, POST, OPTIONS",
};

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: JSON_HEADERS });

function bearer(request) {
  const h = request.headers.get("authorization") || "";
  return h.startsWith("Bearer ") ? h.slice(7) : "";
}

function timingSafeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let out = 0;
  for (let i = 0; i < a.length; i++) out |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return out === 0;
}

// ---------------------------------------------------------------- VAPID ---
const b64u = (buf) =>
  btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function vapidHeaders(env, endpoint) {
  if (!env.VAPID_PRIVATE_JWK || !env.VAPID_PUBLIC_KEY) return null;
  const { origin } = new URL(endpoint);
  const key = await crypto.subtle.importKey(
    "jwk", JSON.parse(env.VAPID_PRIVATE_JWK),
    { name: "ECDSA", namedCurve: "P-256" }, false, ["sign"]);
  const header = b64u(new TextEncoder().encode(JSON.stringify({ typ: "JWT", alg: "ES256" })));
  const payload = b64u(new TextEncoder().encode(JSON.stringify({
    aud: origin,
    exp: Math.floor(Date.now() / 1000) + 12 * 3600,
    sub: env.VAPID_SUBJECT || "mailto:admin@example.com",
  })));
  const sig = await crypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" }, key,
    new TextEncoder().encode(`${header}.${payload}`));
  return {
    Authorization: `vapid t=${header}.${payload}.${b64u(sig)}, k=${env.VAPID_PUBLIC_KEY}`,
    TTL: "120",
  };
}

async function sendPushes(env, category) {
  const subs = await env.DB.prepare("SELECT endpoint, json, categories FROM push_subscriptions")
    .all();
  for (const row of subs.results || []) {
    try {
      const cats = row.categories ? JSON.parse(row.categories) : null;
      if (cats && !cats.includes(category)) continue;
      const headers = await vapidHeaders(env, row.endpoint);
      if (!headers) return; // push not configured
      const res = await fetch(row.endpoint, { method: "POST", headers });
      if (res.status === 404 || res.status === 410) {
        await env.DB.prepare("DELETE FROM push_subscriptions WHERE endpoint = ?")
          .bind(row.endpoint).run();
      }
    } catch (e) { /* one bad subscription must not break the rest */ }
  }
}

// ----------------------------------------------------------------- app ---
export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") return new Response(null, { headers: JSON_HEADERS });
    const url = new URL(request.url);
    const p = url.pathname;
    const tok = bearer(request);
    const isEngine = timingSafeEqual(tok, env.ENGINE_TOKEN || "");
    const isDash = timingSafeEqual(tok, env.DASHBOARD_TOKEN || "");

    try {
      // ---- engine endpoints ----
      if (p === "/ingest/state" && request.method === "POST") {
        if (!isEngine) return json({ error: "unauthorized" }, 401);
        const body = await request.text();
        if (body.length > 512 * 1024) return json({ error: "too large" }, 413);
        await env.DB.prepare(
          "INSERT INTO state (id, ts, json) VALUES (1, ?, ?) " +
          "ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, json = excluded.json")
          .bind(Math.floor(Date.now() / 1000), body).run();
        return json({ ok: true });
      }

      if (p === "/ingest/notification" && request.method === "POST") {
        if (!isEngine) return json({ error: "unauthorized" }, 401);
        const n = await request.json();
        await env.DB.prepare(
          "INSERT INTO notifications (ts, category, title, body, data) VALUES (?,?,?,?,?)")
          .bind(n.ts || Math.floor(Date.now() / 1000), String(n.category || "info"),
                String(n.title || "").slice(0, 200), String(n.body || "").slice(0, 500),
                JSON.stringify(n.data || {})).run();
        await env.DB.prepare(
          "DELETE FROM notifications WHERE id NOT IN " +
          "(SELECT id FROM notifications ORDER BY id DESC LIMIT 200)").run();
        await sendPushes(env, String(n.category || "info"));
        return json({ ok: true });
      }

      if (p === "/commands/pending" && request.method === "GET") {
        if (!isEngine) return json({ error: "unauthorized" }, 401);
        const rows = await env.DB.prepare(
          "SELECT id, action, ts FROM commands WHERE status = 'pending' ORDER BY ts LIMIT 10")
          .all();
        return json({ commands: rows.results || [] });
      }

      if (p === "/commands/ack" && request.method === "POST") {
        if (!isEngine) return json({ error: "unauthorized" }, 401);
        const b = await request.json();
        await env.DB.prepare(
          "UPDATE commands SET status = 'done', result = ?, acked_ts = ? WHERE id = ?")
          .bind(JSON.stringify(b.result || {}), b.ts || Math.floor(Date.now() / 1000),
                String(b.id)).run();
        return json({ ok: true });
      }

      // ---- dashboard endpoints ----
      if (p === "/api/state" && request.method === "GET") {
        if (!isDash) return json({ error: "unauthorized" }, 401);
        const row = await env.DB.prepare("SELECT ts, json FROM state WHERE id = 1").first();
        const notif = await env.DB.prepare(
          "SELECT ts, category, title, body FROM notifications ORDER BY id DESC LIMIT 20").all();
        return json({
          synced_ts: row ? row.ts : null,
          stale_s: row ? Math.max(0, Math.floor(Date.now() / 1000) - row.ts) : null,
          state: row ? JSON.parse(row.json) : null,
          notifications: notif.results || [],
        });
      }

      if (p === "/api/command" && request.method === "POST") {
        if (!isDash) return json({ error: "unauthorized" }, 401);
        const b = await request.json();
        const action = String(b.action || "");
        if (!["start", "pause", "emergency_stop"].includes(action))
          return json({ error: "unknown action" }, 400);
        const id = crypto.randomUUID();
        await env.DB.prepare(
          "INSERT INTO commands (id, action, ts, status) VALUES (?,?,?, 'pending')")
          .bind(id, action, Math.floor(Date.now() / 1000)).run();
        return json({ ok: true, id });
      }

      if (p.startsWith("/api/command/") && request.method === "GET") {
        if (!isDash) return json({ error: "unauthorized" }, 401);
        const row = await env.DB.prepare(
          "SELECT id, action, status, result, ts, acked_ts FROM commands WHERE id = ?")
          .bind(p.split("/").pop()).first();
        if (!row) return json({ error: "not found" }, 404);
        return json({ ...row, result: row.result ? JSON.parse(row.result) : null });
      }

      if (p === "/api/push/key" && request.method === "GET") {
        if (!isDash) return json({ error: "unauthorized" }, 401);
        return json({ key: env.VAPID_PUBLIC_KEY || null });
      }

      if (p === "/api/push/subscribe" && request.method === "POST") {
        if (!isDash) return json({ error: "unauthorized" }, 401);
        const b = await request.json();
        const sub = b.subscription || {};
        if (!sub.endpoint) return json({ error: "missing endpoint" }, 400);
        await env.DB.prepare(
          "INSERT INTO push_subscriptions (endpoint, json, categories, created_ts) " +
          "VALUES (?,?,?,?) ON CONFLICT(endpoint) DO UPDATE SET json = excluded.json, " +
          "categories = excluded.categories")
          .bind(sub.endpoint, JSON.stringify(sub),
                b.categories ? JSON.stringify(b.categories) : null,
                Math.floor(Date.now() / 1000)).run();
        return json({ ok: true });
      }

      if (p === "/api/notifications/latest" && request.method === "GET") {
        if (!isDash) return json({ error: "unauthorized" }, 401);
        const row = await env.DB.prepare(
          "SELECT ts, category, title, body FROM notifications ORDER BY id DESC LIMIT 1").first();
        return json({ notification: row || null });
      }

      return json({ error: "not found" }, 404);
    } catch (e) {
      return json({ error: "internal", detail: String(e && e.message || e) }, 500);
    }
  },
};
