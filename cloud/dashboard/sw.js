/* ARGUS service worker: offline shell + push handling.
   Pushes arrive WITHOUT payload; on wake we fetch the latest notification
   from the Worker (config read from IndexedDB) and display it. */

const CACHE = "argus-v5";
const SHELL = ["./", "./index.html", "./manifest.webmanifest", "./icon.svg", "./icon-192.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(self.clients.claim());
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  if (/^\/(api|ingest|setup|commands)\//.test(url.pathname)) return;   // live data: never cache
  e.respondWith(
    fetch(e.request).then((r) => {
      const copy = r.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy));
      return r;
    }).catch(() => caches.match(e.request))
  );
});

function getCfg() {
  return new Promise((resolve) => {
    const req = indexedDB.open("argus", 1);
    req.onupgradeneeded = () => req.result.createObjectStore("kv");
    req.onerror = () => resolve(null);
    req.onsuccess = () => {
      try {
        const tx = req.result.transaction("kv", "readonly");
        const get = tx.objectStore("kv").get("cfg");
        get.onsuccess = () => resolve(get.result || null);
        get.onerror = () => resolve(null);
      } catch (e) { resolve(null); }
    };
  });
}

self.addEventListener("push", (e) => {
  e.waitUntil((async () => {
    let title = "ARGUS", body = "Nová udalosť na účte.";
    const cfg = await getCfg();
    if (cfg && cfg.url && cfg.token) {
      try {
        const r = await fetch(cfg.url + "/api/notifications/latest", {
          headers: { Authorization: "Bearer " + cfg.token } });
        const d = await r.json();
        if (d.notification) { title = d.notification.title; body = d.notification.body || ""; }
      } catch (err) { /* keep generic text */ }
    }
    await self.registration.showNotification(title, {
      body, icon: "./icon-192.png", badge: "./icon-192.png", tag: "argus",
    });
  })());
});

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: "window" }).then((cs) => {
    for (const c of cs) if ("focus" in c) return c.focus();
    return self.clients.openWindow("./");
  }));
});
