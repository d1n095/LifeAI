import { test, expect } from "@playwright/test";
import { FRONTEND_URL } from "./urls";
import { loginViaUi } from "./helpers";

// Closes the one founder-journey step Workstream 4 (PR #494) flagged as not driven through a
// real browser: goal creation -> AI plan proposal -> task creation -> founder approval. #494's
// own raw page.request attempt couldn't carry the app's in-page CSRF token; this uses the real
// UI form/buttons instead, the same way a founder actually would.
//
// Requires scripts/ci/run_e2e_backend.py's PLANNER_SYSTEM_PROMPT branch in fake_chat() (added
// alongside this spec) — without it, the planner's AI call gets the generic conversational
// fake reply, which app/mainai_execution/planner.py's _parse_plan_specs() correctly rejects as
// invalid JSON, and this test's "Skapa plan (AI)" step fails with a visible error banner
// instead of producing a task to approve.
test.describe.configure({ mode: "serial" });
test.use({ baseURL: FRONTEND_URL });

const FOUNDER_EMAIL = process.env.FOUNDER_EMAIL || "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.FOUNDER_PASSWORD || "TestFounderPassword123!";

test("a founder can create a goal, get an AI plan, and approve the resulting task", async ({ page }) => {
  test.setTimeout(60_000);
  await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
  await expect(page.getByRole("button", { name: "Logga ut" })).toBeVisible({ timeout: 30_000 });

  await page.goto(`${FRONTEND_URL}/admin/mainai-execution`, { waitUntil: "networkidle" });
  await expect(page.getByRole("heading", { name: "MainAI Execution Loop" })).toBeVisible();

  const title = `E2E goal ${Date.now()}`;
  await page.getByPlaceholder("Titel").fill(title);
  await page.getByPlaceholder("Fullständig instruktion").fill("E2E-driven goal for the approval round-trip spec.");
  await page.getByRole("button", { name: "Skapa mål" }).click();

  const goalCell = page.getByRole("cell", { name: title });
  await expect(goalCell).toBeVisible({ timeout: 15_000 });
  await goalCell.click();

  await page.getByRole("button", { name: "Skapa plan (AI)" }).click();
  // A real AI call (even to a faked provider) takes a moment; wait for the plan to land
  // rather than a fixed sleep racing the response.
  await expect(page.getByText(/^Plan v1/)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Granskning (läs)")).toBeVisible();

  await page.locator("li").filter({ hasText: "Granskning (läs)" }).first().click();
  const approveBtn = page.getByRole("button", { name: "Godkänn" });
  await expect(approveBtn).toBeVisible({ timeout: 10_000 });
  await approveBtn.click();

  // Approval is real: the goal transitions out of "pending" once a task is dispatched for it.
  // Actual execution correctly stays gated behind the separate `task_execution` runtime
  // capability (first_cycle_go) — this spec verifies the approval step, not unrestricted
  // autonomous execution, which is deliberately out of scope here.
  await expect(page.getByText("Status: running")).toBeVisible({ timeout: 10_000 });
});
