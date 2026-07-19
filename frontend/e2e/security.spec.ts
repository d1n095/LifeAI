import { expect, test } from "@playwright/test";
import { ATTACKER_URL, FRONTEND_URL } from "../playwright.config";
import { loginViaUi, trackCsrf } from "./helpers";

// Rewritten for the combined-container same-origin topology (Dockerfile.combined,
// scripts/entrypoint-combined.sh): the backend is loopback-only inside its own container and
// has NO origin a browser, an attacker, or this test process can dial directly — every check
// below goes exclusively through the frontend's own same-origin proxy
// (frontend/app/api/[...path]/route.ts), which is the only path that exists in production
// regardless of topology (Docker Compose or the combined container). This is a stricter test
// than the old cross-origin version, not a weaker one: see the "backend has no reachable
// origin at all" test below, which is a strictly stronger property than "cookies aren't
// visible on the backend's origin" (the old version's version of this check, meaningless now
// that there IS no separate backend origin to visit).
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

async function getCookie(context: import("@playwright/test").BrowserContext, name: string) {
  const cookies = await context.cookies();
  return cookies.find((c) => c.name === name) ?? null;
}

test.describe("cookie/CSRF/rotation security", () => {
  test("session tokens are unreadable by JavaScript", async ({ page, context }) => {
    const csrf = trackCsrf(page);
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    expect(csrf.get()).toBeTruthy();

    const accessCookie = await getCookie(context, "access_token");
    const refreshCookie = await getCookie(context, "refresh_token");
    expect(accessCookie?.httpOnly).toBe(true);
    expect(refreshCookie?.httpOnly).toBe(true);
    expect(accessCookie?.secure).toBe(true);
    expect(refreshCookie?.secure).toBe(true);
    expect(refreshCookie?.path).toBe("/api/auth");
    expect(accessCookie?.path).toBe("/");
    // The cookie is scoped to the frontend's own host — there is no other origin it could
    // leak to even if HttpOnly were somehow bypassed (see the loopback-isolation test below).
    expect(accessCookie?.domain.replace(/^\./, "")).toBe(new URL(FRONTEND_URL).hostname);

    const cookieStringOnFrontendOrigin = await page.evaluate(() => document.cookie);
    expect(cookieStringOnFrontendOrigin).not.toContain("access_token");
    expect(cookieStringOnFrontendOrigin).not.toContain("refresh_token");
  });

  // Only meaningful against a real container where the backend is genuinely loopback-only
  // and unreachable from outside (see Dockerfile.combined) — E2E_BACKEND_DIRECT_PORT is set
  // by the combined-container-verify CI job (.github/workflows/ci.yml) to the backend's
  // in-container port. Skipped (not "expected to fail" — genuinely inapplicable) when the
  // env var is absent, e.g. under the cross-origin Docker Compose dev topology, where the
  // backend's port is intentionally published for local development.
  test("the backend has no reachable origin at all, from outside its own container", async ({ request }) => {
    test.skip(!process.env.E2E_BACKEND_DIRECT_PORT, "not testing a loopback-isolated backend in this run");
    const port = process.env.E2E_BACKEND_DIRECT_PORT;
    const host = new URL(FRONTEND_URL).hostname;
    await expect(request.get(`http://${host}:${port}/api/health`, { timeout: 3000 })).rejects.toThrow();
  });

  test("a genuine cross-origin CSRF attack against the proxy is rejected server-side", async ({ page, context }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    // The attacker page fetches the FRONTEND's own /api/projects (the proxy) — that's the
    // only network path to the backend that exists at all now, so it's also the only one
    // worth attacking. SameSite=None (app/config.py) means the real session cookie is
    // attached automatically even though the request originates from evil.example; CSRF-token
    // enforcement, not same-origin-ness, is what has to stop this.
    await page.goto(`${ATTACKER_URL}/`, { waitUntil: "load" });
    const attackResult = await page.evaluate((frontendUrl) => (window as any).runAttack(frontendUrl), FRONTEND_URL);
    expect(attackResult.attempted).toBe(true); // browser attached real cookies automatically

    // The response is opaque to the attacker (mode:"no-cors") — verify the actual
    // server-side effect out-of-band, as the legitimate user.
    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    const projectsRes = await context.request.get(`${FRONTEND_URL}/api/projects`);
    const projects = await projectsRes.json();
    expect(Array.isArray(projects)).toBe(true);
    expect(projects.some((p: { name: string }) => p.name === "CSRF-attack-project")).toBe(false);
  });

  test("mutating request without a CSRF header is rejected (403)", async ({ page, context }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    const res = await context.request.post(`${FRONTEND_URL}/api/projects`, {
      data: { name: "no csrf header", status: "active" },
    });
    expect(res.status()).toBe(403);
  });

  test("refresh rotates the token and CSRF value; replay of the old one is rejected and revokes the family", async ({
    page,
    context,
  }) => {
    const csrf = trackCsrf(page);
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    const csrf1 = csrf.get()!;
    const refresh1 = await getCookie(context, "refresh_token");

    const refreshRes1 = await context.request.post(`${FRONTEND_URL}/api/auth/refresh`, {
      headers: { "X-CSRF-Token": csrf1 },
    });
    expect(refreshRes1.status()).toBe(200);
    const csrf2 = (await refreshRes1.json()).csrf_token as string;
    const refresh2 = await getCookie(context, "refresh_token");
    expect(refresh2?.value).not.toBe(refresh1?.value);
    expect(csrf2).not.toBe(csrf1);

    // Replay the OLD (already-rotated-away) refresh token.
    await context.addCookies([{ ...refresh1!, value: refresh1!.value }]);
    const replayRes = await context.request.post(`${FRONTEND_URL}/api/auth/refresh`, {
      headers: { "X-CSRF-Token": csrf1 },
    });
    expect(replayRes.status()).toBe(401);

    // The token that replaced it (otherwise still valid in isolation) must ALSO now be
    // dead — full-family revocation, not just rejection of the replayed token.
    await context.addCookies([{ ...refresh2!, value: refresh2!.value }]);
    const postReuseRes = await context.request.post(`${FRONTEND_URL}/api/auth/refresh`, {
      headers: { "X-CSRF-Token": csrf2 },
    });
    expect(postReuseRes.status()).toBe(401);
  });

  test("logout revokes the access token immediately, not just the refresh token", async ({ page, context }) => {
    const csrf = trackCsrf(page);
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    const before = await context.request.get(`${FRONTEND_URL}/api/auth/me`);
    expect(before.status()).toBe(200);

    const logoutRes = await context.request.post(`${FRONTEND_URL}/api/auth/logout`, {
      headers: { "X-CSRF-Token": csrf.get()! },
    });
    expect(logoutRes.status()).toBe(200);

    const after = await context.request.get(`${FRONTEND_URL}/api/auth/me`);
    expect(after.status()).toBe(401);
  });
});
