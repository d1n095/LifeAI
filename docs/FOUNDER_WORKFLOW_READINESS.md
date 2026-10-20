# Founder-interface and autonomous-workflow readiness

Isolated successor of frozen `#292` @ `5e3cd01ac3a2cded69ef19bfa074d15196d3afaa`.
Does **not** amend `#292`, compose Codex transport/grants, enable Recall, merge, or claim first-cycle GO.

Machine-readable flags: `GET /api/mainai/founder-ops/snapshot` (`first_cycle_go: false`).

## What this workstream closed

1. Founder work dashboard (`/admin/founder-ops` plus dashboard summary) shows durable
   agents, assignments, execution status, dependencies, blockers, next actions, live
   spend, and review outcomes. Backend rows are the only source of truth.
2. Status vocabulary distinguishes `queued`, `assigned`, `acknowledged`, `running`,
   `waiting_on_dependency`, `failed`, `completed`, `independently_verified`.
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
