import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

test.describe("Founder workflow in safe local mode", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
  });

  test("dashboard and founder-ops show durable status, never a fabricated GO", async ({ page }) => {
    await expect(page.getByRole("heading", { name: /God kväll/ })).toBeVisible();
    await expect(page.getByTestId("dashboard-health")).not.toContainText("GO (oväntat)");
    await expect(page.locator('a[href="#main-content"]')).toHaveCount(1);

    await page.getByRole("link", { name: "Founder-status" }).first().click();
    await expect(page.getByRole("heading", { name: "Founder-status" })).toBeVisible({ timeout: 8000 });
    await expect(page.getByText("Första cykelns GO = false").first()).toBeVisible();
    await expect(page.getByText("Recall HTTP inte monterad")).toBeVisible();
    await expect(page.getByText("GitHub-skrivning av")).toBeVisible();
    await expect(page.getByTestId("work-status-counts")).toContainText("Tilldelad");
    await expect(page.getByTestId("work-status-counts")).toContainText("Kvitterad");
    await expect(page.getByTestId("founder-agents")).toBeVisible();
    await expect(page.getByTestId("founder-next-actions")).toBeVisible();
    await expect(page.getByTestId("independent-ready-work")).toBeVisible();
    await expect(page.getByTestId("founder-dependencies")).toBeVisible();
    await expect(page.getByTestId("readiness-level")).toBeVisible();
  });

  test("shell navigation, skip link, and mobile menu remain usable", async ({ page }) => {
    await expect(page.locator("#main-content")).toBeVisible();
    const nav = page.getByRole("navigation", { name: "Huvudmeny" });
    await nav.getByRole("link", { name: "Chat", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Chat med MainAI", exact: true })).toBeVisible();
    await nav.getByRole("link", { name: "Knowledge Studio", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Life Library", exact: true })).toBeVisible();
    await nav.getByRole("link", { name: "Projekt", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Projekt & roadmap", exact: true })).toBeVisible();
    await nav.getByRole("link", { name: "Konto", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Mitt konto", exact: true })).toBeVisible();
    await nav.getByRole("link", { name: "Agentuppdrag", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Agentuppdrag", exact: true })).toBeVisible();

    await page.setViewportSize({ width: 390, height: 844 });
    await expect(page.getByRole("button", { name: "Öppna meny" })).toBeVisible();
    await page.getByRole("button", { name: "Öppna meny" }).click();
    await expect(page.locator("#mobile-nav")).toBeVisible();
    await page.locator("#mobile-nav").getByRole("link", { name: "Founder-status", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Founder-status", exact: true })).toBeVisible();
  });

  test("snapshot API stays fail-closed: no GO, no Recall, assigned is not running", async ({ page }) => {
    const snapshot = await page.evaluate(async () => {
      const res = await fetch("/api/mainai/founder-ops/snapshot", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(snapshot.status).toBe(200);
    expect(snapshot.body.first_cycle_go).toBe(false);
    expect(snapshot.body.fabricated_progress).toBe(false);
    expect(snapshot.body.safe_launch.github_write_enabled).toBe(false);
    expect(snapshot.body.safe_launch.recall_http_mounted).toBe(false);
    expect(Array.isArray(snapshot.body.agents)).toBe(true);
    expect(Array.isArray(snapshot.body.next_actions)).toBe(true);
    for (const item of snapshot.body.work_items || []) {
      if (item.source === "workforce_assignment" && item.source_status === "assigned") {
        expect(item.founder_status).toBe("assigned");
        expect(item.founder_status).not.toBe("running");
      }
    }
  });

  test("security dashboard shows findings without inventing GO or applying remediations", async ({ page }) => {
    await page.getByRole("link", { name: "Säkerhet", exact: true }).first().click();
    await expect(page.getByRole("heading", { name: "Säkerhetsöversikt" })).toBeVisible({ timeout: 8000 });
    await expect(page.getByTestId("security-first-cycle")).toContainText("Första cykelns GO = false");
    await expect(page.getByTestId("security-restrictions")).toBeVisible();
    await expect(page.getByTestId("security-findings")).toBeVisible();
    await expect(page.getByTestId("security-limitations")).toContainText("Does not apply remediations");

    const dashboard = await page.evaluate(async () => {
      const res = await fetch("/api/mainai/security/dashboard", { credentials: "include" });
      return { status: res.status, body: await res.json() };
    });
    expect(dashboard.status).toBe(200);
    expect(dashboard.body.first_cycle_go).toBe(false);
    expect(dashboard.body.blocks_first_cycle).toBe(false);
    expect(dashboard.body.invented_policy).toBe(false);
    expect(dashboard.body.autonomous_privilege_escalation).toBe(false);
  });
});
