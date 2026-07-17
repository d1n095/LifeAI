const TOKEN_KEY = "mainai_token";

// Stored in localStorage, not an httpOnly cookie — a deliberate MVP tradeoff, not an
// oversight. The backend (see backend/app/security.py) issues stateless bearer JWTs with no
// server-side session/cookie infrastructure; adding one is real work (a same-origin auth
// proxy or a shared cookie domain) tracked as a Fas 1 hardening item, not something to fake
// here. localStorage is readable by any script on the page, so it depends on this frontend
// staying free of XSS — no unsanitized HTML injection anywhere (see chat message rendering).
export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}
