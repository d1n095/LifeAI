# MainAI Executive Business Workflow

A Founder-facing executive layer that turns a business objective into a verified, persisted
assessment produced by a **team of specialist agents**, driven entirely through the existing
workforce substrate. It is a composition layer — **no new tables, no new migration, no
provider invocation** — so it reuses the already-reviewed guarantees rather than restating
them.

## What it is

`app/mainai_business/` composes these existing, tested primitives into one end-to-end path:

| Directive capability | Reused primitive |
| --- | --- |
| Accept a complex objective | `BusinessObjectiveSpec` (generic, declarative) |
| Persistent project + execution plan | real `WorkforceDelegationRequest` rows stamped with a shared `business_workflow_id` in provenance |
| Organise specialist agents into a team | `registry.register_workforce_agent` + `broker.form_team` |
| Delegate with scoped permissions | `broker.resolve_delegation` + `authority.TaskScopedAuthority` |
| Execute approved tools | in-process deterministic workers (no provider) |
| Track dependencies / progress / results | `objective.topological_order` + per-task assignment lifecycle |
| Coordinate failures / retries / recovery | `failure.resume_after_restart`, `failure.safe_retry_same_agent`, `broker.cancel_assignment` |
| Request Founder approval before consequential actions | engine-level fail-closed gate + `verification.apply_verification_decision(founder_approval_ref=...)` |
| Preserve history + evidence | persisted assignments, `WorkforceVerificationDecision`, checkpoints |
| One Founder-facing interface | `view.assemble_business_project_view` + `/api/mainai/business` router |

The architecture is **generic and reusable**: swap the `BusinessObjectiveSpec`, keep the
engine. The e-commerce objective is one instance, not a special case.

## Honesty — what is real vs. in-process

- **Real orchestration.** Agent registration, evidence-based selection, the delegation
  broker, task-scoped authority envelopes, context-package disclosure minimisation,
  independent verification (builder-cannot-self-verify), the performance ledger, cancellation
  and restart recovery are all the real production primitives, exercised against real
  Postgres with RLS.
- **In-process workers.** Per-task inference is a deterministic, local, in-process
  computation — **not** a model call — consistent with `workforce.vertical_slice` and
  `safe_internal_run`, and with the boundary that provider activation stays gated
  (`first_cycle_go=false`). Wiring the verified local model from PR #334 (Ollama
  `all-minilm` / `qwen2.5:0.5b`) in as the real worker inference is the honest next step; it
  is explicitly **out of scope** here and is **not** claimed.

## Founder-approval boundary (fail-closed)

A task marked `consequential=True` (e.g. `publish_assessment`) is **never auto-run**. Without
an explicit Founder approval token the engine records it `blocked_awaiting_founder_approval`,
mints no authority and runs no worker. With the token, the task runs and the approval
reference is persisted on the `WorkforceVerificationDecision` for audit.

## Reference objective — synthetic e-commerce opportunity assessment

`ECOMMERCE_OPPORTUNITY_ASSESSMENT`: roles research / finance / operations / analyst /
publisher + an independent verifier. Tasks: `market_research`, `unit_economics`,
`fulfillment_ops` → `opportunity_synthesis` (depends on all three) → `publish_assessment`
(consequential, Founder-gated). No real purchases, payments, publications or external
operations.

## API

- `POST /api/mainai/business/ecommerce-assessment` `{approve_publish, founder_approval_ref?}`
  — run the reference assessment; returns the assembled project.
- `GET /api/mainai/business/{workflow_id}` — the Founder-facing project view, reassembled
  from the database alone.

Both are `require_founder` + RLS-scoped, like every MainAI route.

## Verified properties (executed tests, real Postgres)

`tests/backend/mainai/test_business_workflow.py` (8) + `tests/backend/test_mainai_business_api.py` (4):

1. **End-to-end** — team organised, every task delegated to the matching specialist and
   independently verified, dependency order enforced (synthesis sees all three upstream
   outputs), aggregated recommendation produced, project reassembled from the DB.
2. **Founder-approval boundary** — publish blocked without approval; completes with an
   explicit token; the approval ref is recorded on the verification decision.
3. **Agent privilege isolation** — a task assignment grants only its scoped tool class / read
   path; everything else (`deploy`, `repo_edit`, writes, execution effects) is denied.
4. **Cross-owner isolation** — enforced by Postgres RLS: a restricted session scoped to owner
   B sees zero of owner A's workflow rows, while owner A's session sees them.
5. **Duplicate prevention** — re-running the same `workflow_id` reuses verified tasks; exactly
   one delegation request and one VERIFIED assignment per task.
6. **Prompt-injection resistance** — builder cannot self-verify; a worker result that tries to
   grant itself authority is scrubbed and never applied; credentials/vault are never
   packageable into any trust zone.
7. **Cancellation + restart recovery** — cancel revokes authority (no longer live); restart
   records a durable checkpoint without re-invoking anything; the view still reassembles and
   reflects the revocation.
8. **Audit / provenance** — every executed task carries the workflow provenance stamp and at
   least one independent `VERIFIED` decision; the Founder view exposes the same evidence.

## Boundaries honoured

No STRATO deploy, no merges of frozen/unreviewed PRs, no paid or local provider activation,
no production/personal data (synthetic only), no unsupervised autonomy. `first_cycle_go`
stays `false`. Independent review of this PR is required before merge (no self-certification).
