# Independent security review — MainAI cross-platform app (PWA + Tauri)

Brief for a **separate authorized examiner**. The author of this code (the implementer lane)
does **not** self-certify it. This scopes exactly what to review and the questions to answer
before any deploy. Two independent branches, reviewable separately.

## A. PWA branch `claude/mainai-pwa-installable` (@ commit on this branch)

Files (frontend/, additive — web app behavior otherwise unchanged):
- `frontend/public/sw.js`
- `frontend/app/manifest.ts`
- `frontend/components/PwaController.tsx`
- `frontend/app/layout.tsx` (metadata/viewport + mounts PwaController)
- `frontend/public/icons/*`, `frontend/scripts/*`
- `docs/PWA_STRATO_DNS.md`, `docs/PWA_DEPLOY_READINESS.md`

Must-answer review questions:
1. **Cache safety (highest priority):** Does `sw.js` ever cache anything authenticated or
   sensitive? Verify the fetch handler: non-GET, cross-origin, `/api/*`, and navigations/HTML
   are all bypassed (no `respondWith` / network-only); only `/_next/static`, `/icons`, and the
   manifest are cached, and only for `response.ok && response.type === "basic"`. Confirm no
   path or query that carries Founder data can match `isCacheableStatic`.
2. **No weakening of auth:** The SW and PWA add no token handling; HttpOnly cookies + CSRF
   (`frontend/lib/api.ts`, `frontend/lib/auth.ts`, `backend/app/cookies.py`) are untouched.
   Confirm nothing reads/writes session material in JS or caches.
3. **Scope creep:** diff vs. base `64c07d5b0` touches only the files above + registry. No
   backend, no API, no auth-route changes.
4. **Install UI:** `PwaController` registration is production-only, failure-swallowed (never
   breaks the app), and exposes no privileged capability.
5. **Deploy config:** `PWA_DEPLOY_READINESS.md` introduces no secret and no backend change;
   HTTPS-only for Android install is enforced (no LAN-HTTP shortcut).

## B. Tauri branch `claude/mainai-crossplatform-app-shell` (@ `7de509412`)

Files (desktop/):
- `desktop/src-tauri/src/proxy.rs` — **the security core**
- `desktop/src-tauri/src/lib.rs`, `main.rs`
- `desktop/src-tauri/tauri.conf.json` (CSP), `capabilities/default.json`
- `desktop/scripts/*`, `.github/workflows/mainai-android-apk.yml`

Must-answer review questions:
1. **Session confidentiality:** In `proxy.rs`, confirm HttpOnly cookies live only in the Rust
   `SessionStore` (file 0600), that `Set-Cookie` is stripped from every response returned to
   the webview, and that the webview is never handed `access_token`/`refresh_token`.
2. **Egress containment:** Confirm the webview cannot cause Rust to fetch an arbitrary URL —
   `forward()` always builds `BACKEND_BASE + path`; there is no caller-controlled host.
3. **Bridge surface:** Confirm the JS-callable command list is minimal (`session_status`,
   `native_sign_out`, `backend_origin`) and exposes no fs/shell/arbitrary-http. Review the CSP
   (`connect-src 'self' mainaiapi: ipc:`) and `capabilities/default.json` (no fs/shell).
4. **Cookie store correctness:** `capture_set_cookie` path-scoping and deletion (Max-Age=0)
   handling; no cookie leakage across paths; persistence atomicity (temp-file + rename).
5. **CORS in `lib.rs`:** `add_cors` reflects origin for an app-internal scheme; confirm this
   cannot be abused (no ambient webview credentials exist, since the session is in Rust).
6. **No embedded secrets:** only `BACKEND_BASE` (a URL) is baked in.

## Out of scope / unchanged
Security Kernel, RLS, owner isolation, backend auth, provider keys — none are modified by
either branch. Prior examiner findings remain open and separate:
`erasure-session-defect` (460e8ad) and `coordination-multiinstance` (#314 2308b697).

## Verification already performed by the implementer (to re-check, not to trust)
- PWA: production `next build` green; manifest/sw/icons served with correct status+type;
  `sw.js` serving diagnosed (correct MIME, identical bytes, root scope, no CSP); valid JS;
  ESLint clean; login renders. SW registration/install NOT yet observed in a real browser
  (Claude-in-Chrome not connected); embedded in-app pane cannot register SW (evidenced, not
  assumed).
- Tauri: macOS `.app`/`.dmg` built, arm64, SPA embedded, app launches without error.
