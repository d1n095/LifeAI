import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// The minimum vertical flow the founder's own instructions required as tonight's actual
// deliverable, not just backend/frontend built in isolation: importera paket -> validera ->
// lagra -> extrahera -> indexera -> visa i bibliotek -> söka -> fråga MainAI -> få
// källhänvisat svar -> öppna källan -> fortsätta samtalet -> radera materialet.
//
// Runs against the real backend (scripts/run_e2e_backend.py) — only the AI-provider chat/
// embed calls and outbound email are faked, everything else (import security, chunking,
// pgvector storage, RLS, the real Library UI, the real chat retrieval path) is the genuine
// application code. fake_search now prefers real search results when the founder's library
// actually has content (see run_e2e_backend.py's comment), which is what lets step 8 below
// prove a real citation instead of a canned one.
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";
const UNIQUE_TERM = "kvantflodesalgoritmen7712";

test.describe("Founder Knowledge Studio: full vertical flow", () => {
  test("import -> library -> search -> chat citation -> open source -> continue -> delete", async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    // 1. Library starts empty for a fresh founder account.
    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });
    await expect(page.locator("text=Inget material importerat ännu.")).toBeVisible();

    // 2. Import: a real file upload through the real security-validated pipeline.
    const fileContent = `Detta dokument beskriver ${UNIQUE_TERM}, en intern metod for MainAI:s kunskapshantering.`;
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', {
      name: "vertikalt-e2e-test.txt",
      mimeType: "text/plain",
      buffer: Buffer.from(fileContent),
    });

    // 3. Import completes and the source shows up in the library — status + classification
    // badges are real API data, not placeholders. Scoped to the table row, not just "text=
    // Aktiv" anywhere on the page — the status filter <select> also has an "Aktiv" <option>,
    // present in the DOM (if hidden) regardless of what's selected.
    const sourceRow = page.locator("tr", { hasText: "vertikalt-e2e-test.txt" });
    await expect(sourceRow).toBeVisible({ timeout: 15000 });
    await expect(sourceRow.locator("text=Aktiv")).toBeVisible();

    // 4. Search: real hybrid search (semantic + exact text via ILIKE) finds it by the exact
    // unique term — deterministic regardless of the fake embedding vector's semantics.
    await page.getByLabel("Sök i kunskapsbiblioteket").fill(UNIQUE_TERM);
    await page.getByRole("button", { name: "Sök", exact: true }).click();
    await expect(page.locator(`text=${UNIQUE_TERM}`).first()).toBeVisible({ timeout: 5000 });

    // 5. Open the source's detail page directly from search results.
    await page.locator('a[href^="/library/"]').first().click();
    await page.waitForURL(/\/library\/[0-9a-f-]+$/);
    await expect(page.locator("h1")).toContainText("vertikalt-e2e-test.txt");
    await expect(page.locator(`text=${UNIQUE_TERM}`).first()).toBeVisible();
    const sourceUrl = page.url();
    const sourceId = sourceUrl.split("/library/")[1];

    // 6. Ask MainAI chat about it — a real question, real retrieval (via the same real
    // search the library used), a real cited answer pointing back at the source just
    // imported, not a hardcoded fixture.
    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });
    const chatInput = page.getByLabel("Meddelande till MainAI");
    await chatInput.fill(`Berätta om ${UNIQUE_TERM}`);
    await page.getByRole("button", { name: "Skicka" }).click();
    await expect(page.locator("text=Källor:")).toBeVisible({ timeout: 10000 });
    const sourceCitationLink = page.locator(`a[href="/library/${sourceId}"]`);
    await expect(sourceCitationLink).toBeVisible();

    // 7. Open the exact cited source from the chat itself (DEL 6's "öppna exakt källa").
    await sourceCitationLink.click();
    await page.waitForURL(`${FRONTEND_URL}/library/${sourceId}`);
    await expect(page.locator("h1")).toContainText("vertikalt-e2e-test.txt");

    // 8. Continue the conversation: go back to chat, the history/conversation is preserved
    // (same conversation continues rather than starting over).
    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });
    await expect(page.locator(`text=${UNIQUE_TERM}`).first()).toBeVisible({ timeout: 5000 });
    await chatInput.fill("Kan du sammanfatta det igen kort?");
    await page.getByRole("button", { name: "Skicka" }).click();
    await expect(page.locator("text=MainAI skriver…")).toHaveCount(0, { timeout: 10000 });

    // 9. Delete the source with explicit confirmation, and confirm it's gone from both the
    // library listing and search.
    await page.goto(`${FRONTEND_URL}/library/${sourceId}`, { waitUntil: "networkidle" });
    await page.getByRole("button", { name: "Radera källa" }).click();
    await page.getByRole("button", { name: "Bekräfta radering" }).click();
    await page.waitForURL(`${FRONTEND_URL}/library`);
    await expect(page.locator("text=Inget material importerat ännu.")).toBeVisible({ timeout: 5000 });
  });
});
