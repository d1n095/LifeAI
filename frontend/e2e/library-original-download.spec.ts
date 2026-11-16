import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// Library "download original" control (Blueprint §16.6). The backend endpoint
// GET /api/library/:id/original (PR #329) streams the untouched original bytes back to the
// founder as an attachment, octet-stream, nosniff; this UI is the matching frontend control
// on the source-detail page. Written against the real auth/session flow (loginViaUi), with
// page.route() mocking only the two Library endpoints this page hits so the detail shape and
// the download outcome (success / integrity-gated failure) are deterministic — the same
// convention library-upload-queue.spec.ts uses for its error/timing-sensitive checks.
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

const SOURCE_ID = "11111111-1111-1111-1111-111111111111";
const ORIGINAL_FILENAME = "founder-notes.txt";
const ORIGINAL_BYTES = "exakt de bytes grundaren laddade upp\n";

function detail(overrides: Record<string, unknown> = {}) {
  return {
    id: SOURCE_ID,
    title: "Grundarens anteckningar",
    source: "upload",
    media_type: "text/plain",
    original_filename: ORIGINAL_FILENAME,
    category: null,
    classification: "general",
    active_truth_status: "active",
    status: "indexed",
    chunk_count: 1,
    checksum: "a".repeat(64),
    project_id: null,
    version_number: 1,
    error_message: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    imported_at: new Date().toISOString(),
    media_duration_seconds: null,
    transcript_provider: null,
    versions: [],
    relationships: [],
    chunk_preview: ["ett utdrag"],
    claims: [],
    segments: [],
    ...overrides,
  };
}

// Routes only the detail GET and the /original GET for SOURCE_ID; everything else
// (auth/me, the upload-queue job poll, the listLibrary call this page makes for its
// relationship picker, ...) falls through to the real backend untouched. `onOriginal`
// receives the intercepted /original request so a test can both choose the response and
// assert on how the browser asked for it (same-origin, authenticated).
async function routeLibrary(
  page: import("@playwright/test").Page,
  opts: {
    detailOverrides?: Record<string, unknown>;
    original: { status: number; body: string; contentType?: string; disposition?: string };
    onOriginal?: (req: import("@playwright/test").Request) => void;
  }
) {
  await page.route("**/api/library/**", async (route) => {
    const req = route.request();
    const pathname = new URL(req.url()).pathname;
    if (pathname === `/api/library/${SOURCE_ID}/original` && req.method() === "GET") {
      opts.onOriginal?.(req);
      await route.fulfill({
        status: opts.original.status,
        contentType: opts.original.contentType ?? "application/octet-stream",
        headers: opts.original.disposition ? { "Content-Disposition": opts.original.disposition } : {},
        body: opts.original.body,
      });
      return;
    }
    if (pathname === `/api/library/${SOURCE_ID}` && req.method() === "GET") {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(detail(opts.detailOverrides)),
      });
      return;
    }
    await route.continue();
  });
}

test.describe("Library: download original", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
  });

  test("downloads the untouched original with the server-chosen filename, over the authenticated same-origin session", async ({
    page,
  }) => {
    let originalRequestUrl = "";
    let originalRequestHadCookie = false;
    await routeLibrary(page, {
      original: {
        status: 200,
        body: ORIGINAL_BYTES,
        // Both forms the backend's _content_disposition() emits — the RFC 5987 filename*
        // (preferred by the parser) and the quoted ASCII fallback.
        disposition: `attachment; filename="${ORIGINAL_FILENAME}"; filename*=UTF-8''${encodeURIComponent(ORIGINAL_FILENAME)}`,
      },
      onOriginal: (req) => {
        originalRequestUrl = req.url();
        // The HttpOnly session cookie must ride along (credentials:"include") — this is a
        // founder-only endpoint, not a public link. If the control were a bare anonymous
        // <a href> to a cross-origin URL the cookie would not be here.
        originalRequestHadCookie = Boolean(req.headers()["cookie"]);
      },
    });

    await page.goto(`${FRONTEND_URL}/library/${SOURCE_ID}`, { waitUntil: "networkidle" });

    const downloadButton = page.getByRole("button", { name: `Ladda ner originalfilen ${ORIGINAL_FILENAME}` });
    await expect(downloadButton).toBeVisible();

    const [download] = await Promise.all([page.waitForEvent("download"), downloadButton.click()]);
    expect(download.suggestedFilename()).toBe(ORIGINAL_FILENAME);

    // Authorization boundary: the request went same-origin (never leaked to a cross-origin
    // host) and carried the session cookie.
    expect(originalRequestUrl.startsWith(FRONTEND_URL)).toBe(true);
    expect(originalRequestHadCookie).toBe(true);

    // No error surfaced on the happy path.
    await expect(page.getByRole("alert")).toHaveCount(0);
  });

  test("an integrity-gated failure (404/503) shows an inline error and triggers no download", async ({ page }) => {
    let downloadFired = false;
    page.on("download", () => {
      downloadFired = true;
    });
    await routeLibrary(page, {
      original: { status: 404, contentType: "application/json", body: JSON.stringify({ detail: "Originalet hittades inte." }) },
    });

    await page.goto(`${FRONTEND_URL}/library/${SOURCE_ID}`, { waitUntil: "networkidle" });

    const downloadButton = page.getByRole("button", { name: `Ladda ner originalfilen ${ORIGINAL_FILENAME}` });
    await downloadButton.click();

    // The backend's already-Swedish HTTPException detail is surfaced verbatim, inline,
    // without navigating the founder away to a raw error page.
    await expect(page.getByRole("alert").filter({ hasText: "Originalet hittades inte." })).toBeVisible({ timeout: 5000 });
    expect(page.url()).toBe(`${FRONTEND_URL}/library/${SOURCE_ID}`);
    expect(downloadFired).toBe(false);
  });

  test("no download control is shown when the source has no stored original", async ({ page }) => {
    await routeLibrary(page, {
      detailOverrides: { original_filename: null },
      original: { status: 404, body: "" },
    });

    await page.goto(`${FRONTEND_URL}/library/${SOURCE_ID}`, { waitUntil: "networkidle" });

    // Page rendered (title present) but the download control is gated off.
    await expect(page.getByRole("heading", { name: "Grundarens anteckningar" })).toBeVisible();
    await expect(page.getByRole("button", { name: /Ladda ner original/ })).toHaveCount(0);
  });
});
