# R15 routing integration — independent acceptance packet

## Exact sources

Own branch: `codex/r15-routing-integration-hardening`.
Parent: R15 checkpoint `16a6bd301a8a389e7648b8a3c1cdd80a3d115662` (`transfer/freebuff-r15-20261119`).
Imported source delta: #463 `dbd7680d267ae56b57e4a823c7cb1657f2308287` against `69dc5ea5a9cfc750303ea1a9b335c78612bc8f39`, applied as a source patch, not a branch merge. Both source branches remain unchanged. No newer R15 ref was advertised at baseline.

No new router, scheduler, memory store or migration. `first_cycle_go=false`. No deployment, production secret change or provider activation.

## Corrected defects

### Eval/HTTP identity

Production `zai` retains #463's authenticated HTTPS adapter. R15's CLI adapter moves to `zai_eval_provider.ZAIEvaluationProvider`, named `zai_eval` and never globally registered. The model gate no longer overwrites `_PROVIDERS` or writes/deletes global `provider_config` rows.

A task-local evaluation scope requires explicit trusted-code authorization and a closed test/development environment allowlist. It resolves both eval roles without production fallback. A revocable lease invalidates copied/background contexts on exit; an expired scope fails closed rather than resolving production defaults. Direct eval chat/embed also requires that lease. Synthetic embeddings have a separate identity and SHA-256 buckets, not process-salted hashes. Real CLI evaluation requires `RUN_REAL_MODEL_EVAL=1` and was not activated here.

### Verification composition

Retains R15's local cold-start timeout/default-model handling and #463's exact-model cache, latest-failure invalidation, probe-start ordering, explicit model argument and invocation-specific audit attribution. Config changes remain Founder-only, serialized and atomically audited. R15's configured-local-first behavior is retained without allowing a primary preference to waive caller restrictions.

Guarded chat verifies response provider/model attribution. A mismatch is an uncertain external outcome, not success or authority to invoke another provider.

### Retry and accounting

The existing assistant reply slot is a durable dispatch claim (`failed/provider_inflight`) before retrieval/provider work. A user-message row lock serializes both initial and retry races, refreshing cached reply rows after acquiring the lock. Success replays the saved reply. In-flight, uncertain provider or uncertain budget outcomes refuse automatic retry (409). No new table/status is added.

Guarded chat falls back only after explicit `provider_request_may_have_left=False`. Timeouts and ambiguous failures stop the chain. A crash leaves the durable claim in place. Budget reservations occur per actual provider attempt, after egress checks, through existing user/tenant primitives. Unknown/stale external price bounds are refused before invocation rather than priced by a generic fallback.

Proven pre-effect failures release their holds. Ambiguous outcomes retain HOLDs, not invented measured usage. Stable attempt keys bind message/provider/model and budget-subject identity. Redis reserve/finalize operations atomically cover both windows and an identity marker; replay is idempotent and mismatched identities/conflicting terminal transitions fail closed. The local test/development store has equivalent locking semantics.

Successful chat accounting uses the existing `record_usage` writer with a stable reply key. Cross-owner/provider/model key reuse is rejected. Usage and final reply commit together. Explicit output bounds reach all adapters, including Gemini/Ollama; chat bounds output to 1024 tokens. Static prices/token estimates are not billing-grade guarantees.

## Transaction/crash contract

1. Authenticate Founder and establish owner/RLS context.
2. Lock the user-message identity, refresh existing reply, commit the dispatch claim.
3. Check egress, reserve the exact attempt budget if enabled, commit disclosure before invoke.
4. Invoke once; on success reconcile idempotently, then commit usage and final reply together.
5. Release only proven pre-effect failures; each eligible fallback reserves separately.
6. Park unknown external or counter-store effects without automatic refund/reinvocation.

PostgreSQL and Redis do not share a transaction. Cross-store failures remain conservatively parked. Resolving an uncertain claim/hold requires actual provider/budget evidence; this patch does not invent an operator refund or activation API.

## Tests and reproduction

`tests/backend/providers/test_r15_integration_security.py` covers eval isolation/revocation, direct-adapter authorization, composed local verification, real Redis/local idempotency, tenant/owner separation, ambiguous vs pre-effect fallback, durable retry refusal, cached-row concurrency, usage identity and wire output limits. #463's 47 hardening tests remain intact.

```bash
cd backend
python -m pytest tests/backend/providers/ tests/backend/chat/ \
  tests/backend/test_billing_engine.py tests/backend/test_financial_correctness.py \
  tests/backend/test_billing_pg_period_dates.py tests/backend/test_integration_contracts_r11.py \
  tests/backend/test_project_status_work_chain.py tests/backend/test_github_status.py tests/security/ -q
```

Use disposable Cloud PostgreSQL/pgvector + Redis. Exact-head CI evidence is recorded in the draft PR description. Earlier focused composed regressions: 96 passed. TypeScript and three audit-script tests passed; advisory exceptions were not added. A broader local run hit Cloud ENOSPC after 307 passes; its 105 infrastructure errors are not relabeled as passes and that run is superseded by clean reruns/CI.

Inherited test-contract repairs preserve assertions: billing migration tests reference R15's actual 0080–0083 lineage; the prompt-fencing fixture stamps embedding-space identity required by the production filter. Persistence fixtures isolate fallback and distinguish pre-effect doubles from actual timeouts. No framing/security assertion was removed.

## Independent reviewer gates

- Reproduce concurrency, duplicate reserve/finalize, timeout/crash and wrong-owner/provider cases on the exact SHA.
- Verify no eval configuration/registration survives scope exit, including copied contexts, and eval failures cannot reach HTTP authentication.
- Verify owner/tenant attribution from authenticated context through claim, egress, budget and usage; model output supplies no authority.
- Verify unknown pricing and unknown Redis outcomes fail closed.
- Real CLI/model quality, paid-provider behavior, deployment network policy and backup/restore remain separate acceptance gates. No such live activation is performed here.
- Unresolved holds need evidence-based reconciliation, not blind retries or refunds. Actual provider prices remain externally verified data.
- Independent human security acceptance remains required; this agent does not independently approve its own implementation. No merge/deployment/activation is authorized by green tests.
