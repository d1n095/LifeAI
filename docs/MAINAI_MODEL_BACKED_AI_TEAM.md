# Model-backed AI team

Connects the real local-model inference verified in **#334** to the executive business
workflow from **#335**, so the specialist agents do genuine model-backed work instead of
deterministic in-process computation. Same orchestration, same security kernel — only the
worker execution strategy changes.

## Design — one new seam, no second framework

The engine gained a pluggable `WorkerRunner` (`app/mainai_business/execution.py`):

- **`InProcessRunner`** — the #335 deterministic default (no provider).
- **`ModelBackedRunner`** — REAL local inference through the existing provider abstraction
  (`providers.resolve_active(db, "chat")` → `OllamaProvider.chat`). It reads the worker's
  inputs from the **assignment's context package only**, so a worker can never see anything
  outside what the broker was authorised to disclose.

Everything else is reused unchanged: the workforce registry, evidence-based selector,
delegation broker, `TaskScopedAuthority`, independent-verification pipeline, performance
ledger, failure/restart recovery, the Founder-approval gate and the persistent project view.

## The seven requirements (directive P1)

1. **Distinct, authorized tasks** — each specialist gets its own delegation request +
   task-scoped authority envelope (unchanged from #335).
2. **Real local model inference** — `ModelBackedRunner` calls the local Ollama model; no stub
   in production.
3. **Only permitted context** — the prompt is built from the context package the broker
   produced (objective + upstream verified findings), nothing else (`prompts.build_messages`).
4. **Structured, schema-validated output** — `schemas.parse_capability_output` extracts and
   Pydantic-validates the model's JSON; an unparseable answer **fails the task**, never coerced.
5. **Evidence + execution receipts** — provider, model, latency, token usage, prompt and
   output SHA-256, attempts — persisted on the assignment provenance and surfaced in the view.
6. **Untrusted until independently verified** — output is stored as DATA; promotion to
   VERIFIED requires a **deterministic validator** (`validators.py`) applied by a *different*
   verifier agent.
7. **Failures reported, never bypassed** — provider-unreachable / schema-invalid / validator-
   rejected all mark the task failed or rejected and **block dependents**; no fabricated result.

### Independent verification is NOT the model reviewing itself

Per the directive: verification here is a **deterministic validator** — real code checking the
output's schema and its consistency with the permitted upstream findings (e.g. a synthesis may
not recommend "go" when the finance specialist reported *not viable*). It is never the builder
model certifying itself, and never a second model rating the first. The verifier agent profile
is always distinct from the builder, and the decision records `deterministic_validator`.

### No paid-provider fallback

`ModelBackedRunner` resolves the active chat provider and **refuses, fail-closed, any
non-local provider** (`require_local`). It never calls `chat_with_fallback`, so there is no
path to a paid provider. In CI `CHAT_FALLBACK_ORDER` is empty and no paid key exists, so a
paid provider is structurally unreachable; a dead local model surfaces as a task failure.

## Founder interface

`POST /api/mainai/business/ecommerce-assessment` takes `mode: "in_process" | "model_backed"`.
`GET /api/mainai/business/{workflow_id}` surfaces each task's `runtime`, `provider_invoked`
and full `execution_receipt`, plus the aggregated assessment — the one place the Founder reads
the team's verified work.

## Verification (directive P3)

Two layers, both in `.github/workflows/model-backed-ai-team.yml`:

1. **Deterministic business pytest suite** (real Postgres, stub local provider + in-process):
   `test_business_schemas.py` (7), `test_model_backed_workers.py` (6), `test_business_workflow.py`
   (8), `test_mainai_business_api.py` (4). Covers schema validation, receipts, independent
   deterministic verification, honest failure + dependency blocking, context isolation,
   duplicate prevention, prompt-injection resistance, Founder-approval enforcement, cancellation
   + restart recovery, audit/provenance, cross-owner RLS isolation, and fail-closed refusal of a
   non-local provider. **25 executed locally, all PASS.**
2. **Real local-model acceptance** (`scripts/ci/model_backed_ai_team_acceptance.py`) against a
   running Ollama (`qwen2.5:0.5b`): asserts the real model was invoked for multiple roles with
   ollama receipts, the approval boundary holds, the view reassembles with receipts, and no
   paid provider is used. PASS is reported only for what actually executes.

## Honest status

Real model weights run only in CI (no local Ollama on the dev box) — the same split as #334.
The adapter logic is fully executed locally against real Postgres via a stub local provider;
the real-model run is the CI job. A tiny 0.5B model may fail some roles' JSON; that is handled
honestly (task failed, dependents blocked) and the acceptance script asserts the integration
invariants, not model answer quality.

## Boundaries

No STRATO, no merges of frozen/unreviewed PRs, no paid-provider activation, synthetic data
only, no autonomy. `first_cycle_go` stays `false`. Depends on #335; independent review required
before merge.
