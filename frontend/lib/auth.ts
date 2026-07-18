// No token storage here — deliberately. Session identity lives entirely in HttpOnly cookies
// (access_token, refresh_token) that this frontend's JavaScript can never read, by design
// (see docs/AUTH_THREAT_MODEL.md).
//
// The CSRF value below is held in a plain in-memory module variable, not a cookie: cookies
// set by the backend belong to the BACKEND's origin, and frontend JavaScript can never read
// another origin's cookies via document.cookie regardless of HttpOnly — that's a basic
// same-origin-policy fact, not something we opted out of. So the backend instead sends the
// current CSRF value once in the login/refresh/me JSON response body (readable cross-origin
// because our CORS allow-list explicitly permits it), and this module just remembers it for
// the lifetime of the page. It resets to null on every full page load/reload by construction
// — lib/api.ts's AuthGuard-triggered /me call repopulates it immediately.

let csrfToken: string | null = null;

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}
