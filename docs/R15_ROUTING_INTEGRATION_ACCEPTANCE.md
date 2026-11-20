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

The existing assistant reply slot is a durable dispatch claim (`failed/inflight:<attempt-id>`) before retrieval/provider work. A user-message row lock serializes both initial and retry races, refreshing cached reply rows after acquiring the lock. Success replays the saved reply. In-flight, uncertain provider or uncertain budget outcomes refuse automatic retry (409). No new table/status is added.

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

Additional integration verification exposed two inherited CI setup gaps: R15 consultation tests import fakeredis but requirements-dev did not declare it, and the fresh migration job did not provision the restricted `mainai_app` role targeted by R15's grants. The candidate pins fakeredis[lua] as test-only and provisions a NOSUPERUSER/NOBYPASSRLS test role before migration checks; no migration or security assertion is skipped. Full backend collection succeeds (3,106 cases at that checkpoint).

All remaining R12 deterministic tests now avoid global `zai` registration/config changes. Their mocked prompt capture stays deterministic; real model-mode suites remain explicitly opt-in and unavailable CLI checks are reported as skips, never cognitive passes. Eval prompt/model observations are lease-local and erased on scope exit rather than retained in class-global variables.

Clean Cloud rerun after ENOSPC recovery: **418 passed** across provider/chat/billing/financial contracts, R15 project/GitHub status and RLS/security suites. Separate eval/consultation follow-up: **65 passed, 12 skipped** (real CLI/model evaluation not authorized/available). These overlap and are not summed. Final CLI credential-environment regression is additionally included before the next CI checkpoint. CLI subprocesses receive only an environment allowlist (PATH/HOME/locale/temp/config path), never HTTP API keys, application secrets or database credentials; CLI credentials must be independently provisioned. Missing model/usage identity in CLI output fails closed rather than assuming the requested alias.

Each safely retried reply receives a fresh durable attempt nonce; the budget key is stable within one attempt but cannot reuse a previously released hold as new admission. Missing/malformed usage on an enforced attempt retains the hold instead of settling it as zero. The nonce, duplicate-claim and malformed-usage regressions cover these transitions explicitly.

The durable attempt nonce uses URL-safe encoding of all 128 UUID bits in the existing 32-character error-category field (31 characters including its prefix). The real-PostgreSQL retry/concurrency tests validate this schema boundary; no column widening or migration is introduced.

Before any budget-store call, an existing `AuditLog` intent row durably binds the authenticated owner, tenant snapshot, reply slot, provider/model and full attempt reference. The reply slot also retains provider/model attribution. This permits exact reconciliation after an uncertain Redis/provider outcome even after the public error category changes; the intent is not mislabeled as proof of actual disclosure or measured spend. Disclosure is committed before invocation whether or not billing enforcement is enabled.

Final local reply/claim/intent/accounting regression checkpoint: **48 passed** against real PostgreSQL/Redis, including the full UUID nonce within the existing field width, durable provider/attempt audit attribution and safe retry after a released hold. No real model/CLI was invoked. Full exact-head CI outcomes are tracked in PR #464 and must supersede earlier failed/intermediate runs.

## Query-embedding boundary

A final call-chain check found that query retrieval precedes chat admission: its vector-only embedding adapter returns neither measured usage nor an idempotency/reconciliation identity. Re-running retrieval after a proven pre-chat-effect failure could therefore repeat an unmetered external embedding call. Guarded `/api/chat` now propagates an effect guard through the existing retrieval and embedding boundary. It refuses production external query embedding before transport, independently of billing mode, and uses the existing explicit degraded-retrieval response. Ollama retrieval retains its owner-bound egress gate; synthetic evaluation requires the active authorized evaluation lease. Unknown providers and an unleased `zai_eval` fail closed. Legacy retrieval callers retain their existing behavior and are not certified here.

This intentionally limits guarded Founder chat to local query embedding until the shared embedding result contract supplies trustworthy usage, attempt identity and reconciliation semantics. It does not silently substitute an external embedding model or mix embedding spaces. Real paid embedding support remains a release dependency, not a claimed pass. Negative tests assert zero external calls, exact guard/owner propagation and preservation of local redaction.

Query-embedding guard regression: **142 passed** across integration-security, chat and egress suites. Source/context fixtures use a mocked local embedding transport, retain active-space identity, and keep their original grounding, conflict, status and redaction assertions. An initial guard run exposed the obsolete external-embedding fixtures; that interrupted run is not a pass.

## Finding index for the independent reviewer

Risk describes the defect before correction, not independent approval of the patch. Line references identify the corrected boundary in this candidate; the exact parent above preserves the reproducing before-state.

| Risk | Boundary | Reproducible evidence / disposition |
| --- | --- | --- |
| High | `providers/registry.py:76`, `mainai_evals/model_gate.py` | Concurrent eval could replace production Z.ai registration/config. Scoped identity, expired copied-context and credential-environment regressions now refuse cross-context use. |
| High | `routers/chat.py:96`, `providers/registry.py:330` | Concurrent retry or a timeout could reinvoke/fall back after an uncertain effect. Real PostgreSQL reply-slot/concurrency and timeout tests assert one invocation and retry HTTP 409. |
| High | `billing/enforcement.py:213`, `:315`, `billing/reservation.py:137` | Repeated reserve/finalize or safe retry could reuse/release counters incorrectly. Real Redis and local concurrent replay, wrong-key and fresh-attempt tests verify idempotent transitions. Unknown outcomes remain held. |
| High | `providers/registry.py:212`, `rag/retrieve.py` | Retrying retrieval could repeat an unmetered external query embedding. Guarded chat refuses it before transport; negative tests assert zero calls and preserve owner-bound local redaction. Full paid embedding support is still unavailable. |
| Medium | `project_status/service.py:462`, `routers/chat.py` | Unconditional R15 status composition pushed measured chat requests to 98/102 SQL statements beyond the existing limit of 80. Context is now question-relevant, includes two recent user turns, remains owner-scoped, and explicitly forbids status claims when not loaded. The limit and latency assertions are unchanged. |

Older cognitive fixtures also omitted the model/native-dimension identity required by the existing vector filter. Their active-space identities are now stamped and their deterministic query transport is local; grounding, conflict, prompt-fencing, owner isolation and latency assertions remain. The inherited `test_dbg_gate2.py` unconditionally asserted false; it now asserts the two retrieved source identities, actual science/graph prompt blocks and owner-scoped research topics instead of printing diagnostic output. These fixture/artifact repairs are not represented as live cognitive/model evaluation.

Final composed follow-up: **58 passed**, covering all dedicated integration-security cases, both unchanged SQL-budget/latency assertions, scientific composition, the repaired diagnostic regression and recent-user-turn status continuity. The preceding broad cognitive/status/GitHub/integration run had **157 passed, 1 failed** on the unconditional debug assertion; it is not labeled a full pass. Final full-backend and browser acceptance must come from exact-head CI.
