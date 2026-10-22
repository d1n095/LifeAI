import http from "node:http";
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

  test("encoded path traversal is rejected by the same-origin proxy", async ({ request }) => {
    const encoded = await request.get(`${PROXY_FRONTEND_URL}/api/%2e%2e/secret`);
    expect([400, 404]).toContain(encoded.status());
    if (encoded.status() === 400) {
      const body = await encoded.json();
      expect(body.proxy_error).toBeTruthy();
    }
    const dotted = await request.get(`${PROXY_FRONTEND_URL}/api/../secret`);
    expect([400, 404]).toContain(dotted.status());
  });

  test("client X-Forwarded-For is not a rate-limit identity", async ({ request }) => {
    const statuses: number[] = [];
    for (let i = 0; i < 12; i++) {
      const res = await request.post(`${PROXY_FRONTEND_URL}/api/auth/login`, {
        data: { email: "brute-force-target@example.com", password: "wrong-guess" },
        headers: { "X-Forwarded-For": `203.0.113.${i + 1}` },
      });
      statuses.push(res.status());
    }
    expect(statuses).toContain(429);
  });

  test("PX-9/10/11 real next start peers stay distinct and forged XFF cannot hide that", async () => {
    const target = new URL(`${PROXY_FRONTEND_URL}/api/auth/login`);
    const post = (localAddress: string, path: string, body: Record<string, string>, xff: string) =>
      new Promise<number>((resolve, reject) => {
        const payload = JSON.stringify(body);
        const req = http.request(
          {
            hostname: target.hostname,
            port: Number(target.port),
            path,
            method: "POST",
            localAddress,
            headers: {
              "content-type": "application/json",
              "content-length": Buffer.byteLength(payload),
              "x-forwarded-for": xff,
            },
          },
          (res) => {
            res.resume();
            res.on("end", () => resolve(res.statusCode || 0));
          },
        );
        req.on("error", reject);
        req.write(payload);
        req.end();
      });

    const peerA: number[] = [];
    for (let i = 0; i < 12; i++) {
      peerA.push(
        await post("127.0.0.1", "/api/auth/login", { email: "px9-a@example.com", password: "wrong-guess" }, `198.51.100.${i + 1}`),
      );
    }
    expect(peerA, "PX-9 same TCP peer must still 429 despite rotating XFF").toContain(429);

    const peerB = await post(
      "127.0.0.2",
      "/api/auth/login",
      { email: "px11-b@example.com", password: "wrong-guess" },
      "198.51.100.9",
    );
    expect(peerB, "PX-11 a distinct TCP peer must not share the exhausted key").not.toBe(429);
    expect([400, 401, 403, 422]).toContain(peerB);

    const otherSurface = await post(
      "127.0.0.1",
      "/api/auth/register",
      { email: "px10-surface@example.com", password: "TestSurfacePassword123!", website: "" },
      "198.51.100.9",
    );
    expect(otherSurface, "PX-10 exhausting login must not lock register").not.toBe(429);
  });
});
