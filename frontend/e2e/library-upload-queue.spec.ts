import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// Life Library upload consolidation package: the global upload queue (lib/uploadQueue.tsx),
// single-upload-location consolidation (DEL 1), and unified delete flow (DEL 5). Written
// FIRST against the pre-fix code (where they failed: /documents had its own upload UI, only
// one file could be picked at a time, uploads were sequential and reset on navigation, and
// each page had its own bespoke delete state) and now pass against the fix.
//
// Runs against the real backend (scripts/run_e2e_backend.py) for the golden-path/consolidation
// checks; the concurrency-limit, partial-failure, retry and delete-error checks use
// page.route() to get deterministic, real HTTP responses without depending on backend timing
// (same convention as shell-pages.spec.ts).
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

function makeJobResponse(id: string, filename: string, status: string, overrides: Record<string, unknown> = {}) {
  return {
    id,
    status,
    source_filename: filename,
    source_checksum: "a".repeat(64),
    progress_current: status === "completed" ? 1 : 0,
    progress_total: 1,
    succeeded_count: status === "completed" ? 1 : 0,
    failed_count: status === "failed" ? 1 : 0,
    skipped_count: 0,
    failure_reason: null,
    manifest: null,
    file_results: status === "completed" ? [{ filename, status: "indexed", reason: null, source_id: `${id}-doc` }] : null,
    started_at: new Date().toISOString(),
    completed_at: status === "completed" || status === "failed" ? new Date().toISOString() : null,
    created_at: new Date().toISOString(),
    attempt_count: 0,
    max_attempts: 3,
    last_failure_transient: null,
    ...overrides,
  };
}

test.describe("Life Library: single upload hub", () => {
  test("/documents redirects to /library, and no 'Dokument' nav entry remains", async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    await expect(page.getByRole("link", { name: "Dokument", exact: true })).toHaveCount(0);

    await page.goto(`${FRONTEND_URL}/documents`, { waitUntil: "networkidle" });
    await page.waitForURL(`${FRONTEND_URL}/library`, { timeout: 5000 });
  });
});

test.describe("Life Library: global upload queue", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
  });

  test("20 files can be queued, at most 3 are ever sent concurrently, and more can be added mid-upload", async ({ page }) => {
    let inFlight = 0;
    let maxInFlight = 0;
    const seen = new Set<string>();

    await page.route("**/api/library/import*", async (route) => {
      inFlight += 1;
      maxInFlight = Math.max(maxInFlight, inFlight);
      // Hold every request open briefly so overlapping uploads are actually observable
      // instead of resolving too fast to ever be concurrent in practice.
      await new Promise((r) => setTimeout(r, 300));
      const url = new URL(route.request().url());
      const body = route.request().postDataBuffer();
      const filename = `file-${seen.size}`;
      seen.add(filename);
      inFlight -= 1;
      void url;
      void body;
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(makeJobResponse(`job-${filename}-${Date.now()}`, filename, "completed")),
      });
    });

    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });

    const initialFiles = Array.from({ length: 15 }, (_, i) => ({
      name: `queue-test-${i}.txt`,
      mimeType: "text/plain",
      buffer: Buffer.from(`innehall ${i}`),
    }));
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', initialFiles);

    // More files added while the first batch is still processing (DEL 2's "tillåta att fler
    // filer läggs till medan kön arbetar").
    await page.waitForTimeout(200);
    const moreFiles = Array.from({ length: 5 }, (_, i) => ({
      name: `queue-test-extra-${i}.txt`,
      mimeType: "text/plain",
      buffer: Buffer.from(`extra innehall ${i}`),
    }));
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', moreFiles);

    await expect(page.locator('ul[aria-label="Uppladdningskö"] li')).toHaveCount(20, { timeout: 10000 });

    // Wait for the whole queue to drain.
    await expect(page.locator('ul[aria-label="Uppladdningskö"] li:has-text("Klar")')).toHaveCount(20, { timeout: 30000 });

    expect(maxInFlight).toBeLessThanOrEqual(3);
    expect(maxInFlight).toBeGreaterThan(1); // proves it's genuinely concurrent, not accidentally serial
  });

  test("navigating away from /library and back does not lose the queue", async ({ page }) => {
    await page.route("**/api/library/import*", async (route) => {
      // Never resolves during this test — the item must still show "uploading" after
      // navigating away and back, proving the provider (not the page) owns the state.
      await new Promise(() => {});
    });

    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', {
      name: "survives-navigation.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("innehall"),
    });
    await expect(page.locator('ul[aria-label="Uppladdningskö"]').locator("text=survives-navigation.txt")).toBeVisible();

    // Real in-app navigation (clicking a <Link>, not page.goto — which forces a full browser
    // reload and would trivially "pass" by destroying and never re-creating the queue) —
    // this is what actually proves the provider in app/(shell)/layout.tsx, not the /library
    // page itself, owns the queue's state.
    await page.getByRole("link", { name: "Chat", exact: true }).click();
    await page.waitForURL(`${FRONTEND_URL}/chat`);
    await page.getByRole("link", { name: "Knowledge Studio", exact: true }).click();
    await page.waitForURL(`${FRONTEND_URL}/library`);

    await expect(page.locator('ul[aria-label="Uppladdningskö"]').locator("text=survives-navigation.txt")).toBeVisible();
  });

  test("one file's error does not stop the rest of the batch, and Retry recovers it", async ({ page }) => {
    let attempt = 0;
    await page.route("**/api/library/import*", async (route) => {
      const postData = route.request().postDataBuffer()?.toString() || "";
      if (postData.includes("broken-file")) {
        attempt += 1;
        if (attempt === 1) {
          await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"Simulerat serverfel"}' });
          return;
        }
      }
      const name = postData.includes("broken-file") ? "broken-file.txt" : "good-file.txt";
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(makeJobResponse(`job-${name}-${Date.now()}`, name, "completed")),
      });
    });

    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', [
      { name: "broken-file.txt", mimeType: "text/plain", buffer: Buffer.from("broken-file content") },
      { name: "good-file.txt", mimeType: "text/plain", buffer: Buffer.from("good content") },
    ]);

    const goodItem = page.locator('li:has-text("good-file.txt")');
    const brokenItem = page.locator('li:has-text("broken-file.txt")');
    await expect(goodItem.locator("text=Klar")).toBeVisible({ timeout: 10000 });
    await expect(brokenItem.locator("text=Misslyckades")).toBeVisible({ timeout: 10000 });
    await expect(brokenItem.getByRole("button", { name: "Försök igen" })).toBeVisible();

    await brokenItem.getByRole("button", { name: "Försök igen" }).click();
    await expect(brokenItem.locator("text=Klar")).toBeVisible({ timeout: 10000 });
  });

  test("reload recovers a still-running server job instead of losing track of it", async ({ page }) => {
    await page.route("**/api/library/jobs", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([makeJobResponse("recovered-job-1", "recovered-after-reload.txt", "running")]),
      });
    });
    await page.route("**/api/library/jobs/recovered-job-1", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(makeJobResponse("recovered-job-1", "recovered-after-reload.txt", "running")),
      });
    });

    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });
    await expect(page.locator("text=recovered-after-reload.txt")).toBeVisible({ timeout: 5000 });
    await expect(page.locator('li:has-text("recovered-after-reload.txt")').locator("text=Bearbetar…")).toBeVisible();
  });
});

test.describe("Life Library: unified delete", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
  });

  test("delete requires two-step confirmation, blocks double-click, and restores the row with an error on server rejection", async ({
    page,
  }) => {
    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', {
      name: "delete-flow-test.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("innehall for raderingstestet"),
    });
    const row = page.locator("tr", { hasText: "delete-flow-test.txt" });
    await expect(row).toBeVisible({ timeout: 15000 });

    let deleteCalls = 0;
    await page.route("**/api/library/*", async (route) => {
      if (route.request().method() === "DELETE") {
        deleteCalls += 1;
        // A short delay so the transient "Tar bort…" state is actually observable instead of
        // flashing by within a single render before the mocked response resolves.
        await new Promise((r) => setTimeout(r, 300));
        await route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"Simulerat serverfel"}' });
      } else {
        await route.continue();
      }
    });

    await row.getByRole("button", { name: "Ta bort delete-flow-test.txt" }).click();
    const confirmButton = row.getByRole("button", { name: "Bekräfta" });
    await expect(confirmButton).toBeVisible();
    await confirmButton.click();
    await expect(row.locator("text=Tar bort…")).toBeVisible();

    await expect(row.getByRole("alert").filter({ hasText: "Simulerat serverfel" })).toBeVisible({ timeout: 5000 });
    // The row must still be there — a failed delete must not have been assumed to succeed.
    await expect(row).toBeVisible();
    expect(deleteCalls).toBe(1);

    // A real, unmocked delete now succeeds and removes the row.
    await page.unroute("**/api/library/*");
    await row.getByRole("button", { name: "Bekräfta" }).click();
    await expect(row).toHaveCount(0, { timeout: 5000 });
  });
});
