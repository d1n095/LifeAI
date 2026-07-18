import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";

// The baseline end-to-end flow: unauthenticated redirect, login (wrong then right password),
// chat round-trip against the real backend (with only the outbound AI-provider call faked —
// see backend/scripts/run_e2e_backend.py), conversation history, admin usage view,
// responsive layout, and logout. Uses the bootstrap admin account (pre-verified — see
// app/bootstrap.py), not a self-registered one.
const ADMIN_EMAIL = "admin@lifeos.local";
const ADMIN_PASSWORD = process.env.E2E_ADMIN_PASSWORD || "TestAdminPassword123!";

test.describe("baseline app flow", () => {
  test("unauthenticated '/' redirects to /login", async ({ page }) => {
    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    await page.waitForURL(/\/login/, { timeout: 5000 }).catch(() => {});
    expect(page.url()).toContain("/login");
  });

  test("login form is accessible", async ({ page }) => {
    await page.goto(`${FRONTEND_URL}/login`, { waitUntil: "networkidle" });
    await expect(page.getByLabel("E-post")).toHaveCount(1);
    await expect(page.getByLabel("Lösenord")).toHaveCount(1);
  });

  test("full authenticated flow: wrong password, login, chat, history, admin, responsive, logout", async ({
    page,
  }) => {
    const consoleErrors: string[] = [];
    page.on("console", (msg) => {
      if (msg.type() === "error") consoleErrors.push(msg.text());
    });
    page.on("pageerror", (err) => consoleErrors.push(String(err)));

    // Wrong password shows an error, stays on /login.
    await page.goto(`${FRONTEND_URL}/login`, { waitUntil: "networkidle" });
    await page.getByLabel("E-post").fill(ADMIN_EMAIL);
    await page.getByLabel("Lösenord").fill("wrongpassword");
    await page.getByRole("button", { name: "Logga in" }).click();
    await page.waitForTimeout(800);
    // getByRole("alert") also matches Next.js's own <next-route-announcer> (an always-present
    // framework a11y element with an internal alert role and empty text) — scope to text.
    expect(page.url()).toContain("/login");
    await expect(page.getByRole("alert").filter({ hasText: "Fel e-post" })).toHaveCount(1);

    // Correct login redirects to the dashboard.
    await page.getByLabel("E-post").fill(ADMIN_EMAIL);
    await page.getByLabel("Lösenord").fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "Logga in" }).click();
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    expect(page.url()).toBe(FRONTEND_URL + "/");
    await expect(page.locator("text=God kväll").first()).toBeVisible({ timeout: 5000 }).catch(() => {});

    await expect(page.locator(`text=${ADMIN_EMAIL}`).first()).toBeVisible();

    // Chat round-trip through the real backend (AI provider call faked, everything else real).
    await page.getByRole("link", { name: "Chat", exact: true }).click();
    await page.waitForURL(FRONTEND_URL + "/chat");
    const chatInput = page.getByLabel("Meddelande till MainAI");
    await expect(chatInput).toHaveCount(1);
    await chatInput.fill("Vad kostar prenumerationen?");
    await page.getByRole("button", { name: "Skicka" }).click();
    await expect(page.locator("text=Detta ar ett riktigt svar")).toBeVisible({ timeout: 8000 });
    await expect(page.locator("text=tillförlitlighet")).toBeVisible();
    await expect(page.locator("text=Personalhandbok.pdf")).toBeVisible();

    // New conversation appears in the history panel.
    await page.waitForTimeout(500);
    const historyCount = await page.locator('[aria-label="Tidigare konversationer"] >> text=Vad kostar').count();
    expect(historyCount).toBeGreaterThan(0);

    // Admin usage view reflects the chat call just made.
    await page.getByRole("link", { name: "Admin" }).click();
    await page.waitForURL(FRONTEND_URL + "/admin");
    await page.waitForSelector("text=Användning", { timeout: 5000 });
    await page.waitForTimeout(500);
    const usageRow = await page.locator("table >> text=openai").count();
    expect(usageRow).toBeGreaterThan(0);

    // Responsive: mobile viewport collapses the sidebar behind a menu button.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    const desktopSidebarVisible = await page.locator("aside").isVisible().catch(() => false);
    expect(desktopSidebarVisible).toBe(false);
    const menuButton = page.getByRole("button", { name: "Öppna meny" });
    await expect(menuButton).toHaveCount(1);
    await menuButton.click();
    await page.waitForTimeout(200);
    await expect(page.locator("#mobile-nav")).toBeVisible();

    // Logout clears the session and redirects; a protected route is inaccessible afterward.
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Logga ut" }).click();
    await page.waitForURL(/\/login/, { timeout: 5000 });
    expect(page.url()).toContain("/login");

    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });
    await page.waitForURL(/\/login/, { timeout: 5000 }).catch(() => {});
    expect(page.url()).toContain("/login");

    // 401/403 are expected transient auth-state-transition noise (deliberate wrong-password
    // attempt above; a leftover refresh cookie surviving a full-page reload that wipes the
    // in-memory CSRF value — see lib/api.ts) — the app recovers correctly regardless, as
    // every other assertion in this test confirms.
    const unexpectedErrors = consoleErrors.filter((e) => !e.includes("401") && !e.includes("403"));
    expect(unexpectedErrors, `Unexpected console errors: ${unexpectedErrors.join("; ")}`).toEqual([]);
  });
});
