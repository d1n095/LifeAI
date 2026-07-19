import { execSync } from "child_process";
import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { extractToken, latestEmailTo, loginViaUi, randomEmail, registerViaApi, trackCsrf } from "./helpers";

const DB_URL = process.env.E2E_DATABASE_URL || "postgresql://lifeos@localhost:5433/lifeos";

function psql(sql: string): string {
  return execSync(`psql "${DB_URL}" -t -A -c "${sql.replace(/"/g, '\\"')}"`, { encoding: "utf-8" }).trim();
}

const PASSWORD = "Test1234Password!";

// MainAI is Founder-only: chat/conversations/documents/knowledge/projects/admin all require
// the founder account specifically (see backend/app/deps.py's require_founder). That does
// NOT mean self-registration or login themselves are founder-restricted — register() stays
// reachable in non-production environments (see backend/app/routers/auth.py — the 404 block
// is scoped to ENVIRONMENT=production, which CI never sets) and a verified non-founder
// account can still log in and use generic self-service (session management, password
// reset, account export/deletion) exactly like before. What changed is what a logged-in
// non-founder can *do*: nothing MainAI-specific — see the dedicated test below.
test.describe.serial("account lifecycle", () => {
  const userA = { email: randomEmail("e2e-a"), password: PASSWORD };
  const userB = { email: randomEmail("e2e-b"), password: PASSWORD };

  test("registration, duplicate email, honeypot, and verification", async ({ page, context }) => {
    const registerRes = await registerViaApi(context, FRONTEND_URL, userA.email, userA.password);
    expect(registerRes.status()).toBe(202);

    const firstVerifyEmail = latestEmailTo(userA.email);
    expect(firstVerifyEmail).toBeTruthy();
    const firstToken = extractToken(firstVerifyEmail!.body);

    // Duplicate registration: neutral response, no second account, fresh token issued
    // (old one invalidated).
    const dupRes = await registerViaApi(context, FRONTEND_URL, userA.email, userA.password);
    expect(dupRes.status()).toBe(202);
    expect(psql(`SELECT count(*) FROM users WHERE email = '${userA.email}'`)).toBe("1");

    const secondToken = extractToken(latestEmailTo(userA.email)!.body);
    expect(secondToken).not.toBe(firstToken);

    const oldTokenRes = await context.request.post(`${FRONTEND_URL}/api/auth/verify-email`, {
      data: { token: firstToken },
    });
    expect(oldTokenRes.status()).toBe(400);

    // Honeypot: silently dropped, same response, no account created.
    const botEmail = randomEmail("e2e-bot");
    const botRes = await registerViaApi(context, FRONTEND_URL, botEmail, "Test1234Password!", "http://spam.example");
    expect(botRes.status()).toBe(202);
    expect(psql(`SELECT count(*) FROM users WHERE email = '${botEmail}'`)).toBe("0");

    // Login blocked before verification.
    const preVerifyLogin = await context.request.post(`${FRONTEND_URL}/api/auth/login`, { data: userA });
    expect(preVerifyLogin.status()).toBe(403);

    // Expired token rejected.
    psql(
      `UPDATE email_verification_tokens SET expires_at = now() - interval '1 hour' ` +
        `WHERE token_hash = (SELECT t.token_hash FROM email_verification_tokens t JOIN users u ON u.id = t.user_id ` +
        `WHERE u.email = '${userA.email}' ORDER BY t.created_at DESC LIMIT 1)`
    );
    const expiredRes = await context.request.post(`${FRONTEND_URL}/api/auth/verify-email`, {
      data: { token: secondToken },
    });
    expect(expiredRes.status()).toBe(400);

    // Resend issues a fresh, valid token; verifying with it succeeds.
    await context.request.post(`${FRONTEND_URL}/api/auth/resend-verification`, { data: { email: userA.email } });
    const thirdToken = extractToken(latestEmailTo(userA.email)!.body);
    const verifyRes = await context.request.post(`${FRONTEND_URL}/api/auth/verify-email`, {
      data: { token: thirdToken },
    });
    expect(verifyRes.status()).toBe(200);

    // Reusing a consumed token fails.
    const reuseRes = await context.request.post(`${FRONTEND_URL}/api/auth/verify-email`, {
      data: { token: thirdToken },
    });
    expect(reuseRes.status()).toBe(400);

    // Login now succeeds via the real UI — self-registration and login are unaffected by the
    // founder-only restriction, only MainAI's own routes are (see the dedicated test below).
    await loginViaUi(page, FRONTEND_URL, userA.email, userA.password);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    expect(page.url()).toBe(FRONTEND_URL + "/");

    // Weak password rejected on registration.
    const weakRes = await registerViaApi(context, FRONTEND_URL, randomEmail("e2e-weak"), "short1");
    expect(weakRes.status()).toBe(400);
  });

  test("a logged-in non-founder account is denied every MainAI-surface route", async ({ page, context }) => {
    // The actual founder-only boundary: complements backend/tests/account/
    // test_founder_only.py (pytest-level) with an E2E-level check that a real, logged-in,
    // non-founder session — cookies and CSRF token and all — still can't touch anything
    // MainAI-specific. 403, not 401: get_current_user already authenticated this session
    // successfully: require_founder is what then refuses it.
    const csrf = trackCsrf(page);
    await loginViaUi(page, FRONTEND_URL, userA.email, userA.password);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    for (const path of ["/api/conversations", "/api/documents", "/api/projects", "/api/admin/providers/status"]) {
      expect((await context.request.get(`${FRONTEND_URL}${path}`)).status()).toBe(403);
    }
    expect(
      (
        await context.request.post(`${FRONTEND_URL}/api/chat`, {
          data: { message: "hej" },
          headers: { "X-CSRF-Token": csrf.get() || "" },
        })
      ).status()
    ).toBe(403);
    expect(
      (
        await context.request.post(`${FRONTEND_URL}/api/knowledge/search`, {
          data: { query: "hej" },
          headers: { "X-CSRF-Token": csrf.get() || "" },
        })
      ).status()
    ).toBe(403);

    // Generic account self-service is NOT MainAI functionality and stays reachable — /me
    // still resolves this exact session, proving the 403s above are require_founder acting
    // deliberately, not a broken/expired session.
    expect((await context.request.get(`${FRONTEND_URL}/api/auth/me`)).status()).toBe(200);
  });

  test("password reset: forgot -> reset -> old sessions revoked -> new password works", async ({ context }) => {
    const forgotRes = await context.request.post(`${FRONTEND_URL}/api/auth/forgot-password`, {
      data: { email: userA.email },
    });
    expect(forgotRes.status()).toBe(202);
    const resetToken = extractToken(latestEmailTo(userA.email)!.body);

    const weakResetRes = await context.request.post(`${FRONTEND_URL}/api/auth/reset-password`, {
      data: { token: resetToken, new_password: "short1" },
    });
    expect(weakResetRes.status()).toBe(400);

    const newPassword = "NewerPassword5678!";
    const resetSuccessRes = await context.request.post(`${FRONTEND_URL}/api/auth/reset-password`, {
      data: { token: resetToken, new_password: newPassword },
    });
    expect(resetSuccessRes.status()).toBe(200);

    // The session established before the reset must now be dead.
    const meAfterReset = await context.request.get(`${FRONTEND_URL}/api/auth/me`);
    expect(meAfterReset.status()).toBe(401);

    const reusedTokenRes = await context.request.post(`${FRONTEND_URL}/api/auth/reset-password`, {
      data: { token: resetToken, new_password: "AnotherPassword789!" },
    });
    expect(reusedTokenRes.status()).toBe(400);

    const oldPasswordLogin = await context.request.post(`${FRONTEND_URL}/api/auth/login`, {
      data: { email: userA.email, password: userA.password },
    });
    expect(oldPasswordLogin.status()).toBe(401);

    const newPasswordLogin = await context.request.post(`${FRONTEND_URL}/api/auth/login`, {
      data: { email: userA.email, password: newPassword },
    });
    expect(newPasswordLogin.status()).toBe(200);
    userA.password = newPassword;

    // Expired reset token (DB-manipulated) is rejected.
    await context.request.post(`${FRONTEND_URL}/api/auth/forgot-password`, { data: { email: userA.email } });
    const expiredResetToken = extractToken(latestEmailTo(userA.email)!.body);
    psql(
      `UPDATE password_reset_tokens SET expires_at = now() - interval '1 hour' ` +
        `WHERE token_hash = (SELECT t.token_hash FROM password_reset_tokens t JOIN users u ON u.id = t.user_id ` +
        `WHERE u.email = '${userA.email}' ORDER BY t.created_at DESC LIMIT 1)`
    );
    const expiredResetRes = await context.request.post(`${FRONTEND_URL}/api/auth/reset-password`, {
      data: { token: expiredResetToken, new_password: "YetAnotherPassword12!" },
    });
    expect(expiredResetRes.status()).toBe(400);
  });

  test("logout from all devices ends every session, not just the caller's", async ({ browser }) => {
    const deviceX = await browser.newContext();
    const deviceY = await browser.newContext();
    const pageX = await deviceX.newPage();
    const pageY = await deviceY.newPage();

    for (const page of [pageX, pageY]) {
      await loginViaUi(page, FRONTEND_URL, userA.email, userA.password);
      await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    }

    expect((await deviceX.request.get(`${FRONTEND_URL}/api/auth/me`)).status()).toBe(200);
    expect((await deviceY.request.get(`${FRONTEND_URL}/api/auth/me`)).status()).toBe(200);

    await pageX.getByRole("link", { name: "Konto" }).click();
    await pageX.waitForURL(FRONTEND_URL + "/account");
    await pageX.getByRole("button", { name: "Logga ut från alla enheter" }).click();
    await pageX.waitForURL(/\/login/, { timeout: 5000 });
    expect(pageX.url()).toContain("/login");

    expect((await deviceY.request.get(`${FRONTEND_URL}/api/auth/me`)).status()).toBe(401);

    await deviceX.close();
    await deviceY.close();
  });

  test("account deletion: wrong password rejected, correct password deletes permanently", async ({
    page,
    context,
  }) => {
    const registerRes = await registerViaApi(context, FRONTEND_URL, userB.email, userB.password);
    expect(registerRes.status()).toBe(202);
    const userBToken = extractToken(latestEmailTo(userB.email)!.body);
    await context.request.post(`${FRONTEND_URL}/api/auth/verify-email`, { data: { token: userBToken } });

    const csrf = trackCsrf(page);
    await loginViaUi(page, FRONTEND_URL, userB.email, userB.password);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    const wrongPasswordDelete = await context.request.delete(`${FRONTEND_URL}/api/account`, {
      headers: { "X-CSRF-Token": csrf.get()! },
      data: { password: "definitely-wrong-password" },
    });
    expect(wrongPasswordDelete.status()).toBe(403);
    expect(psql(`SELECT count(*) FROM users WHERE email = '${userB.email}'`)).toBe("1");

    await page.goto(`${FRONTEND_URL}/account`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Radera mitt konto" }).click();
    await page.getByLabel("Ange ditt lösenord för att bekräfta permanent radering").fill(userB.password);
    await page.getByRole("button", { name: "Radera permanent" }).click();
    await page.waitForURL(/\/login/, { timeout: 5000 });
    expect(page.url()).toContain("/login");

    expect(psql(`SELECT count(*) FROM users WHERE email = '${userB.email}'`)).toBe("0");
    expect(
      psql(`SELECT count(*) FROM conversations WHERE user_id = (SELECT id FROM users WHERE email = '${userB.email}')`)
    ).toBe("0");

    const loginAfterDelete = await context.request.post(`${FRONTEND_URL}/api/auth/login`, {
      data: { email: userB.email, password: userB.password },
    });
    expect(loginAfterDelete.status()).toBe(401);
  });
});

// The former "cross-user isolation holds for freshly-registered accounts (RLS regression)"
// test is gone, not just renamed: it drove two different self-registered accounts through
// /chat to prove their conversation lists didn't leak into each other, but /api/chat and
// /api/conversations are now founder-only (see the "denied every MainAI-surface route" test
// above) — a self-registered account can no longer reach either endpoint at all, so there is
// no "two users both chatting" scenario left to regress on at this layer. RLS itself
// (Postgres row-level policies keyed on app.current_user_id) is still exercised directly,
// with an explicit multi-user context, in backend/tests/security/test_rls_isolation.py.
