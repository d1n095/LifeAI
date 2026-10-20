# Combined application + activation acceptance (Codex integration candidate)

One command judges an integration candidate for **both** first-cycle activation (Codex transport/grants on the Cursor
execution layer) and **application readiness** (website, Founder interface, proxy):

```
FE_BUILT=$(probes/proxy_v1/build_frontend.sh <candidate-worktree> <scratch>/fe) \
REDIS_URL=redis://127.0.0.1:6390/0 probes/first_cycle_acceptance_v1/run_acceptance.sh combined <candidate-worktree>
```

| Suite | What it proves | combined requirement |
|---|---|---|
| stop_classification (A-K) | external failures terminal + never paid again; FO-6/FO-7 exact paths; recovery bound; concurrent STOP vs draft PR; erasure after full cycle | all required |
| credential_canary, transport_contract | host credential helpers / askpass never consulted (sandbox + canary + localhost 401) | all required (TC-1/TC-3 too) |
| review_stop, task_authority, spend | review receipts, STOP-before-merge, target authority, spend reservation | as codex profile |
| authorization_grants (activation_280), grant_contract (GC-1..8, adapter) | Founder-only grant APIs; one-use / race-safe / expiring / exactly bound push grant; distinct draft-PR grant; no re-issue without fresh preflight; typed denial | all required |
| candidate_harness | candidate's own acceptance harness (CRED-1/2/3, E2E-3, GRANT-PROD, TA-12/13, PUB-2) sandboxed | as codex profile |
| app_readiness (AR-1..AR-13) | route authz matrix, STOP, truthfulness, isolation, duplicate-work races, safe launch, no paid egress | all required |
| proxy (PX-1..PX-9) | traversal, header trust, XFF rate-limit bypass with the Dockerfile uvicorn flags | all required |
| backend_regression, fc4_*, ci_exact_sha | candidate regression, npm-audit policy, 12 required checks x push+PR at the exact SHA | all required |

Rules: BLOCKED / ADAPTER-NEEDED / FRONTEND_NOT_BUILT are failures, never skips. When Codex's grant API differs from #280,
only `grant_contract_v1/grant_adapter.py` learns it (see `../grant_contract_v1/INTEGRATION_CONTRACTS.md` §2); assertions
never change. Class-F Founder/GitHub facts (MATRIX.md) are never implied by a green gate. Manual row AP-9 (stale
Founder-status UI) is reproduced with `proxy_v1/live_stack.sh` until an E2E exists.

Validated: #292 5e3cd01 through `cursor` and `combined` summaries (`../fc292/app_proxy_gate/summary_*.txt`).
