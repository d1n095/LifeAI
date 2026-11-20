import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// Founder-only shell routes (/library, /projects, /) beyond the baseline auth.spec.ts flow.
// /documents is intentionally only a redirect to /library now (Life Library upload
// consolidation), so this spec exercises the current library surface rather than the removed
// documents upload UI. These tests simulate failures via page.route() (a real network response
// the browser actually receives, not a mocked React state) so they fail against the pre-fix
// code and pass against the fix.
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

test.describe("shell pages: empty states and mutation error handling", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
  });

  test("library: empty state renders, and a failed delete shows a visible error instead of silently doing nothing", async ({
    page,
  }) => {
    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });

    // Empty state (a fresh founder account before any upload) — real backend, no mock.
    await expect(page.locator("text=Inget material importerat ännu.")).toBeVisible();

    // Upload one real document so there's a row with a delete button to fail on.
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', {
      name: "e2e-error-handling-test.txt",
      mimeType: "text/plain",
      buffer: Buffer.from("Innehall for PRIO 4-felhanteringstestet."),
    });
    const row = page.locator("tr", { hasText: "e2e-error-handling-test.txt" });
    await expect(row).toBeVisible({ timeout: 15000 });

    // Force the DELETE call to fail server-side (a real HTTP 500 response reaching the
    // browser) and confirm the UI surfaces it via role="alert" rather than staying silent.
    await page.route("**/api/library/*", (route) => {
      if (route.request().method() === "DELETE") {
        route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"Simulerat serverfel"}' });
      } else {
        route.continue();
      }
    });
    await row.getByRole("button", { name: "Ta bort e2e-error-handling-test.txt" }).click();
    await row.getByRole("button", { name: "Bekräfta" }).click();
    await expect(row.getByRole("alert").filter({ hasText: "Simulerat serverfel" })).toBeVisible({ timeout: 5000 });
    // The row must still be there — a failed delete must not have been assumed to succeed.
    await expect(row).toBeVisible();

    // Clean up through the real API so this spec leaves the shared founder library empty.
    await page.unroute("**/api/library/*");
    await row.getByRole("button", { name: "Bekräfta" }).click();
    await expect(row).toHaveCount(0, { timeout: 5000 });
  });

  test("projects: empty states render, and a failed create shows a visible error instead of silently doing nothing", async ({
    page,
  }) => {
    await page.goto(`${FRONTEND_URL}/projects`, { waitUntil: "networkidle" });

    await expect(page.locator("text=Inga projekt ännu.")).toBeVisible();
    await expect(page.locator("text=Inga uppgifter ännu.")).toBeVisible();

    await page.route("**/api/projects", (route) => {
      if (route.request().method() === "POST") {
        route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"Simulerat serverfel"}' });
      } else {
        route.continue();
      }
    });
    await page.getByLabel("Nytt projektnamn").fill("Ett projekt som aldrig sparas");
    await page.getByRole("button", { name: "Lägg till" }).first().click();
    // getByRole("alert") also matches Next.js's own <next-route-announcer> (an always-present
    // framework a11y element with an internal alert role and empty text) — scope to text, same
    // convention as auth.spec.ts's baseline flow.
    await expect(page.getByRole("alert").filter({ hasText: "Simulerat serverfel" })).toBeVisible({ timeout: 5000 });
    // Failed create: the list must still show the empty state, and the typed name must not
    // have been thrown away as if it had been submitted successfully.
    await expect(page.locator("text=Inga projekt ännu.")).toBeVisible();
    await expect(page.getByLabel("Nytt projektnamn")).toHaveValue("Ett projekt som aldrig sparas");
  });

  test("dashboard ('/'): a cold-start backend failure is announced via role=alert, not shown as silent success", async ({
    page,
  }) => {
    await page.route("**/api/documents", (route) => {
      if (route.request().method() === "GET") {
        route.fulfill({ status: 500, contentType: "application/json", body: '{"detail":"Simulerat serverfel"}' });
      } else {
        route.continue();
      }
    });
    await page.goto(`${FRONTEND_URL}/`, { waitUntil: "networkidle" });
    await expect(page.getByRole("alert").filter({ hasText: "Simulerat serverfel" })).toBeVisible({ timeout: 5000 });
    // The page must not claim "allt system operationellt" is actually true while also
    // showing the error — both render, screen-reader users get the alert either way.
  });
});
