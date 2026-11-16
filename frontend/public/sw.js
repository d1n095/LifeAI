// MainAI service worker — deliberately minimal and security-first.
//
// The whole point of this SW is installability + fast repeat loads of the *public, immutable*
// app shell. It must NEVER cache anything that could carry Founder data or credentials:
//   * /api/*         -> never touched (session cookies are HttpOnly; responses are per-user).
//   * non-GET        -> never touched.
//   * cross-origin   -> never touched.
//   * HTML documents / navigations -> network-only (authed state is client-rendered; we do not
//                       want a stale or another-session view served from cache).
// Only hashed, immutable build assets (/_next/static/*), the icons, and the manifest are
// cached (cache-first). Those are public and content-addressed, so caching them is safe.
//
// See frontend/docs/AUTH_THREAT_MODEL.md (session security) — nothing here weakens it.

const STATIC_CACHE = "mainai-static-v1";

function isCacheableStatic(url) {
  return (
    url.pathname.startsWith("/_next/static/") ||
    url.pathname.startsWith("/icons/") ||
    url.pathname === "/manifest.webmanifest"
  );
}

self.addEventListener("install", () => {
  // Activate immediately; we precache nothing (hashed asset names aren't known here).
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    (async () => {
      // Drop caches from older SW versions.
      const keys = await caches.keys();
      await Promise.all(keys.filter((k) => k !== STATIC_CACHE).map((k) => caches.delete(k)));
      await self.clients.claim();
    })()
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  const url = new URL(req.url);

  // Only ever consider same-origin GETs. Everything else goes straight to the network and is
  // never read from or written to the cache.
  if (req.method !== "GET" || url.origin !== self.location.origin) return;

  // Hard exclusion: API / auth traffic is never cached.
  if (url.pathname.startsWith("/api/")) return;

  // Public immutable assets: cache-first, populate on first hit.
  if (isCacheableStatic(url)) {
    event.respondWith(
      (async () => {
        const cache = await caches.open(STATIC_CACHE);
        const hit = await cache.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        // Only cache successful, basic (same-origin) responses.
        if (res && res.ok && res.type === "basic") cache.put(req, res.clone());
        return res;
      })()
    );
    return;
  }

  // Navigations and any other GET (HTML, data): network-only — do not cache authed views.
  // (Falls through: no respondWith => default browser network handling.)
});
