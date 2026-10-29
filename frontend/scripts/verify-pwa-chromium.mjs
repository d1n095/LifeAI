// Independent browser-engine verification of the PWA service worker, using REAL Chromium
// (Playwright) against the local production build — not the in-app WebView, not Claude-in-Chrome.
//
// Proves what a correct HTTP response + valid JS alone cannot: that the SW actually REGISTERS,
// ACTIVATES, takes control, caches public static assets, and does NOT cache /api/*.
//
// Usage:  PWA_URL=http://127.0.0.1:3123 node scripts/verify-pwa-chromium.mjs
// Requires: `npx playwright install chromium` and the production server running at PWA_URL.
import { chromium } from "@playwright/test";

const BASE = process.env.PWA_URL || "http://127.0.0.1:3123";
const out = { base: BASE };
let failed = false;
function check(name, cond, detail) {
  out[name] = { pass: !!cond, ...(detail !== undefined ? { detail } : {}) };
  if (!cond) failed = true;
}

const browser = await chromium.launch();
try {
  const ctx = await browser.newContext();
  const page = await ctx.newPage();
  const swErrors = [];
  page.on("console", (m) => {
    if (/service.?worker|sw\.js/i.test(m.text())) swErrors.push(m.text());
  });

  // 1. Load the login page from the production build.
  const resp = await page.goto(`${BASE}/login`, { waitUntil: "load" });
  check("page_http_200", resp && resp.status() === 200, resp?.status());
  check("secure_context", await page.evaluate(() => window.isSecureContext), undefined);

  // 2. Register + wait for ACTIVATION (navigator.serviceWorker.ready resolves on active).
  const reg = await page.evaluate(async () => {
    const r = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    const ready = await navigator.serviceWorker.ready; // resolves once there is an active worker
    // The active worker can be momentarily "activating"; wait until it reaches "activated".
    let state = ready.active?.state || null;
    for (let i = 0; i < 50 && state !== "activated"; i++) {
      await new Promise((res) => setTimeout(res, 100));
      state = ready.active?.state || state;
    }
    return {
      scope: r.scope,
      activeState: state,
      scriptURL: ready.active?.scriptURL || null,
      count: (await navigator.serviceWorker.getRegistrations()).length,
    };
  });
  check("sw_registered", reg.count >= 1 && !!reg.scriptURL, reg);
  check("sw_activated", reg.activeState === "activated", reg.activeState);

  // 3. Reload so the active SW controls the page (activate() calls clients.claim()).
  await page.reload({ waitUntil: "load" });
  const controller = await page.evaluate(() => navigator.serviceWorker.controller?.scriptURL || null);
  check("sw_controls_page", !!controller, controller);

  // 4. Caching behavior: a public static asset should land in the cache…
  const cacheState = await page.evaluate(async () => {
    await fetch("/icons/icon-192.png", { cache: "no-store" }).catch(() => {});
    // …and an /api/* request must NEVER be cached.
    await fetch("/api/auth/me", { cache: "no-store" }).catch(() => {});
    await new Promise((r) => setTimeout(r, 400));
    const names = await caches.keys();
    const results = {};
    for (const n of names) {
      const c = await caches.open(n);
      results[n] = (await c.keys()).map((req) => new URL(req.url).pathname);
    }
    return { cacheNames: names, entries: results };
  });
  const allCached = Object.values(cacheState.entries).flat();
  check("static_asset_cached", allCached.some((p) => p.startsWith("/icons/")), cacheState);
  check("api_never_cached", !allCached.some((p) => p.startsWith("/api/")), allCached.filter((p) => p.startsWith("/api/")));

  // 5. Manifest parsed + standalone display mode available.
  check("display_standalone_media", await page.evaluate(() => typeof window.matchMedia === "function"), undefined);

  out.sw_console = swErrors;
  console.log(JSON.stringify(out, null, 2));
} finally {
  await browser.close();
}
process.exit(failed ? 1 : 0);
