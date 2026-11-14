import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

function makeOpsStatus(overrides: Record<string, unknown> = {}) {
  return {
    worker_reachable: true,
    queue_length: 0,
    running_jobs: 0,
    oldest_pending_age_seconds: null,
    failed_last_24h: 0,
    storage_writable: true,
    free_disk_bytes: 1024 * 1024 * 1024,
    last_heartbeat_at: "2026-11-14T06:00:00Z",
    ...overrides,
  };
}

test.describe("Life Library processing status", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
  });

  test("distinguishes worker reachability from persisted running-job counts", async ({ page }) => {
    await page.route("**/api/library/ops/status", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(
          makeOpsStatus({
            worker_reachable: false,
            queue_length: 3,
            running_jobs: 2,
          })
        ),
      })
    );

    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });

    const status = page.getByLabel("Bearbetningsstatus");
    await expect(status).toContainText("Worker onåbar");
    await expect(status).toContainText("Jobb registrerade som kör: 2");
    await expect(status).toContainText("Senaste registrerade heartbeat:");
    await expect(status).toContainText("Status hämtad:");
  });

  test("shows unavailable status and lets the Founder retry successfully", async ({ page }) => {
    let requests = 0;
    await page.route("**/api/library/ops/status", async (route) => {
      requests += 1;
      if (requests === 1) {
        await route.fulfill({ status: 503, contentType: "application/json", body: '{"detail":"unavailable"}' });
        return;
      }
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(makeOpsStatus()),
      });
    });

    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });

    const status = page.getByLabel("Bearbetningsstatus");
    await expect(status).toContainText("Worker- och köstatus är okända");
    await status.getByRole("button", { name: "Försök igen" }).click();
    await expect(status).toContainText("Worker aktiv");
    await expect(status).toContainText("Status hämtad:");
  });
});
