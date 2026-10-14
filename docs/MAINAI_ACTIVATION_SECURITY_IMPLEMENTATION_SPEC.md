# MainAI first-cycle activation security contract

Status: implementation specification only. This document grants no authority and changes no runtime behavior.

Base examined: `d5153eb1fa1727427a24a983c005b9b88b718b2c` (PR #270 registry freeze; its Path A product lineage includes PR #269).

## Invariants

1. `repo_edit`, remote push, and `open_pr` are three separate authority transitions.
2. A task approval is never push authority. A push grant is never PR authority.
3. CI success and independent review are evidence, never authority.
4. Authority is exact and conjunctive: every bound field must match; missing or unknown means deny.
5. A consumed, expired, cancelled, superseded, or mismatched grant can never be revived.
6. Activation always targets one Founder-approved repository and immutable base SHA.
7. Enabling GitHub connectivity never grants remote-write authority to legacy or unrelated goals.

## Minimal design decision

Use per-goal/per-task remote-write grants, not a dedicated worker.

The current worker, MainAI job leases, execution envelopes, task graph, worktree ownership records, provider-spend grants, and CI waits remain the only orchestration system. A dedicated activation worker would duplicate dispatch and leasing and still require durable authorization. Remote effects instead require explicit capabilities in the active execution envelope plus an effect-specific grant.

For the first cycle, every remote-writing goal must be envelope-governed. The legacy `never governed => Path A allowed` compatibility rule may continue for read/local-only work, but must never authorize `push` or `open_pr` when `GITHUB_WRITE_ENABLED=true`.

## Data model

### Extend `execution_authorization_envelopes`

Migration required. Reuse the existing `repository_identity` field and add immutable
activation authority fields:

- existing `repository_identity` is required for activation envelopes and normalized as `owner/repo`;
- `base_branch TEXT NOT NULL` for activation envelopes.
- `approved_base_sha CHAR(40) NOT NULL` for activation envelopes.
- `required_check_contexts JSONB NOT NULL DEFAULT '[]'` containing canonical `{name, app_slug}` pairs.
- `authority_version INTEGER NOT NULL DEFAULT 1`.

Existing rows may remain nullable through a staged migration, but Path A remote writes must reject null activation fields. These values are Founder inputs to `authorize_execution_scope`; they must not be copied from model prose or a proposal automatically.

### New `mainai_push_grants`

A new table is genuinely unavoidable because neither task approval events nor execution envelopes describe a one-shot, post-verification, exact-commit remote effect.

Required columns:

- `id UUID PRIMARY KEY`
- `owner_id UUID NOT NULL`
- `goal_id UUID NOT NULL`
- `task_id UUID NOT NULL`
- `job_id UUID NOT NULL`
- `worktree_id UUID NOT NULL`
- `execution_envelope_id UUID NOT NULL`
- `preflight_receipt_id UUID NOT NULL`
- `repository_identity TEXT NOT NULL`
- `branch TEXT NOT NULL`
- `base_branch TEXT NOT NULL`
- `base_sha CHAR(40) NOT NULL`
- `local_commit_sha CHAR(40) NOT NULL`
- `authorized_by TEXT NOT NULL`
- `authorized_at TIMESTAMPTZ NOT NULL`
- `expires_at TIMESTAMPTZ NOT NULL`
- `status TEXT NOT NULL` in `active|consumed|expired|cancelled|superseded`
- `consumed_at TIMESTAMPTZ NULL`
- `consumed_by_job_id UUID NULL`
- `idempotency_key TEXT NOT NULL`
- `provenance JSONB NOT NULL`

Constraints/indexes:

- Composite owner-bound foreign keys wherever the referenced tables support them.
- Unique `(owner_id, idempotency_key)`.
- Partial unique active grant on `(owner_id, task_id)` where `status='active'`.
- `expires_at > authorized_at`.
- Consumed fields are present iff status is consumed.
- Append-only authority identity columns; only the status/consumption fields may change.

### New `mainai_activation_preflight_receipts`

Required columns:

- `id UUID PRIMARY KEY`
- `owner_id`, `goal_id`, `execution_envelope_id`, `provider_spend_authorization_id`
- `repository_identity`, `integration_branch`, `activation_sha`, `base_branch`, `base_sha`
- `authority_version`
- `required_check_contexts JSONB`
- `runtime_config_digest CHAR(64)`
- `runtime_token_identity TEXT`
- `runtime_token_attestation_digest CHAR(64)`
- `ruleset_attestation_digest CHAR(64)`
- `facts JSONB NOT NULL`
- `unknown_required_facts JSONB NOT NULL`
- `verdict TEXT NOT NULL` in `go|no_go`
- `observed_at`, `expires_at`, `invalidated_at`
- `receipt_digest CHAR(64) UNIQUE NOT NULL`

The digest is SHA-256 over canonical JSON containing every field above except database-generated identifiers/timestamps that are not part of the decision. Receipts are immutable. Invalidation appends a new audit event or sets `invalidated_at`; it never edits the facts.

## APIs

All mutation endpoints require `require_founder` and owner-scoped RLS.

1. `POST /api/mainai/execution/goals/{goal_id}/preflight`
   - Read-only with respect to GitHub/provider; it may persist the immutable receipt.
   - Returns GO only when every required runtime fact and every required attestation is present, current, and matching.
2. `POST /api/mainai/execution/tasks/{task_id}/push-grants`
   - Accepts `job_id`, `worktree_id`, `local_commit_sha`, `repository_identity`, `branch`, `base_branch`, `base_sha`, `preflight_receipt_id`, `expires_at`, `idempotency_key`.
   - Server independently re-reads all bound facts. Client-supplied values are comparisons, not authority.
3. `POST /api/mainai/execution/push-grants/{grant_id}/cancel`
   - Active to cancelled only; idempotent cancellation may return the already-cancelled row.
4. No API lets MainAI create its own grant or attest its own GitHub restrictions.

`open_pr` continues to use the existing task approval endpoint, but requires an active envelope with the exact `open_pr` capability. Remove `repo_edit` from `OPEN_PR_CAPABILITIES`.

## Push grant state machine

Grant creation is allowed only after:

- the repo-edit task is still on the bound job/attempt;
- worktree ownership verifies;
- worktree `HEAD` equals the requested local commit SHA;
- verification PASS is durably checkpointed for that job and commit;
- the active envelope exactly matches the grant's envelope, repo, branch scope, base branch, and base SHA;
- the GO preflight receipt is live and all its bindings still match;
- the remote task branch is absent or is an ancestor/equal state compatible with a non-force push.

Atomic consumption immediately before push:

1. Start a database transaction.
2. Lock grant, task, job, worktree, envelope and receipt rows in a fixed order.
3. Re-read local `HEAD`, ownership marker, current active envelope and receipt validity.
4. Re-read the remote base-branch SHA and require equality with `approved_base_sha` and receipt `base_sha`.
5. Atomically update `active -> consumed`, recording job and time; zero updated rows means deny.
6. Commit consumption before the network effect.
7. Perform the plain non-force push.
8. Record remote SHA evidence and require remote SHA equals the approved local SHA.

If the process crashes after consumption but before proof of push, the grant remains consumed. Recovery inspects the remote branch. It may record that the exact approved SHA is already remote, or require a new Founder grant; it must never reactivate the old grant.

### Cancellation and retry

- Cancelling a task or goal cancels every active push grant for it.
- Replanning or replacing the active envelope supersedes every active grant bound to the old plan/envelope.
- A retry creates a new job and therefore requires a new push grant, even when the local commit is byte-identical.
- A verification failure cancels/supersedes active grants for that attempt.
- Changing worktree HEAD after approval invalidates the grant; it cannot be amended.
- Expiry is checked using database time during grant creation and consumption.
- Historical `approval_granted` events remain task-dispatch evidence only and are never queried by push authorization.

## Remote-write blast-radius rule

Add one effect-bound predicate, used at every remote-write call site:

`require_remote_effect_authority(db, task, effect, context)`

Rules:

- `effect=push`: requires current activation envelope capabilities `path_a`, `repo_edit`, and `push`, plus a consumable exact push grant.
- `effect=open_pr`: requires current activation envelope capabilities `path_a` and `open_pr`, plus the ordinary Founder task approval for the current task/plan.
- A goal with no envelope, an inactive envelope, a mismatched repository/base SHA, or no effect capability is denied regardless of `GITHUB_WRITE_ENABLED` or approval policy.
- `GITHUB_WRITE_ENABLED` remains only a global kill switch. It can deny but can never grant.
- `standard_repo_work` may retain legacy local behavior; it cannot cause a remote effect without the activation envelope/effect grant.

This predicate must guard both the live Path A functions and any legacy GitHub write helper. The legacy `prepare_github_pr` route remains disabled.

## Immutable SHA binding

The Founder-approved SHA is stored on the envelope and receipt. It is checked at five boundaries:

1. Before paid planning: configured repo/base branch remote tip equals approved SHA; receipt is GO/live.
2. Before worktree creation: fetch the remote base tip and require the same SHA; create from that SHA only.
3. Before paid editing: worktree `base_sha`, active envelope and receipt all equal the approved SHA.
4. Before push: repeat the remote base-tip comparison and every local binding.
5. Before PR publication: PR base equals approved base branch; pushed head equals recorded approved commit; envelope/receipt remain current.

Any branch movement produces a typed `ActivationAuthorityStale` outcome. It parks the task for Founder review and must not trigger a provider call or paid replan.

## Required CI set

At envelope authorization, the Founder supplies the required check set from an admin-attested ruleset snapshot. Each entry is canonical `{name, app_slug}`; duplicate names with different apps are distinct.

`start_ci_wait` copies this set into the immutable wait condition together with repository, PR number, base branch, head branch, and exact pushed SHA. Polling:

- queries check runs for exactly the wait SHA;
- matches both name and GitHub App slug/identity;
- keeps waiting while any expected check has not appeared or is incomplete;
- fails on any expected failing conclusion;
- ignores unexpected additional checks for PASS calculation but records them as evidence;
- rejects a same-name check from another app as substitution;
- re-reads the PR and requires its base, head branch and head SHA still match the wait;
- cannot pass on a check run whose `head_sha` differs;
- times out to failure, never optimistic success.

Once all required contexts pass, the task may truthfully report CI PASS. This creates no push, PR, merge, deploy, Recall or replan authority.

### FC-4 / npm-audit integration

Claude's FC-4 work owns remediation of the inherited npm-audit failure. This activation batch does not suppress, rename, ignore, or downgrade that check. The Founder-approved required set must include the exact npm-audit context once FC-4 establishes its stable name. An inherited failure resolves the CI wait as failed/blocked and records evidence; it must not automatically invoke planning or editing. A new paid remediation cycle requires a new Founder goal/envelope/spend authorization.

## Deterministic preflight

### Runtime-verifiable facts

- exact running code/activation SHA and clean checkout;
- configured repository, base and integration branches;
- remote base/integration SHA observations;
- current envelope identity/status/version, exact capabilities, paths, repo and SHA;
- current spend grant identity/status/envelope binding, provider/model allowlist, request/token/USD ceilings and expiry;
- provider/model has a pinned price entry or the grant has an explicit per-request USD cap;
- no active Supervisor lease for the same goal/workspace;
- no conflicting active Path A job/worktree;
- worktree ownership and SHAs when a worktree exists;
- `github_write_enabled=true`, `github_auto_merge_enabled=false`, legacy write route false;
- no merge/deploy/credential/Recall capabilities in the envelope;
- required check set is non-empty and canonical;
- database time is before every expiry.

### Founder/admin-attested facts

- ruleset applies to the intended integration branch;
- PR required, required-check list, no bot bypass, no force push, no deletion;
- Founder retains final merge authority;
- fine-grained token identity, repository selection and exact permission manifest;
- forbidden token permissions are absent;
- attestation issuer, observed time, expiry and canonical payload signature/digest.

MainAI cannot author these attestations. The attestation must be produced by a separate Founder/admin control plane and verified against a pinned trust key/identity.

### Currently unprovable without new attestation/control-plane support

- absence of all forbidden permissions from a classic PAT;
- absence of ruleset bypass actors when queried by a token unable to view full ruleset administration;
- future immutability of GitHub settings after preflight;
- provider list-price freshness from the static application price table.

If any required fact is unknown, preflight returns NO-GO. Settings changing after receipt creation change `runtime_config_digest` and invalidate it. Envelope/spend/ruleset/token/base-SHA changes likewise invalidate the binding.

Receipt lifetime for the first supervised cycle should be at most 15 minutes and never beyond envelope, spend grant, token attestation or ruleset attestation expiry.

## Exact production call sites

1. `app/models/execution_envelope.py` and a new migration: activation SHA/check-set fields.
2. `app/models/mainai_execution.py` or dedicated `app/models/mainai_activation.py`: push grants and receipts.
3. `app/schemas.py`: Founder request/response schemas.
4. `app/routers/mainai_execution.py`: preflight, create/cancel push grant endpoints.
5. New `app/mainai_execution/activation_security.py`: canonical digest, preflight, grant creation/consumption, remote-effect predicate.
6. `app/mainai_execution/envelope_scope.py`: strict capabilities; never use repo-edit authority for PR or push.
7. `app/mainai_execution/planner.py` and `billed_chat.py`: live receipt/SHA check before paid planning.
8. `app/mainai_execution/execution_job.py`: checks before editing, push and PR; remove generic approval as push authority.
9. `app/mainai_execution/worktree.py`: accept the approved base SHA, do not independently select a moving branch tip.
10. `app/mainai_execution/approval.py`: task approval stays dispatch-only; add plan/task freshness if not already supplied by the caller.
11. `app/worker.py`: remote effects deny never-governed/legacy goals and avoid paid retry/replan for inherited CI failures.
12. `app/mainai_execution/ci_wait.py`: immutable expected contexts and PR identity verification.
13. `app/integrations/github_client.py`: read rules/PR/check identity needed for verification; no merge implementation.
14. `app/config.py` and startup validation: activation mode fails closed on incomplete configuration.

## Dependency-ordered implementation

1. Migration/models/constraints.
2. Canonical authority/receipt serialization and pure validators.
3. Founder/admin attestation verification and preflight service.
4. Envelope activation-SHA/check-set authorization fields.
5. Effect-bound remote-write predicate and legacy blast-radius closure.
6. Worktree/base-SHA binding at planning, creation and edit boundaries.
7. Push grant APIs and atomic consumption/recovery.
8. Separate `open_pr` capability enforcement and PR identity checks.
9. Required CI set evaluation and inherited-failure blocking.
10. Startup validation, observability and audit output.
11. Focused adversarial suite, existing Path A suite, migration roundtrip, then exact-SHA CI.

## Acceptance tests

Required test groups:

- `test_activation_preflight.py`: GO/NO-GO fact matrix, attestation trust/expiry, digest/staleness.
- `test_activation_push_grant.py`: exact binding, concurrency, consumption, retry/cancel/recovery.
- `test_activation_remote_write_boundary.py`: legacy/unrelated goals and global-switch behavior.
- `test_activation_sha_binding.py`: movement at every paid/mutating boundary.
- `test_activation_ci_contract.py`: complete expected set, app identity, late checks and PR drift.
- Existing execution-envelope, Path A, worktree/recovery, provider-spend and CI suites remain green.

The executable local reference probes are in `docs/evidence/activation_security/activation_security_probe.py`. They do not call GitHub, providers, agents or production services.

## Cursor PASS/FAIL checklist

- [ ] P1: repo edit, push and open PR each require distinct explicit authority.
- [ ] P1: exact one-shot push grant exists and binds every required identity/SHA field.
- [ ] P1: task approval events are never accepted as push authority.
- [ ] P1: concurrent consumption permits exactly one winner.
- [ ] P1: retry/new job requires a new grant; cancellation/supersession invalidates old grants.
- [ ] P1: legacy and unrelated goals cannot perform remote writes when GitHub is globally enabled.
- [ ] P1: Founder-approved base SHA is durable and checked before every paid/mutating boundary.
- [ ] P1: branch movement yields a parked/NO-GO state without paid replanning.
- [ ] P1: CI PASS requires every exact expected `{name, app_slug}` check for the exact pushed SHA.
- [ ] P1: PR base/head/head-SHA drift fails the wait.
- [ ] P1: inherited npm-audit failure cannot trigger paid replanning.
- [ ] P1: deterministic preflight exists; every unknown required fact yields NO-GO.
- [ ] P1: receipt binds code SHA, config, envelope, spend, ruleset/token attestations and base SHA.
- [ ] P1: stale receipts are rejected at every paid/remote boundary.
- [ ] P1: no merge/deploy/credential/Recall authority is introduced.
- [ ] P2: independent-review request remains evidence/workflow only, never authority.
- [ ] Migration upgrade/downgrade and RLS/owner-isolation tests pass.
- [ ] Focused probes and full relevant regression suite pass.
- [ ] Exact remote SHA and exact-SHA CI are independently verified before freeze.

PASS requires every P1 item. A skipped, xfailed, unknown, locally-only, or builder-self-reported result is FAIL for activation.
