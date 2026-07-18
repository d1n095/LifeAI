import { execSync } from "child_process";
import { expect, test } from "@playwright/test";
import { BACKEND_URL, FRONTEND_URL } from "../playwright.config";
import { extractToken, latestEmailTo, loginViaUi, randomEmail, registerViaUi, trackCsrf } from "./helpers";

const DB_URL = process.env.E2E_DATABASE_URL || "postgresql://lifeos@localhost:5433/lifeos";

function psql(sql: string): string {
  return execSync(`psql "${DB_URL}" -t -A -c "${sql.replace(/"/g, '\\"')}"`, { encoding: "utf-8" }).trim();
}

const PASSWORD = "Test1234Password!";

test.describe.serial("account lifecycle", () => {
  const userA = { email: randomEmail("e2e-a"), password: PASSWORD };
  const userB = { email: randomEmail("e2e-b"), password: PASSWORD };

  test("registration, duplicate email, honeypot, and verification", async ({ page, context }) => {
    await registerViaUi(page, FRONTEND_URL, userA.email, userA.password);

    const firstVerifyEmail = latestEmailTo(userA.email);
    expect(firstVerifyEmail).toBeTruthy();
    const firstToken = extractToken(firstVerifyEmail!.body);

    // Duplicate registration: neutral response, no second account, fresh token issued
    // (old one invalidated).
    const dupRes = await context.request.post(`${BACKEND_URL}/api/auth/register`, {
      data: { email: userA.email, password: userA.password, website: "" },
    });
    expect(dupRes.status()).toBe(202);
    expect(psql(`SELECT count(*) FROM users WHERE email = '${userA.email}'`)).toBe("1");

    const secondToken = extractToken(latestEmailTo(userA.email)!.body);
    expect(secondToken).not.toBe(firstToken);

    const oldTokenRes = await context.request.post(`${BACKEND_URL}/api/auth/verify-email`, {
      data: { token: firstToken },
    });
    expect(oldTokenRes.status()).toBe(400);

    // Honeypot: silently dropped, same response, no account created.
    const botEmail = randomEmail("e2e-bot");
    const botRes = await context.request.post(`${BACKEND_URL}/api/auth/register`, {
      data: { email: botEmail, password: "Test1234Password!", website: "http://spam.example" },
    });
    expect(botRes.status()).toBe(202);
    expect(psql(`SELECT count(*) FROM users WHERE email = '${botEmail}'`)).toBe("0");

    // Login blocked before verification.
    const preVerifyLogin = await context.request.post(`${BACKEND_URL}/api/auth/login`, { data: userA });
    expect(preVerifyLogin.status()).toBe(403);

    // Expired token rejected.
    psql(
      `UPDATE email_verification_tokens SET expires_at = now() - interval '1 hour' ` +
        `WHERE token_hash = (SELECT t.token_hash FROM email_verification_tokens t JOIN users u ON u.id = t.user_id ` +
        `WHERE u.email = '${userA.email}' ORDER BY t.created_at DESC LIMIT 1)`
    );
    const expiredRes = await context.request.post(`${BACKEND_URL}/api/auth/verify-email`, {
      data: { token: secondToken },
    });
    expect(expiredRes.status()).toBe(400);

    // Resend issues a fresh, valid token; verifying with it succeeds.
    await context.request.post(`${BACKEND_URL}/api/auth/resend-verification`, { data: { email: userA.email } });
    const thirdToken = extractToken(latestEmailTo(userA.email)!.body);
    const verifyRes = await context.request.post(`${BACKEND_URL}/api/auth/verify-email`, {
      data: { token: thirdToken },
    });
    expect(verifyRes.status()).toBe(200);

    // Reusing a consumed token fails.
    const reuseRes = await context.request.post(`${BACKEND_URL}/api/auth/verify-email`, {
      data: { token: thirdToken },
    });
    expect(reuseRes.status()).toBe(400);

    // Login now succeeds via the real UI.
    await loginViaUi(page, FRONTEND_URL, userA.email, userA.password);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    expect(page.url()).toBe(FRONTEND_URL + "/");

    // Weak password rejected on registration.
    const weakRes = await context.request.post(`${BACKEND_URL}/api/auth/register`, {
      data: { email: randomEmail("e2e-weak"), password: "short1", website: "" },
    });
    expect(weakRes.status()).toBe(400);
  });

  test("password reset: forgot -> reset -> old sessions revoked -> new password works", async ({ context }) => {
    const forgotRes = await context.request.post(`${BACKEND_URL}/api/auth/forgot-password`, {
      data: { email: userA.email },
    });
    expect(forgotRes.status()).toBe(202);
    const resetToken = extractToken(latestEmailTo(userA.email)!.body);

    const weakResetRes = await context.request.post(`${BACKEND_URL}/api/auth/reset-password`, {
      data: { token: resetToken, new_password: "short1" },
    });
    expect(weakResetRes.status()).toBe(400);

    const newPassword = "NewerPassword5678!";
    const resetSuccessRes = await context.request.post(`${BACKEND_URL}/api/auth/reset-password`, {
      data: { token: resetToken, new_password: newPassword },
    });
    expect(resetSuccessRes.status()).toBe(200);

    // The session established before the reset must now be dead.
    const meAfterReset = await context.request.get(`${BACKEND_URL}/api/auth/me`);
    expect(meAfterReset.status()).toBe(401);

    const reusedTokenRes = await context.request.post(`${BACKEND_URL}/api/auth/reset-password`, {
      data: { token: resetToken, new_password: "AnotherPassword789!" },
    });
    expect(reusedTokenRes.status()).toBe(400);

    const oldPasswordLogin = await context.request.post(`${BACKEND_URL}/api/auth/login`, {
      data: { email: userA.email, password: userA.password },
    });
    expect(oldPasswordLogin.status()).toBe(401);

    const newPasswordLogin = await context.request.post(`${BACKEND_URL}/api/auth/login`, {
      data: { email: userA.email, password: newPassword },
    });
    expect(newPasswordLogin.status()).toBe(200);
    userA.password = newPassword;

    // Expired reset token (DB-manipulated) is rejected.
    await context.request.post(`${BACKEND_URL}/api/auth/forgot-password`, { data: { email: userA.email } });
    const expiredResetToken = extractToken(latestEmailTo(userA.email)!.body);
    psql(
      `UPDATE password_reset_tokens SET expires_at = now() - interval '1 hour' ` +
        `WHERE token_hash = (SELECT t.token_hash FROM password_reset_tokens t JOIN users u ON u.id = t.user_id ` +
        `WHERE u.email = '${userA.email}' ORDER BY t.created_at DESC LIMIT 1)`
    );
    const expiredResetRes = await context.request.post(`${BACKEND_URL}/api/auth/reset-password`, {
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

    expect((await deviceX.request.get(`${BACKEND_URL}/api/auth/me`)).status()).toBe(200);
    expect((await deviceY.request.get(`${BACKEND_URL}/api/auth/me`)).status()).toBe(200);

    await pageX.getByRole("link", { name: "Konto" }).click();
    await pageX.waitForURL(FRONTEND_URL + "/account");
    await pageX.getByRole("button", { name: "Logga ut från alla enheter" }).click();
    await pageX.waitForURL(/\/login/, { timeout: 5000 });
    expect(pageX.url()).toContain("/login");

    expect((await deviceY.request.get(`${BACKEND_URL}/api/auth/me`)).status()).toBe(401);

    await deviceX.close();
    await deviceY.close();
  });

  test("cross-user isolation holds for freshly-registered accounts (RLS regression)", async ({ page, context }) => {
    await registerViaUi(page, FRONTEND_URL, userB.email, userB.password);
    const userBToken = extractToken(latestEmailTo(userB.email)!.body);
    await context.request.post(`${BACKEND_URL}/api/auth/verify-email`, { data: { token: userBToken } });

    await loginViaUi(page, FRONTEND_URL, userB.email, userB.password);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    await page.getByRole("link", { name: "Chat", exact: true }).click();
    await page.waitForURL(FRONTEND_URL + "/chat");
    await page.getByLabel("Meddelande till MainAI").fill("Unikt meddelande fran anvandare B");
    await page.getByRole("button", { name: "Skicka" }).click();
    await expect(page.locator("text=Detta ar ett riktigt svar")).toBeVisible({ timeout: 8000 });

    const userBConversations = await (await context.request.get(`${BACKEND_URL}/api/conversations`)).json();
    expect(userBConversations.length).toBeGreaterThan(0);

    // Fresh context, log in as user A, confirm B's conversation is invisible.
    const ctxAIsolationCheck = await page.context().browser()!.newContext();
    await ctxAIsolationCheck.request.post(`${BACKEND_URL}/api/auth/login`, { data: userA });
    const userAConversations = await (await ctxAIsolationCheck.request.get(`${BACKEND_URL}/api/conversations`)).json();
    const leaked = userAConversations.some(
      (c: { title?: string }) => c.title && c.title.includes("Unikt meddelande fran anvandare B")
    );
    expect(leaked).toBe(false);
    await ctxAIsolationCheck.close();
  });

  test("account deletion: wrong password rejected, correct password deletes permanently", async ({
    page,
    context,
  }) => {
    const csrf = trackCsrf(page);
    await loginViaUi(page, FRONTEND_URL, userB.email, userB.password);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    const wrongPasswordDelete = await context.request.delete(`${BACKEND_URL}/api/account`, {
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

    const loginAfterDelete = await context.request.post(`${BACKEND_URL}/api/auth/login`, {
      data: { email: userB.email, password: userB.password },
    });
    expect(loginAfterDelete.status()).toBe(401);
  });
});
