import { test, expect } from "@playwright/test";
import { mkdirSync } from "node:fs";
import { FRONTEND_URL } from "./urls";
import { loginViaUi } from "./helpers";

// Founder Console UI acceptance. The point is not that the components exist in the tree, but
// that a founder can load the console in Chromium, see backend-measured status, and get a real
// model answer from the console's own composer.
const FOUNDER_EMAIL = process.env.FOUNDER_EMAIL || "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.FOUNDER_PASSWORD || "TestFounderPassword123!";

async function login(page: import("@playwright/test").Page) {
  await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
  await expect(page.getByRole("button", { name: "Logga ut" })).toBeVisible({ timeout: 30_000 });
}

test.describe.configure({ mode: "serial" });
test.use({ baseURL: FRONTEND_URL });

test("the Founder Console renders the Living Core and real backend status", async ({ page }) => {
  test.setTimeout(90_000);
  await login(page);
  await expect(page.getByRole("heading", { name: "Founder Console" })).toBeVisible({ timeout: 30_000 });

  const core = page.getByTestId("living-core");
  await expect(core).toBeVisible();
  // The Core must be traceable to an operational state the page derived, never to a random
  // animation: resting/reasoning/creating/degraded are the only values the console can set.
  await expect(core).toHaveAttribute("data-core-state", /resting|reasoning|creating|degraded/);

  await expect(page.getByText("MainAI · driftstatus")).toBeVisible();
  await expect(page.getByText("Lokal modell")).toBeVisible();
  await expect(page.getByText("Kräver ditt godkännande")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Aktiva beslut" })).toBeVisible();
  await expect(page.getByText("Kunde inte nå backend", { exact: false })).toHaveCount(0);

  // Evidence artefact from a real browser session (not a hand-made screenshot).
  mkdirSync("../docs/evidence", { recursive: true });
  await page.screenshot({ path: "../docs/evidence/founder-console.png", fullPage: true });
});

test("the console composer returns a real local-model answer with sources", async ({ page }) => {
  test.setTimeout(120_000);
  await login(page);
  await page.getByPlaceholder("Fråga MainAI…").fill(
    "Vad är återställningstiden för produktionsmiljön? Svara kort."
  );
  await page.getByRole("button", { name: "Skicka" }).click();
  await expect(page.getByText(/45 minuter/).last()).toBeVisible({ timeout: 90_000 });
  // A reply without provenance would be indistinguishable from a canned answer.
  await expect(page.getByText("Källor:", { exact: false })).toBeVisible({ timeout: 30_000 });
});

test("reduced motion is a static fallback, not the animated Core", async ({ page }) => {
  test.setTimeout(90_000);
  await page.emulateMedia({ reducedMotion: "reduce" });
  await login(page);
  const core = page.getByTestId("living-core");
  await expect(core).toHaveAttribute("data-motion", "reduced");
  await page.waitForTimeout(600);
  // A reduced-motion user must still see the Core and the same factual status.
  await expect(core).toBeVisible();
  await expect(page.getByText("MainAI · driftstatus")).toBeVisible();
});
