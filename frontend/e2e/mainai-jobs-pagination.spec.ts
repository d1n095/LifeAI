import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

// Founder re-review round (PR #36), fourth pass, finding M2: /admin/jobs' refreshJobs() had no
// guard against out-of-order responses. A poll-driven fetch for an OLD page could resolve AFTER
// a newer page's fetch and silently overwrite the UI with stale data. Fixed with a monotonic
// request-id guard in app/(shell)/admin/jobs/page.tsx. This test drives the exact race the
// founder asked for: "page 1 responds after page 2" — page 1's response is deliberately
// delayed past page 2's, and the UI must still show page 2's data when page 1's stale response
// finally arrives.
const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";

function makeJob(idSuffix: string, titleTag: string) {
  const now = new Date().toISOString();
  return {
    id: `00000000-0000-0000-0000-0000000000${idSuffix}`,
    owner_id: "00000000-0000-0000-0000-000000000001",
    job_type: `${titleTag}-review`,
    status: "queued",
    created_at: now,
    started_at: null,
    last_heartbeat_at: null,
    completed_at: null,
    progress_current: 0,
    progress_total: null,
    current_phase: null,
    public_message: null,
    error_category: null,
    retry_count: 0,
    max_retries: 3,
    input_refs: [],
    output_refs: [],
    provider: null,
    model: null,
    cancel_requested: false,
    cancel_acknowledged: false,
    created_by: "founder",
  };
}

const PAGE1_JOBS = Array.from({ length: 20 }, (_, i) => makeJob(String(i).padStart(2, "0"), "page1"));
const PAGE2_JOBS = Array.from({ length: 5 }, (_, i) => makeJob(`5${i}`, "page2-fresh"));

test.describe("/admin/jobs pagination: stale responses cannot overwrite a newer page", () => {
  test("page 1's deliberately delayed response arrives after page 2's and is discarded", async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });

    let page1RequestCount = 0;
    await page.route("**/api/mainai/jobs?limit=20&offset=0", async (route) => {
      page1RequestCount += 1;
      if (page1RequestCount === 1) {
        // The very first load: fulfil immediately with a FULL page so "Nästa" becomes enabled.
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PAGE1_JOBS) });
      } else {
        // The POLL_INTERVAL_MS poll re-fetching offset=0 (fired while page 1 was still the
        // current page) is deliberately delayed well past page 2's own response below --
        // simulating exactly "page 1 responds after page 2".
        await new Promise((resolve) => setTimeout(resolve, 3000));
        await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PAGE1_JOBS) });
      }
    });
    await page.route("**/api/mainai/jobs?limit=20&offset=20", async (route) => {
      // Resolves fast -- must win the race against the delayed, stale offset=0 response above.
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(PAGE2_JOBS) });
    });
    await page.route("**/api/documents**", async (route) => {
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) });
    });

    await page.goto(`${FRONTEND_URL}/admin/jobs`, { waitUntil: "networkidle" });
    await expect(page.locator("text=page1-review").first()).toBeVisible({ timeout: 8000 });

    // Let POLL_INTERVAL_MS (4000ms) elapse once while still on page 1, so the poll fires a
    // SECOND offset=0 request -- caught by the route handler above and held in flight (3s
    // delay) -- before we navigate away from page 1.
    await page.waitForTimeout(4300);
    expect(page1RequestCount).toBeGreaterThanOrEqual(2);

    // Navigate to page 2 WHILE that stale offset=0 request is still in flight.
    await page.getByRole("button", { name: "Nästa" }).click();
    await expect(page.locator("text=page2-fresh-review").first()).toBeVisible({ timeout: 8000 });

    // Give the deliberately-delayed stale offset=0 response time to finally arrive and attempt
    // to overwrite state.
    await page.waitForTimeout(3500);

    // The stale response must have been discarded by the monotonic request-id guard -- page 2's
    // data must still be what's on screen, not page 1's.
    await expect(page.locator("text=page2-fresh-review").first()).toBeVisible();
    await expect(page.locator("text=page1-review")).toHaveCount(0);
  });
});
