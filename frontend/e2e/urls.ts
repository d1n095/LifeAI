// Central, env-overridable origin constants for the E2E suite. Defaults match the existing
// local/CI cross-origin dev setup (playwright.config.ts); the combined-container verification
// job (playwright.combined.config.ts, .github/workflows/ci.yml's combined-container-verify
// job) overrides E2E_FRONTEND_URL/E2E_ATTACKER_URL to point at the container's published
// port instead, without needing a second copy of every spec file.
//
// There is deliberately no BACKEND_URL export. Every spec calls the backend exclusively
// through the frontend's own same-origin proxy (frontend/app/api/[...path]/route.ts,
// INTERNAL_API_URL) — that's the only path that exists at all once the backend is
// loopback-only (see Dockerfile.combined, scripts/entrypoint-combined.sh), and it's already
// how production traffic is routed regardless of topology, so testing through it is more
// representative than dialing the backend directly ever was.
// "localhost", not "127.0.0.1": Playwright's context.request (used throughout security.spec.ts
// and account.spec.ts for direct API calls, not page navigation) does not attach Secure
// cookies to a plain-http request whose host is an IP literal — verified empirically while
// adapting these specs, where every context.request call after a real login came back 401
// despite the browser's own cookie jar holding a valid, unexpired, correctly-scoped Secure
// cookie. Switching the host string from "127.0.0.1" to "localhost" (same loopback interface,
// same server) made every one of those calls succeed. Browser *navigation* (page.goto, used by
// loginViaUi) is unaffected either way — Chromium already treats both as trustworthy origins —
// so this only matters for the context.request call sites, but there's no reason to keep two
// conventions, so this is the one default.
export const FRONTEND_URL = process.env.E2E_FRONTEND_URL || "http://localhost:3020";
export const ATTACKER_URL = process.env.E2E_ATTACKER_URL || "http://127.0.0.1:9099";
