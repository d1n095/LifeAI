import { expect, test } from "@playwright/test";
import { ATTACKER_URL, BACKEND_URL, FRONTEND_URL } from "../playwright.config";
import { loginViaUi, trackCsrf } from "./helpers";

const ADMIN_EMAIL = "admin@lifeos.local";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || "TestAdminPassword123!";

async function getCookie(context: import("@playwright/test").BrowserContext, name: string) {
  const cookies = await context.cookies();
  return cookies.find((c) => c.name === name) ?? null;
}

test.describe("cookie/CSRF/rotation security", () => {
  test("session tokens are unreadable by JavaScript, on both origins", async ({ page, context }) => {
    const csrf = trackCsrf(page);
    await loginViaUi(page, FRONTEND_URL, ADMIN_EMAIL, ADMIN_PASSWORD);
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

    // Navigate to the BACKEND's own origin — proves HttpOnly itself blocks JS, not merely
    // the cross-origin same-origin-policy.
    await page.goto(`${BACKEND_URL}/docs`, { waitUntil: "domcontentloaded" });
    const cookieStringOnBackendOrigin = await page.evaluate(() => document.cookie);
    expect(cookieStringOnBackendOrigin).not.toContain("access_token");
    expect(cookieStringOnBackendOrigin).not.toContain("refresh_token");

    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    const cookieStringOnFrontendOrigin = await page.evaluate(() => document.cookie);
    expect(cookieStringOnFrontendOrigin).not.toContain("access_token");
    expect(cookieStringOnFrontendOrigin).not.toContain("refresh_token");
  });

  test("a genuine cross-origin CSRF attack is rejected server-side", async ({ page, context }) => {
    await loginViaUi(page, FRONTEND_URL, ADMIN_EMAIL, ADMIN_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    await page.goto(`${ATTACKER_URL}/`, { waitUntil: "load" });
    const attackResult = await page.evaluate((backendUrl) => (window as any).runAttack(backendUrl), BACKEND_URL);
    expect(attackResult.attempted).toBe(true); // browser attached real cookies automatically

    // The response is opaque to the attacker (mode:"no-cors") — verify the actual
    // server-side effect out-of-band, as the legitimate user.
    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    const projectsRes = await context.request.get(`${BACKEND_URL}/api/projects`);
    const projects = await projectsRes.json();
    expect(Array.isArray(projects)).toBe(true);
    expect(projects.some((p: { name: string }) => p.name === "CSRF-attack-project")).toBe(false);
  });

  test("mutating request without a CSRF header is rejected (403)", async ({ page, context }) => {
    await loginViaUi(page, FRONTEND_URL, ADMIN_EMAIL, ADMIN_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    const res = await context.request.post(`${BACKEND_URL}/api/projects`, {
      data: { name: "no csrf header", status: "active" },
    });
    expect(res.status()).toBe(403);
  });

  test("refresh rotates the token and CSRF value; replay of the old one is rejected and revokes the family", async ({
    page,
    context,
  }) => {
    const csrf = trackCsrf(page);
    await loginViaUi(page, FRONTEND_URL, ADMIN_EMAIL, ADMIN_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    const csrf1 = csrf.get()!;
    const refresh1 = await getCookie(context, "refresh_token");

    const refreshRes1 = await context.request.post(`${BACKEND_URL}/api/auth/refresh`, {
      headers: { "X-CSRF-Token": csrf1 },
    });
    expect(refreshRes1.status()).toBe(200);
    const csrf2 = (await refreshRes1.json()).csrf_token as string;
    const refresh2 = await getCookie(context, "refresh_token");
    expect(refresh2?.value).not.toBe(refresh1?.value);
    expect(csrf2).not.toBe(csrf1);

    // Replay the OLD (already-rotated-away) refresh token.
    await context.addCookies([{ ...refresh1!, value: refresh1!.value }]);
    const replayRes = await context.request.post(`${BACKEND_URL}/api/auth/refresh`, {
      headers: { "X-CSRF-Token": csrf1 },
    });
    expect(replayRes.status()).toBe(401);

    // The token that replaced it (otherwise still valid in isolation) must ALSO now be
    // dead — full-family revocation, not just rejection of the replayed token.
    await context.addCookies([{ ...refresh2!, value: refresh2!.value }]);
    const postReuseRes = await context.request.post(`${BACKEND_URL}/api/auth/refresh`, {
      headers: { "X-CSRF-Token": csrf2 },
    });
    expect(postReuseRes.status()).toBe(401);
  });

  test("logout revokes the access token immediately, not just the refresh token", async ({ page, context }) => {
    const csrf = trackCsrf(page);
    await loginViaUi(page, FRONTEND_URL, ADMIN_EMAIL, ADMIN_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    const before = await context.request.get(`${BACKEND_URL}/api/auth/me`);
    expect(before.status()).toBe(200);

    const logoutRes = await context.request.post(`${BACKEND_URL}/api/auth/logout`, {
      headers: { "X-CSRF-Token": csrf.get()! },
    });
    expect(logoutRes.status()).toBe(200);

    const after = await context.request.get(`${BACKEND_URL}/api/auth/me`);
    expect(after.status()).toBe(401);
  });
});
