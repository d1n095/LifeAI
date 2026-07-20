import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// STEG 12/13: audio/video import v1 + its UI. Uploads a real .mp3 through the real
// security-validated pipeline against the real E2E backend (scripts/run_e2e_backend.py fakes
// only the chat/embedding provider calls — app/providers/transcription.py's
// MockTranscriptionProvider is never faked, it's already offline/deterministic by design, so
// this exercises the genuine STEG 12 pipeline end to end).
//
// The uploaded bytes carry a real MP3 signature (so app/rag/media_import.py's magic-byte
// check accepts them — the same fixture shape the backend pytest suite already uses in
// tests/backend/test_media_import.py) but are NOT a real decodable audio stream — there's no
// ffmpeg/encoder available to produce one here. A real browser's <audio> element correctly
// refuses to play that and fires onerror, which app/(shell)/library/[id]/page.tsx handles via
// its error/retry UI — itself one of STEG 13's explicit requirements, so this spec verifies
// THAT behavior rather than genuine playback, which this fixture can't honestly exercise.
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";
const MOBILE_VIEWPORT = { width: 390, height: 844 };

// A unique suffix per call keeps each upload's checksum distinct — /api/library/import is
// deliberately idempotent by content checksum (see app/routers/library.py), so two uploads
// with byte-identical content return the SAME already-completed job/source instead of
// creating a second one, which would make two tests in this file collide.
function uniqueMp3(): Buffer {
  const suffix = Math.random().toString(36).slice(2, 10);
  return Buffer.concat([Buffer.from(`ID3\x03\x00\x00\x00\x00\x00\x00${suffix}`), Buffer.alloc(64)]);
}

test.describe("Founder Knowledge Studio: audio import, playback UI, timestamped citations", () => {
  test("upload mp3 -> timestamped transcript -> error/retry player UI -> chat citation carries a timestamp", async ({
    page,
  }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    // 1. Upload through the real Library import UI.
    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', {
      name: "e2e-inspelning.mp3",
      mimeType: "audio/mpeg",
      buffer: uniqueMp3(),
    });

    const sourceRow = page.locator("tr", { hasText: "e2e-inspelning.mp3" });
    await expect(sourceRow).toBeVisible({ timeout: 15000 });

    // 2. Open the source detail page — the player and timestamped transcript section only
    // render for a media source (source.segments.length > 0, see
    // app/(shell)/library/[id]/page.tsx).
    await sourceRow.locator("a").first().click();
    await page.waitForURL(/\/library\/[0-9a-f-]+$/);
    const sourceId = page.url().split("/library/")[1];

    await expect(page.locator("#media-heading")).toBeVisible({ timeout: 10000 });

    // 3. The transcript list shows at least one timestamped segment with a "play from here"
    // control — MockTranscriptionProvider's honest placeholder text, not invented speech.
    const transcriptButton = page.locator('button[aria-label^="Spela från"]').first();
    await expect(transcriptButton).toBeVisible();
    await expect(page.locator("text=0:00")).toBeVisible();

    // 4. Transcript search filters the segment list client-side.
    const searchBox = page.getByLabel("Sök i transkriptet");
    await searchBox.fill("ett ord som definitivt inte finns i transkriptet xyz123");
    await expect(page.locator("text=Inga träffar i transkriptet.")).toBeVisible();
    await searchBox.fill("");
    await expect(transcriptButton).toBeVisible();

    // 5. Error/retry: the synthetic fixture isn't real decodable audio, so the player
    // genuinely fails to load it — proving the error/retry UI (a STEG 13 requirement) reacts
    // to a real failure, not a simulated one. "Försök igen" re-renders the <audio> element.
    const errorAlert = page.locator('[role="alert"]', { hasText: "Mediefilen kunde inte laddas" });
    await expect(errorAlert).toBeVisible({ timeout: 10000 });
    await page.getByRole("button", { name: "Försök igen" }).click();
    await expect(page.locator("audio")).toBeAttached();

    // 6. Ask MainAI about the recording — the citation for a media source must carry a
    // timestamp (?t=) in its link, proving the timestamp flows all the way from the
    // transcript chunk through retrieval into the chat citation (app/routers/chat.py's
    // SourceRef, see app/(shell)/chat/page.tsx's citation rendering).
    await page.goto(`${FRONTEND_URL}/chat`, { waitUntil: "networkidle" });
    const chatInput = page.getByLabel("Meddelande till MainAI");
    await chatInput.fill("Vad sager inspelningen e2e-inspelning?");
    await page.getByRole("button", { name: "Skicka" }).click();
    await expect(page.locator("text=Källor:")).toBeVisible({ timeout: 10000 });

    const citationLink = page.locator(`a[href^="/library/${sourceId}"]`).first();
    await expect(citationLink).toBeVisible();
    const href = await citationLink.getAttribute("href");
    expect(href).toContain("?t=");

    // 7. Following the citation deep-link lands on the source with the timestamp in the URL
    // (app/(shell)/library/[id]/page.tsx reads ?t= to seek the player to that moment).
    await citationLink.click();
    await page.waitForURL(new RegExp(`/library/${sourceId}\\?t=`));
    await expect(page.locator("#media-heading")).toBeVisible();

    // Cleanup: this founder-only architecture shares ONE founder account across every spec
    // file in the suite (see e2e/founder-knowledge-studio.spec.ts's own "starts empty"
    // assumption) — leaving this upload behind would break that and shell-pages.spec.ts's
    // "Inga dokument uppladdade ännu." empty-state assertion for whatever spec runs next.
    await page.getByRole("button", { name: "Radera källa" }).click();
    await page.getByRole("button", { name: "Bekräfta radering" }).click();
    await page.waitForURL(`${FRONTEND_URL}/library`);
  });

  test("/library/[id] media player and transcript are usable at a phone viewport", async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    await page.goto(`${FRONTEND_URL}/library`, { waitUntil: "networkidle" });
    await page.setInputFiles('input[aria-label="Importera till kunskapsbiblioteket"]', {
      name: "e2e-mobil-inspelning.mp3",
      mimeType: "audio/mpeg",
      buffer: uniqueMp3(),
    });
    const sourceRow = page.locator("tr", { hasText: "e2e-mobil-inspelning.mp3" });
    await expect(sourceRow).toBeVisible({ timeout: 15000 });
    await sourceRow.locator("a").first().click();
    await page.waitForURL(/\/library\/[0-9a-f-]+$/);

    await page.setViewportSize(MOBILE_VIEWPORT);
    await expect(page.locator("#media-heading")).toBeVisible();
    // Either the player or its error/retry fallback must be present — never neither (that
    // would mean the section silently rendered nothing at this viewport).
    await expect(page.locator("audio").or(page.locator('[role="alert"]', { hasText: "Mediefilen" }))).toBeVisible({
      timeout: 10000,
    });

    const hasHorizontalOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
    );
    expect(hasHorizontalOverflow).toBe(false);

    // Cleanup — see the identical comment in the test above: the shared founder account must
    // be left empty for whatever spec runs next.
    await page.getByRole("button", { name: "Radera källa" }).click();
    await page.getByRole("button", { name: "Bekräfta radering" }).click();
    await page.waitForURL(`${FRONTEND_URL}/library`);
  });
});
