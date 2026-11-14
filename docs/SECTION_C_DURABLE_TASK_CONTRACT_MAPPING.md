# Section C — Durable Task Handoff Contract: mapping to existing infrastructure

**Continuous Build Directive, Section C** asked for: "a durable, resumable task contract
(task ID, outcome, owner, repo scope, source commit, dependencies, current state/last step,
acceptance criteria, test evidence, retry counters/failure classification, cancellation/stop
state); resume from verified checkpoints after restart; reuse existing task/execution storage."

**Finding: this already exists, built and reviewed across several prior migrations (0025–0028,
0032–0036, 0057–0059).** This document is the deliverable itself — a field-by-field mapping
from the directive's exact requirement list to the real, already-running model, so a future
builder (human or MainAI) does not reinvent a parallel contract out of not knowing this one is
here. No new schema, no new table, no new migration. This is the directive's own instruction —
"reuse existing task/execution storage" — applied literally: the honest result of reusing
something that already fully covers the requirement is a map of it, not a rewrite.

Companion document: `docs/BRANCH_REGISTRY.md`'s 2026-11-14 entries record the session that
produced this mapping (PRs #395/#396, the GitHub-evidence-reading work that motivated looking
at this layer in the first place) and the still-open architecture question about whether
GitHub-evidence-derived work deserves its own founder-reviewable staging table — a related but
distinct question from the one this document answers.

## The contract, field by field

| Directive requirement | Where it lives today | Citation |
|---|---|---|
| **Task ID** | `MainAITask.id` (UUID primary key) | `app/models/mainai_execution.py` |
| **Outcome** | `MainAITask.status` (terminal: `completed`/`failed`/`cancelled`); goal-level `MainAIGoal.final_outcome` (free text, written once a goal reaches a terminal status) | `app/models/mainai_execution.py` |
| **Owner** | `MainAITask.owner_id`, `MainAIGoal.owner_id` — both FK `users.id ON DELETE CASCADE`, consistently indexed | `app/models/mainai_execution.py` |
| **Repo scope** | `ExecutionAuthorizationEnvelope.repository_identity` + `.authorized_paths` — the founder-granted ceiling every task-level binding must fit inside; never created directly, only via `app.execution_envelopes.service.authorize_execution_scope()` | `app/models/execution_envelope.py` |
| **Source commit** | Recorded in `MainAICheckpoint.executor_state` (a structured, closed shape documented in `app/mainai_execution/checkpoint.py` — branch/worktree/commit SHA/last-completed-step/pending-step) | `app/models/mainai_execution.py`, `app/mainai_execution/checkpoint.py` |
| **Dependencies** | `MainAITaskDependency` — one edge per `(task_id, depends_on_task_id)`; a task cannot become `ready` until every dependency is `completed` (`app/mainai_execution/graph.py`); self-edges rejected by a DB CHECK, multi-row cycles rejected by the planner's own DFS at plan-creation time | `app/models/mainai_execution.py` |
| **Current state / last step** | `MainAITaskEvent` — one append-only row per transition (18 event types: `created`→…→`completed`/`failed`/`cancelled`, plus V0.3's `wait_*`/`cancel_*`/`auto_recovery_triggered`/`lesson_conflict_detected`); DB trigger enforces INSERT-only, same pattern as `MainAIJobEvent`. `MainAICheckpoint` holds the actual resumable state (see "Resume" below) | `app/models/mainai_execution.py` |
| **Acceptance criteria** | `MainAITask.verification_plan` — structured, closed-vocabulary steps (e.g. `[{"kind": "targeted_tests", "target": "..."}]`), never free text the executor could reinterpret as "passed" on its own say-so | `app/models/mainai_execution.py`, `app/mainai_execution/verify.py` |
| **Test evidence** | `MainAICheckpoint.test_status` / `.ci_status`; `MainAITaskEvent` rows of type `verification_started`/`verification_passed`/`verification_failed` | `app/models/mainai_execution.py` |
| **Retry counters** | `MainAITask.attempts` / `.max_attempts` / `.next_retry_at` (task level); `MainAIJob.retry_count` / `.max_retries` (runtime level) — backoff computed once, by `app/jobs/retry.py`'s `compute_backoff_seconds()`, reused by both layers rather than two policies | `app/models/mainai_execution.py`, `app/models/mainai_job.py` |
| **Failure classification** | V0.2's recovery pipeline — `app/mainai_execution/recovery_classifier.py` classifies a dead job's outcome before any takeover decision; `MainAITaskStatus.retryable_failed` vs. `.failed` is itself a structural classification, not a single bucket | `app/mainai_execution/recovery_classifier.py`, `app/models/mainai_execution.py` |
| **Cancellation / stop state** | `MainAIJob.cancel_requested` / `.cancel_acknowledged` (runtime level, cooperative — a worker checks and acknowledges, never force-killed); `MainAITaskEventType.cancel_requested`/`.cancelling` mirrors the same split at the task level (`app/mainai_execution/cancellation.py`) | `app/models/mainai_job.py`, `app/models/mainai_execution.py` |

## Resume from verified checkpoints after restart

`MainAICheckpoint` is append-only; resume always reads the **latest** row for a goal
(`app/mainai_execution/checkpoint.py`) — an old row is never mutated, only superseded by a
newer insert. The Work Driver (`app/development_driver/service.py`, `run_driver()`) verifies
plan-hash identity before resuming from a checkpoint, so a resume can never silently continue
against a plan that has since changed underneath it. Dead-attempt recovery
(`app/mainai_execution/recovery_takeover.py`) locks the goal row before deciding whether a
takeover is safe, re-verifying (never trusting a cached read) whether the goal has ever had an
execution-authorization envelope — a race-free check, not a convention.

## Execution continuity (the directive's adjacent, same-session requirement)

The directive also asked to "investigate supported unattended execution (queue polling,
bounded leases, heartbeats, expired-lease recovery, idempotent claiming, safe retries with
backoff, concurrency limits, daily budgets, kill-switch enforcement)." Also already built:

| Requirement | Where | Citation |
|---|---|---|
| Queue polling / idempotent claiming | `app/jobs/mainai_job_lease.py` — `FOR UPDATE SKIP LOCKED` + lease TTL | `app/jobs/mainai_job_lease.py` |
| Bounded leases / expired-lease recovery | `MainAIJob.lease_expires_at` + `.lease_generation` (fencing token, bumped on every claim/reclaim — an old holder's write after losing a lease is rejected, not merely discouraged) | `app/models/mainai_job.py` |
| Heartbeats | `MainAIJob.last_heartbeat_at`; `MainAIJobEventType.heartbeat` | `app/models/mainai_job.py` |
| Safe retries with backoff | `MainAIJob.retry_count`/`.max_retries`; `app/jobs/retry.py`'s `compute_backoff_seconds()` | `app/jobs/retry.py` |
| Idempotent claiming | `MainAIJob.idempotency_key` | `app/models/mainai_job.py` |
| Concurrency limits | `app/agent_coordination/service.py` | — |
| Daily budgets | `app/models/provider_spend.py`, migration 0059/0060's provider-spend authorization (a distinct, founder-granted budget for billed planning calls — never silently inherited from a goal's ordinary approval) | `app/models/provider_spend.py` |
| Kill-switch enforcement | `app/workforce/kill_switch.py` | `app/workforce/kill_switch.py` |

## What this means for "Life planning/delegating/inspecting/evaluating engineering work"

The directive's Section B asks MainAI to eventually "track exact commits/test results/
checkpoints, recognize failures and request retries... preserve the Founder's requirements/
priorities, pause on unresolved authority/security decisions." Every one of those reads
directly off the table above — nothing in Section B's tracking/inspection requirement needs new
storage either, only new CODE that reads this already-durable contract (e.g. the GitHub
evidence-reading work in #395/#396) and, eventually, a founder-reviewed decision about whether
externally-sourced (non-founder-knowledge) work proposals get their own staging layer before
ever reaching `MainAIGoal`/`MainAITask` at all — see `docs/BRANCH_REGISTRY.md`'s open question
on that, which this document deliberately does not resolve.
