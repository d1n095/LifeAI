import { expect, test } from "@playwright/test";
import { PROXY_FRONTEND_URL } from "../playwright.proxy.config";

// Confirms the same-origin proxy (frontend/app/api/[...path]/route.ts) actually works from a
// real browser against the frontend's real production server, not just via curl (see
// docs/RENDER_DEPLOY.md for the manual verification this mirrors): the login form calls a
// relative /api/* path, a real backend error (wrong password) surfaces correctly, the session
// cookie ends up scoped to the frontend's own origin rather than the backend's, and a
// CSRF-protected mutating call (logout) round-trips correctly. This is exactly the flow that
// broke in production before this change (see the "Failed to fetch" / "Kunde inte nå servern"
// investigation) — run with playwright.proxy.config.ts, not the default config.
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

test.describe("same-origin proxy", () => {
  test("wrong password error surfaces correctly through the proxy", async ({ page }) => {
    await page.goto(`${PROXY_FRONTEND_URL}/login`, { waitUntil: "networkidle" });
    await page.getByLabel("E-post").fill(FOUNDER_EMAIL);
    await page.getByLabel("Lösenord").fill("wrongpassword");
    await page.getByRole("button", { name: "Logga in" }).click();
    await page.waitForTimeout(800);
    expect(page.url()).toContain("/login");
    await expect(page.getByRole("alert").filter({ hasText: "Fel e-post" })).toHaveCount(1);
  });

  test("login sets a same-origin session cookie and CSRF round-trips through logout", async ({
    page,
    context,
  }) => {
    await page.goto(`${PROXY_FRONTEND_URL}/login`, { waitUntil: "networkidle" });
    await page.getByLabel("E-post").fill(FOUNDER_EMAIL);
    await page.getByLabel("Lösenord").fill(FOUNDER_PASSWORD);
    await page.getByRole("button", { name: "Logga in" }).click();
    await page.waitForURL(PROXY_FRONTEND_URL + "/", { timeout: 5000 });

    const cookies = await context.cookies();
    const accessToken = cookies.find((c) => c.name === "access_token");
    expect(accessToken).toBeTruthy();
    // The whole point of the proxy: the cookie is scoped to the FRONTEND's own host, never
    // the backend's — that's what sidesteps cross-origin cookie handling entirely, regardless
    // of where the backend actually runs.
    expect(accessToken!.domain.replace(/^\./, "")).toBe("127.0.0.1");
    expect(accessToken!.httpOnly).toBe(true);
    expect(accessToken!.secure).toBe(true);

    // The sidebar's logout button calls POST /api/auth/logout with the in-memory CSRF token
    // (see lib/auth.ts, components/Sidebar.tsx) — a successful logout (redirect to /login)
    // proves that header round-tripped through the proxy correctly, not just that the cookie
    // arrived.
    await page.getByRole("button", { name: "Logga ut", exact: true }).click();
    await page.waitForURL(/\/login/, { timeout: 5000 });
    expect(page.url()).toContain("/login");
  });
});
