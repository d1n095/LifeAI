# PWA service-worker verification — real browser engine (Playwright/Chromium)

Independent, reproducible verification that the service worker actually registers, activates,
controls the page, caches public static assets, and never caches `/api/*` — exercised in **real
Chromium**, not the in-app WebView and not Claude-in-Chrome.

## How to reproduce
```bash
# 1. production build + server (from frontend/)
npm run build
cp -R .next/static .next/standalone/.next/static && cp -R public .next/standalone/public
PORT=3123 HOSTNAME=127.0.0.1 node .next/standalone/server.js &
# 2. chromium + run
npx playwright install chromium
PWA_URL=http://127.0.0.1:3123 node scripts/verify-pwa-chromium.mjs
```

## Observed result (2026-10-29, Chromium via Playwright)
All checks PASS (exit 0):

| Check | Result |
|---|---|
| page_http_200 | PASS (200) |
| secure_context | PASS |
| sw_registered | PASS (scope `/`, scriptURL `/sw.js`, count 1) |
| sw_activated | PASS (state `activated`) |
| sw_controls_page | PASS (controller `/sw.js` after reload) |
| static_asset_cached | PASS (`mainai-static-v1` holds `/_next/static/*` + `/icons/icon-192.png`) |
| api_never_cached | PASS (no `/api/*` in any cache) |
| display_standalone_media | PASS |

## What this proves / does not prove
- **Proves:** the SW code + its serving are correct — registration, activation, page control,
  and the security-critical cache policy (public static cached; `/api/*` never cached) all work
  in a standards-compliant engine. Correct HTTP + valid JS were necessary but not sufficient;
  this closes that gap.
- **Explains the embedded failure:** the same build fails to register only in the in-app
  WebView pane ("unknown error fetching the script"). Since it registers cleanly in Chromium
  with identical bytes/headers/scope, the embedded-pane failure is an environment limitation of
  that WebView, not a code or serving defect.
- **Does NOT prove (separate acceptance test):** a real end-user *install* (Add to Dock /
  Add to Home screen) and standalone launch on physical macOS Chrome/Safari and a physical
  Google Pixel. macOS can be install-tested from `http://localhost`; the Pixel install requires
  the trusted HTTPS subdomain (see `docs/PWA_DEPLOY_READINESS.md`) — no plain-HTTP-over-LAN.
