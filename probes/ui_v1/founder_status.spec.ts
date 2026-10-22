// UI-1..UI-3: Founder-status truthfulness in a real browser against the live local stack (proxy_v1/live_stack.sh).
// Synthetic CI founder credentials only; no paid provider (CI E2E fakes), GitHub writes off, Recall unmounted.
import { test, expect, Page } from "@playwright/test";

const EMAIL = process.env.UI_FOUNDER_EMAIL || "founder@lifeos.local";
const PASSWORD = process.env.UI_FOUNDER_PASSWORD || "TestFounderPassword123!";

async function login(page: Page) {
  await page.goto("/login");
  await page.getByLabel("E-post").fill(EMAIL);
  await page.getByLabel("Lösenord").fill(PASSWORD);
  await page.getByRole("button", { name: "Logga in" }).click();
  await page.waitForURL((u) => !u.pathname.startsWith("/login"), { timeout: 15000 });
}

test("UI-1 (D4) founder-ops never keeps 'Backend: nåbar' / 'Inte blockerad' after the snapshot becomes unreadable", async ({ page }) => {
  await login(page);
  await page.goto("/admin/founder-ops");
  const health = page.getByTestId("founder-health");
  await expect(health).toContainText("Backend nåbar", { timeout: 15000 });
  // Backend lost from now on: every snapshot poll fails at the network level.
  await page.route("**/api/mainai/founder-ops/**", (route) => route.abort("connectionrefused"));
  await page.waitForTimeout(8000); // > one 5 s poll
  // After the poll fails the page must not keep asserting a positive health/STOP state anywhere
  // (the page may clear the snapshot entirely, or show an error/loading state).
  await expect(page.getByText("Backend nåbar")).toHaveCount(0);
  await expect(page.getByText("STOP inaktiv")).toHaveCount(0);
});

test("UI-2 (D4) dashboard health line does not stay 'Backend nåbar' after the backend is lost", async ({ page }) => {
  await login(page);
  await page.goto("/");
  await expect(page.getByTestId("dashboard-health")).toContainText("Backend nåbar", { timeout: 15000 });
  await page.route("**/api/**", (route) => route.abort("connectionrefused"));
  await page.waitForTimeout(12000);
  await expect(page.getByTestId("dashboard-health")).not.toContainText("Backend nåbar");
});

test("UI-3 (D5) readiness BLOCKED is never shown next to 'BLOCKERINGAR (0)'", async ({ page }) => {
  await login(page);
  await page.goto("/admin/founder-ops");
  const level = page.getByTestId("readiness-level");
  await expect(level).toBeVisible({ timeout: 15000 });
  const text = (await level.textContent()) || "";
  test.skip(!/BLOCKED|UNKNOWN/.test(text), `precondition: live stack readiness is ${text}`);
  await expect(page.getByText(/BLOCKERINGAR \(0\)/i)).toHaveCount(0);
});
