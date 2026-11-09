import { expect, test } from "@playwright/test";
import { FRONTEND_URL } from "../playwright.config";
import { loginViaUi } from "./helpers";

const FOUNDER_EMAIL = "founder@lifeos.local";
const FOUNDER_PASSWORD = process.env.E2E_FOUNDER_PASSWORD || "TestFounderPassword123!";
const HEAD_SHA = "a".repeat(40);

test("GitHub Operations Center renders the API's exact PR, CI, dependency, and freshness evidence", async ({ page }) => {
  const snapshot = {
    schema_version: 1,
    repository: {
      full_name: "d1n095/LifeAI",
      url: "https://github.com/d1n095/LifeAI",
      default_branch: "main",
      availability: "available",
    },
    observation: {
      freshness: "live",
      observed_at: "2026-11-09T12:00:00Z",
      cache_age_seconds: 0,
      error_category: null,
      pagination: { open_pull_requests_truncated: false },
    },
    pull_requests: [
      {
        number: 411,
        title: "Synthetic failing draft",
        state: "open",
        draft: true,
        author: "builder",
        url: "https://github.com/d1n095/LifeAI/pull/411",
        head_branch: "feature/synthetic",
        head_sha: HEAD_SHA,
        base_branch: "main",
        base_sha: "b".repeat(40),
        updated_at: "2026-11-09T11:00:00Z",
        dependencies: [
          {
            number: 410,
            state: "open",
            blocked: true,
            url: "https://github.com/d1n095/LifeAI/pull/410",
            source: "explicit_dependency_in_pull_request_body",
          },
        ],
        dependency_evidence: {
          state: "explicit_dependencies_found",
          method: "explicit_pull_request_body",
          source: "https://github.com/d1n095/LifeAI/pull/411",
        },
        mergeability: { state: "UNKNOWN", mergeable: null, mergeable_state: null },
        review_state: "approval_stale_head",
        ci_state: "failed",
        head_sha_changed_since_list: false,
        head_sha_changed_during_detail: false,
        head_sha_rechecked_at: "2026-11-09T12:00:00Z",
        requested_reviewers: ["reviewer"],
        reviews: [{
          id: 2,
          author: "reviewer",
          state: "APPROVED",
          commit_sha: "b".repeat(40),
          submitted_at: "2026-11-09T11:00:00Z",
          url: "https://github.com/d1n095/LifeAI/pull/411#pullrequestreview-2",
        }],
        comments: [
          {
            id: 1,
            author: "reviewer",
            created_at: "2026-11-09T11:30:00Z",
            body: "Synthetic untrusted comment",
            url: "https://github.com/d1n095/LifeAI/pull/411#issuecomment-1",
            untrusted_content: true,
          },
        ],
        checks: [
          {
            name: "Backend tests",
            status: "completed",
            conclusion: "failure",
            state: "failed",
            is_latest_for_check: true,
            url: "https://github.com/d1n095/LifeAI/actions/runs/411",
          },
          {
            name: "Historical attempt",
            status: "completed",
            conclusion: "failure",
            state: "failed",
            is_latest_for_check: false,
            url: "https://github.com/d1n095/LifeAI/actions/runs/410",
          },
        ],
        workflow_runs: [
          {
            id: 411,
            name: "CI",
            status: "completed",
            conclusion: "failure",
            url: "https://github.com/d1n095/LifeAI/actions/runs/411",
            head_sha: HEAD_SHA,
            is_latest_for_workflow: true,
            jobs: [
              {
                name: "Backend",
                status: "completed",
                conclusion: "failure",
                state: "failed",
                url: "https://github.com/d1n095/LifeAI/actions/runs/411/job/1",
              },
            ],
          },
        ],
        latest_commit: {
          sha: HEAD_SHA,
          message: "Synthetic commit",
          committed_at: "2026-11-09T10:00:00Z",
          url: `https://github.com/d1n095/LifeAI/commit/${HEAD_SHA}`,
        },
        detail_errors: [],
      },
    ],
    closed_pull_requests: [],
    capabilities: [],
    agents: [],
    assignments: [],
    agent_results: [],
    goals: [],
    tasks: [],
    work_candidates: [],
    decision_debt: { items: [] },
    blockers: [
      {
        kind: "failed_check",
        status: "failed",
        title: "PR #411: Backend tests failed",
        source: "https://github.com/d1n095/LifeAI/actions/runs/411",
        evidence: "GitHub check-run observation",
      },
      {
        kind: "pull_request_dependency",
        status: "blocked",
        title: "PR #411 depends on PR #410",
        source: "https://github.com/d1n095/LifeAI/pull/410",
        evidence: "explicit_dependency_in_pull_request_body",
      },
    ],
    recommendations: [],
    activity: [
      {
        kind: "pull_request_updated",
        title: "PR #411: Synthetic failing draft",
        timestamp: "2026-11-09T11:00:00Z",
        source: "https://github.com/d1n095/LifeAI/pull/411",
        head_sha: HEAD_SHA,
      },
    ],
  };

  await loginViaUi(page, FRONTEND_URL, FOUNDER_EMAIL, FOUNDER_PASSWORD);
  await page.route("**/api/github-operations/snapshot*", (route) =>
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(snapshot) }),
  );
  await page.getByRole("link", { name: "GitHub Operations", exact: true }).click();
  await page.waitForURL(`${FRONTEND_URL}/github-operations`);

  await expect(page.getByTestId("snapshot-freshness")).toContainText("LIVE");
  await expect(page.getByTestId("pull-request-411")).toContainText(`HEAD ${HEAD_SHA}`);
  await expect(page.getByTestId("pull-request-411")).toContainText("DRAFT");
  await expect(page.getByTestId("pull-request-411")).toContainText("Backend tests");
  await expect(page.getByTestId("pull-request-411")).toContainText("CI failed");
  await expect(page.getByTestId("pull-request-411")).toContainText("1 failed checks");
  await expect(page.getByTestId("pull-request-411")).toContainText("Historical attempt");
  await expect(page.getByTestId("pull-request-411")).toContainText("approval_stale_head");
  await expect(page.getByTestId("pull-request-411")).toContainText("PR #410 · open");
  await expect(page.getByTestId("pull-request-411")).toContainText("Synthetic untrusted comment");
  await expect(page.getByRole("link", { name: "Open source" }).first()).toHaveAttribute(
    "href",
    "https://github.com/d1n095/LifeAI/actions/runs/411",
  );
});
