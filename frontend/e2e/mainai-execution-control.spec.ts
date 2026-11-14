import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";
const GOAL_ID = "00000000-0000-0000-0000-000000000101";
const TASK_ID = "00000000-0000-0000-0000-000000000102";
const JOB_ID = "00000000-0000-0000-0000-000000000103";

const goal = {
  id: GOAL_ID,
  title: "Founder-owned goal",
  original_instruction: "Implement only the reviewed fix and verify it.",
  status: "running",
  risk_level: "medium",
  approval_policy: "standard_repo_work",
  current_plan_version: 1,
  created_at: "2026-11-14T10:00:00Z",
  started_at: "2026-11-14T10:01:00Z",
  completed_at: null,
  final_outcome: null,
};

const task = {
  id: TASK_ID,
  goal_id: GOAL_ID,
  plan_id: "00000000-0000-0000-0000-000000000104",
  description: "Implement the reviewed fix",
  task_type: "repo_edit",
  status: "running",
  priority: 1,
  risk_level: "medium",
  approval_required: true,
  verification_plan: [{ kind: "targeted_tests", target: "tests/test_reviewed_fix.py" }],
  attempts: 1,
  max_attempts: 3,
  blocker_reason: null,
  mainai_job_id: JOB_ID,
  next_retry_at: null,
  liveness: "running",
  created_at: "2026-11-14T10:01:00Z",
  started_at: "2026-11-14T10:02:00Z",
  completed_at: null,
  events: [],
  checkpoints: [
    {
      id: "00000000-0000-0000-0000-000000000105",
      plan_version: 1,
      executor_state: {
        job_id: JOB_ID,
        step: "verification",
        verification: {
          passed: true,
          steps: [{ kind: "targeted_tests", passed: true, detail: { stdout_tail: "PRIVATE_RAW_TEST_OUTPUT_SENTINEL" } }],
        },
      },
      test_status: "passed",
      ci_status: null,
      blocker: null,
      created_at: "2026-11-14T10:03:00Z",
    },
  ],
  depends_on: [],
  approval_granted: true,
};

function makeJob(status: "running" | "completed" = "running", cancelRequested = false) {
  return {
    id: JOB_ID,
    owner_id: null,
    job_type: "task_execution",
    status,
    created_at: "2026-11-14T10:02:00Z",
    started_at: "2026-11-14T10:02:00Z",
    last_heartbeat_at: "2026-11-14T10:03:30Z",
    completed_at: status === "completed" ? "2026-11-14T10:04:00Z" : null,
    progress_current: 1,
    progress_total: 3,
    current_phase: "repo_edit",
    public_message: "Working at the last durable checkpoint.",
    error_category: null,
    retry_count: 0,
    max_retries: 3,
    input_refs: [{ type: "mainai_task", id: TASK_ID }],
    output_refs: [],
    provider: null,
    model: null,
    cancel_requested: cancelRequested,
    cancel_acknowledged: false,
    created_by: "founder",
    events: [],
  };
}

test.describe("MainAI execution control surface", () => {
  test.beforeEach(async ({ page }) => {
    await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
    await page.waitForURL(FRONTEND_URL + "/", { timeout: 5000 });
    await page.route("**/api/mainai/execution/goals**", async (route) => {
      const url = new URL(route.request().url());
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(url.pathname === `/api/mainai/execution/goals/${GOAL_ID}` ? { ...goal, plan: null, tasks: [task] } : [goal]),
      });
    });
    await page.route(`**/api/mainai/execution/tasks/${TASK_ID}/waits`, (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    await page.route(`**/api/mainai/execution/tasks/${TASK_ID}`, (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(task) })
    );
  });

  test("shows the persisted task brief, durable job heartbeat and bounded checkpoint evidence", async ({ page }) => {
    await page.route(`**/api/mainai/jobs/${JOB_ID}`, (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(makeJob()) })
    );

    await page.goto(`${FRONTEND_URL}/admin/mainai-execution`, { waitUntil: "networkidle" });
    await page.getByRole("row", { name: /Founder-owned goal/ }).click();
    await page.locator("li").filter({ hasText: "repo_edit" }).first().click();

    await expect(page.getByLabel("Uppdragsbrief")).toContainText("Implement only the reviewed fix and verify it.");
    await expect(page.getByLabel("Uppdragsbrief")).toContainText("Inga dokument-/källreferenser registrerade.");
    await expect(page.getByLabel("Uppdragsbrief")).toContainText("Granulär leverantörs-/agentprofil: unavailable");
    await expect(page.getByLabel("Körningssession och kapacitet")).toContainText("Senaste jobb-heartbeat:");
    await expect(page.getByLabel("Körningssession och kapacitet")).toContainText("Worker-ID och lease-utgång: unavailable");
    await expect(page.getByText("verification", { exact: true }).last()).toBeVisible();
    await expect(page.getByText("Kontroll targeted_tests: godkänd")).toBeVisible();
    await expect(page.getByText("PRIVATE_RAW_TEST_OUTPUT_SENTINEL")).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Begär avbrytning" })).toBeVisible();
    await expect(page.getByText(/inget manuellt resume-API/)).toBeVisible();
  });

  test("uses the owner-scoped job cancellation request only after Founder confirmation", async ({ page }) => {
    let cancelCalls = 0;
    await page.route(`**/api/mainai/jobs/${JOB_ID}`, async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify(makeJob("running", cancelCalls > 0)),
      });
    });
    await page.route(`**/api/mainai/jobs/${JOB_ID}/cancel`, async (route) => {
      cancelCalls += 1;
      await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(makeJob("running", true)) });
    });
    page.on("dialog", (dialog) => dialog.accept());

    await page.goto(`${FRONTEND_URL}/admin/mainai-execution`, { waitUntil: "networkidle" });
    await page.getByRole("row", { name: /Founder-owned goal/ }).click();
    await page.locator("li").filter({ hasText: "repo_edit" }).first().click();
    await page.getByRole("button", { name: "Begär avbrytning" }).click();

    await expect.poll(() => cancelCalls).toBe(1);
    await expect(page.getByText("begärd, väntar på worker")).toBeVisible();
    await expect(page.getByRole("button", { name: "Avbrytning begärd…" })).toBeDisabled();
  });

  test("does not offer cancellation for a completed job and identifies unsupported session controls", async ({ page }) => {
    await page.route(`**/api/mainai/jobs/${JOB_ID}`, (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(makeJob("completed")) })
    );

    await page.goto(`${FRONTEND_URL}/admin/mainai-execution`, { waitUntil: "networkidle" });
    await page.getByRole("row", { name: /Founder-owned goal/ }).click();
    await page.locator("li").filter({ hasText: "repo_edit" }).first().click();

    await expect(page.getByLabel("Körningssession och kapacitet")).toContainText(
      "Sessionstatus i extern agent-UI: unavailable — no authorized execution adapter."
    );
    await expect(page.getByRole("button", { name: "Begär avbrytning" })).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Start/paus/fortsätt" })).toHaveCount(0);
  });
});
