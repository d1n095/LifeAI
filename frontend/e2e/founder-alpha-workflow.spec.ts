import { test, expect } from "@playwright/test";

// Founder Alpha UI acceptance: the same workflow the API harness proves, but driven through
// the real Next.js screens a founder actually sees. This exists because an API-only proof can
// pass while the UI shows a stale status, a blank bubble, or an unreachable-backend error.
//
// Run against a live backend + Ollama (see backend/scripts/acceptance/founder_workflow.py for
// the supervised version that also restarts the processes):
//   E2E_INTERNAL_API_URL=http://127.0.0.1:8010 npx playwright test e2e/founder-alpha-workflow.spec.ts
import { FRONTEND_URL } from "./urls";
import { loginViaUi } from "./helpers";

const FOUNDER_EMAIL = process.env.FOUNDER_EMAIL || "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.FOUNDER_PASSWORD || "TestFounderPassword123!";
const TOKEN = `ui${Date.now()}`;

async function login(page: import("@playwright/test").Page) {
  await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
  // loginViaUi only clicks; without waiting for the session to land, a following goto()
  // races the login request and arrives on /chat while still unauthenticated.
  await expect(page.getByRole("button", { name: "Logga ut" })).toBeVisible({ timeout: 30_000 });
}

test.describe.configure({ mode: "serial" });
test.use({ baseURL: FRONTEND_URL });

test("a founder can log in and the dashboard is connected to the backend", async ({ page }) => {
  test.setTimeout(90_000);
  await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
  await expect(page).toHaveURL(new RegExp(`${FRONTEND_URL.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/?$`));
  await expect(page.getByRole("heading", { name: /God kväll/ })).toBeVisible({ timeout: 30_000 });
  // Next.js renders its own empty route-announcer with role="alert", so a bare alert count
  // proves nothing. The dashboard's actual failure UI is this message; the loaded document
  // count proves the API answered.
  await expect(page.getByText("Kunde inte nå backend", { exact: false })).toHaveCount(0);
  await expect(page.getByText("Indexerade dokument")).toBeVisible();
});

test("chat shows a real local-model answer, not a placeholder", async ({ page }) => {
  test.setTimeout(120_000);
  await login(page);
  await page.goto(`${FRONTEND_URL}/chat`);
  await page.getByPlaceholder("Fråga MainAI vad som helst…").fill(
    "Vad är återställningstiden för produktionsmiljön? Svara kort."
  );
  await page.getByRole("button", { name: "Skicka" }).click();
  // The seeded/local corpus contains this fact; a canned or empty reply cannot satisfy it.
  await expect(page.getByText(/45 minuter/).last()).toBeVisible({ timeout: 90_000 });
});

test("an uploaded document is visible in the library and retrievable with its fact", async ({ page }) => {
  test.setTimeout(180_000);
  await login(page);
  await page.goto(`${FRONTEND_URL}/library`);
  const title = `UI workflow ${TOKEN}`;
  await page.locator('input[type="file"]').first().setInputFiles({
    name: `${title}.md`,
    mimeType: "text/markdown",
    buffer: Buffer.from(
      `Dokument ${TOKEN}. Eskaleringsvägen för kritiska incidenter är 12 minuter. ` +
        `Ansvarig är den tekniska plattformsansvarige.`
    ),
  });
  // A link, not any matching text: the upload queue also renders the filename while it is
  // still uploading, so asserting on text alone passed even when the import failed.
  await expect(page.getByRole("link", { name: new RegExp(title) })).toBeVisible({ timeout: 150_000 });

  await page.getByPlaceholder("Sök semantiskt eller på exakt text…").fill(
    "Eskaleringsvägen för kritiska incidenter"
  );
  await page.getByRole("button", { name: "Sök", exact: true }).click();
  await expect(page.getByText(/12 minuter/).first()).toBeVisible({ timeout: 90_000 });
});

test("workbench analysis can be saved as an authorized decision", async ({ page }) => {
  test.setTimeout(180_000);
  await login(page);
  await page.goto(`${FRONTEND_URL}/workbench`);
  await page.locator("#wb-question").fill("Ska lansering 2 ske i två steg?");
  await page.getByRole("button", { name: "Starta analys" }).click();
  await expect(page.getByText("Slutsats", { exact: true })).toBeVisible({ timeout: 90_000 });
  await page.locator("#wb-label").selectOption("decision");
  await page.getByRole("button", { name: "Spara som kunskapsobjekt" }).click();
  await expect(page.getByText("Öppna det sparade objektet")).toBeVisible({ timeout: 60_000 });
});

test("the jobs screen submits a corpus review and shows its progress", async ({ page }) => {
  test.setTimeout(180_000);
  await login(page);
  await page.goto(`${FRONTEND_URL}/admin/jobs`);
  const documentCheckboxes = page.locator('input[type="checkbox"]');
  await expect(documentCheckboxes.first()).toBeVisible({ timeout: 60_000 });
  await documentCheckboxes.first().check();
  await page.getByRole("button", { name: /Starta granskning/ }).click();
  const jobRow = page.getByText("corpus_review").first();
  await expect(jobRow).toBeVisible({ timeout: 60_000 });
  await jobRow.click();
  await expect(page.getByText(/Förlopp:/).first()).toBeVisible({ timeout: 60_000 });
});
