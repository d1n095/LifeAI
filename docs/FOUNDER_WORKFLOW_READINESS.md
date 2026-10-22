# Founder-interface and autonomous-workflow readiness

Isolated P3 successor of frozen `#296` @ `2aeaa00efe4ffe4c5264b725d28e2e97b3bfa7f0`
(parent `#295` @ `97e2b04c9babf3c30e3067fb9e8282a82b071be4`).
Does **not** amend `#296`/`#295`/`#294`, compose Codex transport/grants, enable Recall, merge, or claim first-cycle GO.

Machine-readable flags: `GET /api/mainai/founder-ops/snapshot` (`first_cycle_go: false`).

## P3 remediations (this successor)

Claude's `#295` exam returned RETURN_TO_BUILDER. This batch closes the five
remaining independently confirmed defects without a new feature module.

1. R1 / P1: self-hosted `next start` does not populate `request.ip`. The proxy
   captures the Node TCP peer and forwards that identity. PX-9/10/11 run against
   the real standalone stack: forged XFF cannot split a peer; distinct peers and
   surfaces keep distinct keys. Falling back to `unknown` is a failure, not a fix.
2. D4: dashboard and Founder-status never claim backend reachable or STOP
   inactive unless current evidence says so. Missing `observability_failed` is
   unknown, not inactive. A failed snapshot poll clears stale reachable copy.
3. D5: `BLOCKED` always carries at least one named blocker. Empty BLOCKED is
   rewritten to `blocked_without_named_cause` and cannot be shown as BLOCKED (0).
4. N1: Founder questions are not claims. Mentioning first-cycle GO does not
   fabricate disagreement; assertive false GO/write/recall/verified claims still do.
5. N4: a historical `dispatched` event is not current acknowledgment. Only a live
   queued/claimed job is `acknowledged`. Agent `dispatched` status stays current.

Preserved: R2–R6, FO-6 paid-retry suppression, FO-7, Founder-only authorization,
and the frozen `#296` security diagnostics module.

## P2 remediations (frozen `#295`)

Claude's correction gate against `#294` `27e945d` (evidence `27c91b4e`). Equivalent
executable cases live in `test_founder_workflow_p2.py` and the proxy identity tests.
This package does not claim to have run Claude's local suite.

1. R1 / PX-9/10/11: proxy overwrites forwarding headers with a trusted client IP.
   Rate-limit keys use that IP (or user+surface). Forged XFF cannot spoof; one
   Founder surface or client cannot lock the others.
2. R2: independent verification binds to the task's actual commit SHA. A later
   FAIL / `invalidates_verification_id` revokes PASS.
3. R3: a failed paid `dispatch_task` reverts `dispatched` → `created` so retry is
   recoverable without a second in-flight prompt.
4. R4: Founder `cancel_requested` / `cancelled` survives a worker crash (not
   overwritten to `failed`).
5. R5: ambiguous publication records a reconcilable identity. Prior `work_result`
   / `finalized` checkpoints are reused across jobs so a crash cannot buy a
   second paid run or a second remote effect.
6. R6: safe-mode proposals verify in the isolated workspace (hydrated for tests)
   and can pass verification there.
7. D4/D5: dashboard health uses the snapshot for backend/STOP. Cancelled work is
   not a failed blocker. `blocker_counts` separates operational / readiness / STOP.
8. N1/N4: unavailable live evidence does not fabricate disagreement. Agent
   `dispatched` is `acknowledged`, not running.

Preserved from `#294`: FO-6 typed push-reject, FO-7, D1–D3, Founder authorization,
STOP fail-closed, FC-4, and security controls.

## P1 remediations (frozen `#294`)

1. FO-6: `WorktreePushRejected` maps to typed `PushRejectedError`. Stored evidence
   suppresses paid replan. Worker crashes finalize `task_execution` instead of leaving
   the task silently running.
2. D1: `independently_verified` requires a durable independent exact-SHA PASS receipt
   (`examiner ≠ builder`). Checkpoint CI/tests and builder `verification_passed` events
   stay `completed`.
3. D2/D3: Agent dispatch takes `FOR UPDATE` and moves to `dispatched` before the paid
   call. Delegation uses `pg_advisory_xact_lock` plus a task row lock.
4. D4/D5: Snapshot counts do not double-count a task and its job. STOP/readiness
   unknown fail closed in HTTP and the dashboard.
5. Communication uses the live snapshot. Recall disagreement is Recall-specific.
   Assignment is never treated as execution.
6. Same-origin proxy rejects encoded traversal and strips client forwarding headers.
7. Safe mode writes code-agent proposals to an isolated workspace unless
   `mainai_allow_live_checkout_writes` is Founder-authorized.
8. Founder Authority on the existing snapshot: transparent restrictions, Founder-approved
   sensitive settings, independent enforcement, truthful operational status.

## What this workstream closed

1. Founder work dashboard (`/admin/founder-ops` plus dashboard summary) shows durable
   agents, assignments, execution status, dependencies, blockers, next actions, live
   spend, and review outcomes. Backend rows are the only source of truth.
2. Status vocabulary distinguishes `queued`, `assigned`, `acknowledged`, `running`,
   `waiting_on_dependency`, `failed`, `completed`, `independently_verified`,
   `cancelled`.
   Assignment issuance is never treated as running. A queued job with `locked_by` is
   acknowledged, not running. Self-review is completed, not independently verified.
3. Independent ready work is surfaced when siblings wait on CI or another task.
   The snapshot does not invent activity and does not duplicate live assignments.
   Stored dependency cycles are fail-closed blockers and refuse new assignments.
4. MainAI chat applies a Founder communication policy: factual accuracy over
   agreement, explicit uncertainty, no fabricated progress, no redundant copies,
   concise simple answers, and disagreement when evidence contradicts the Founder.
   MainAI remains Founder-identity, not a customer-facing bot.
5. Granted `approval_required` tasks with an `approval_granted` event are not
   re-prompted. First-cycle GO stays false.
6. Shell skip-link, existing responsive nav, and Founder routes stay on the current
   architecture (workforce + `mainai_execution` + founder-ops). No parallel agent system.

## Safe local startup

Same profile as `docs/APPLICATION_READINESS.md`. Do not set provider API keys,
`GITHUB_TOKEN`, or `PERSONAL_RECALL_SYSTEM_KEK_B64`. Keep GitHub writes and
activation mode false. Recall HTTP is not mounted.

## Remaining launch blockers

Unchanged from `#292`. This PR does not close them.

- Codex Git transport / worktree credentials: CRED-1/2/3, E2E-3
- Production grant interface: GRANT-PROD
- Production publication history/symlink/gitlink: TA-12/TA-13/PUB-2
- Production `evaluate_check_runs` skipped-as-pass: CI-6
- `#280` RETURN_TO_BUILDER dual `0089`
- First-cycle GO remains **false** until those compose with a PASS-reviewed
  activation-security replacement
