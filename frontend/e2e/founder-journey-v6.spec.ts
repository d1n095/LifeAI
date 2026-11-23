import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "./urls";
import { loginViaUi } from "./helpers";

// V6 browser leg: real Next.js proxy, real FastAPI, real PostgreSQL/pgvector and the real
// durable worker. The API-level journey covers approval/execution/cancel; this spec proves
// the same journey is reachable through the browser, including live SSE rendering and a
// citation link to the exact uploaded source.
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

test.describe("V6 Founder Alpha browser journey", () => {
  test("protected routes reject an unauthenticated browser", async ({ page }) => {
    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });
    await page.waitForURL(/\/login/, { timeout: 10_000 }).catch(() => {});
    expect(page.url()).toContain("/login");
  });

  test("streaming, reload, ingestion, citation and supervised task", async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 10_000 });

    const marker = `V6UI-${Date.now()}`;
    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });
    const input = page.getByLabel("Meddelande till MainAI");
    await input.fill(`${marker}: svara kort.`);
    await page.getByRole("button", { name: "Skicka" }).click();

    const transcript = page.locator('div[aria-live="polite"]');
    // The streaming UI shows the assistant's text while the request is still open; the
    // cancel button only exists during that window.
    await expect(page.getByRole("button", { name: "Avbryt", exact: true })).toBeVisible({
      timeout: 15_000,
    });
    await expect(transcript.getByText(`${marker}: svara kort.`, { exact: true })).toHaveCount(1);
    // Budget for a real local model: a small model on 2 CPU cores needs ~30 s for a
    // retrieval-sized prompt, so a 30 s window fails on latency rather than on behaviour.
    await expect(page.getByRole("button", { name: "Avbryt", exact: true })).toHaveCount(0, {
      timeout: 120_000,
    });

    // Reopen from durable history after a real page reload.
    await page.reload({ waitUntil: "networkidle" });
    await expect(transcript.getByText(`${marker}: svara kort.`, { exact: true })).toHaveCount(1, {
      timeout: 60_000,
    });

    // Upload through the real security-validated import path; the durable worker indexes it.
    const filename = `v6-ui-${Date.now()}.txt`;
    const knowledgeMarker = `V6UI-KNOWLEDGE-${Date.now()}`;
    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', {
      name: filename,
      mimeType: "text/plain",
      buffer: Buffer.from(
        `V6 browser acceptance source.\n${knowledgeMarker} ar en unik sokterm.\n`
      ),
    });
    const row = page.locator("tr", { hasText: filename });
    await expect(row).toBeVisible({ timeout: 30_000 });
    const sourceLink = row.locator('a[href^="/library/"]').first();
    const href = await sourceLink.getAttribute("href");
    expect(href).toMatch(/^\/library\/[0-9a-f-]+$/);

    // The document row exists as soon as the import is accepted; retrieval can only cite it
    // once the worker has actually indexed it. Waiting for the queue item to report Klar is
    // the real precondition — with a local embedding model indexing takes long enough that
    // asking immediately would race it (and cite nothing).
    const queueItem = page
      .getByLabel("Uppladdningskö")
      .getByRole("listitem")
      .filter({ hasText: filename });
    await expect(queueItem.getByText("Klar", { exact: true })).toBeVisible({ timeout: 60_000 });

    // Ask through the real chat/retrieval path and verify the exact uploaded source is cited.
    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });
    await page.getByLabel("Meddelande till MainAI").fill(`Berätta om ${knowledgeMarker}`);
    await page.getByRole("button", { name: "Skicka" }).click();
    await expect(page.locator("text=Källor:")).toBeVisible({ timeout: 120_000 });
    await expect(page.locator(`a[href="${href}"]`)).toBeVisible({ timeout: 10_000 });

    // Supervised agent task creation through the current founder-only UI.
    const taskTitle = `V6 UI supervised ${Date.now()}`;
    await page.goto(`${FRONTEND_URL}/admin/agents`, { waitUntil: "networkidle" });
    await page.getByPlaceholder("Titel").fill(taskTitle);
    await page
      .getByPlaceholder("Beskrivning av uppdraget…")
      .fill("Read-only isolated audit. Change no files.");
    await page
      .getByPlaceholder("Acceptanskriterier…")
      .fill("An audit result is recorded and no repository files are modified.");
    await page.getByRole("button", { name: "Skapa uppdrag" }).click();
    await expect(page.getByRole("cell", { name: taskTitle, exact: true }).first()).toBeVisible({
      timeout: 10_000,
    });
  });
});
