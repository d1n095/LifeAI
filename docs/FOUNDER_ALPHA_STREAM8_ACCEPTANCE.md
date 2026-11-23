# Founder Alpha — Stream 8 Real Application Acceptance

**Exact source SHA:** `afcca5e37aaaa13d873279e5fb0b68bfb498c2e7` (PR #485, `claude/founder-alpha-stream7-security`)
**Rehearsal branch:** `claude/founder-alpha-stream8-acceptance`
**Method:** genuinely fresh install into a new database (`lifeai_stream8`), real `uvicorn` backend + real `app.worker` process, no mocks, in a disposable sandbox. Not inferred from any earlier phase/stream's old commits.

This is the final stream of the eight-stream "LifeAI Founder Alpha Completion Program." It does not re-derive what Streams 1–7 already proved; it re-runs the install and the founder journey live, on this exact head, and re-confirms the two fixes Stream 7 just made actually hold under real traffic.

## Installation outcome — PASS

`scripts/setup/bootstrap_database.py` run against a brand-new Postgres database with pgvector 0.6.0: all 7 steps (`ensure_app_role`, `migrate`, `apply_privileges`, `verify_schema`, `verify_boot`, `verify_grants`, `verify_s1a`) reported `ok`. Single migration head confirmed. Real `uvicorn app.main:app` and `python -m app.worker` started cleanly; `/api/health` reported `{"status":"ok","worker":"alive","storage":{"writable":true}}`.

## Real-model readiness — BLOCKED (infra), re-confirmed fresh

Checked fresh, not assumed from prior phases: no `ollama` binary present, no local server on 11434, DNS resolution for `registry.ollama.ai` fails, and the sandbox's own egress proxy returns an explicit `403 Forbidden` for the host (a policy denial, not a timeout). Checked once, not retried further. Identical boundary to every earlier phase/stream in this program — the limitation is this sandbox, not the application.

## Functional acceptance — PASS through the real-model boundary, correctly BLOCKED beyond it

Exercised live, with real HTTP requests against the real backend:

| Step | Result | Evidence |
|---|---|---|
| Login | PASS | Real `/api/auth/login`, 200, session cookie issued |
| Project creation | PASS | Real `/api/projects`, 200 |
| Start conversation | PASS (fail-closed at model boundary) | Real `/api/chat` — user message persisted (`user_message_saved: true`), assistant turn correctly marked `failed` with an empty, non-fabricated reply; `error_category: external_effect_unknown`, "automatic retry and fallback are blocked" |
| Persist conversation | PASS | `GET /api/conversations/{id}` returns both messages after the above |
| Document upload | PASS | Real `/api/documents/upload`, 200, `status: original_stored` |
| Worker claims job | PASS | Worker log: `hämtade jobb <id>` within 5s of upload — confirms the Phase C worker-service fix is live, not a regression |
| Ingestion to embedding boundary | PASS through to BLOCKED (infra) | Document correctly reaches `status: awaiting_provider` — no fabricated chunks, no stuck job |
| Supervised goal creation | PASS | Real `/api/mainai/execution/goals`, 200, `status: pending` |
| Plan proposal (no provider) | PASS (fail-closed) | Real `502` with classified message, not a 500 crash — see Stream 7 re-verification below |
| Goal state after failed plan | PASS | Goal stays `pending`, `plan: null`, `tasks: []` — no partial commit |
| Restart | PASS | Backend + worker killed and restarted; `/api/health` clean within 6s, no manual steps |
| History survives restart | PASS | Conversation, document, and goal all intact and queryable via GET after restart |
| Steps 6–9 of the MainAI cycle (execution/verification/draft PR/inspection) | BLOCKED (infra), not re-derived | These require a successful plan, which requires a reachable provider. Already covered end-to-end with mocked/real-infra test suites in Stream 4 (139 passed) and the earlier program's Phase 4 (239 passed) — cited, not re-run live here, since this sandbox can't produce a real plan either |

## Stream 7 fixes re-verified live — PASS, both hold

1. **Lease-loss write bug (shared-checkout fallback path)**: `tests/backend/test_mainai_execution_checkpoint_fencing.py` (4 tests) run fresh against this exact head — all pass.
2. **Total-provider-failure leaving tasks stuck `running`**: reproduced live above (goal correctly stays `pending`, never `running` with no terminal state) and confirmed by `tests/backend/test_execution_failure_and_cancellation_audit.py` (2 tests, including the exact regression test Stream 7 wrote) — both pass fresh on this head.

## Security results — PASS

Stream 7's full gap-closure suite (14 tests: checkpoint fencing, failure/cancellation audit, central-authorization boundary incl. the new LOCAL_ONLY_MODE positive-path test) — **14 passed, 0 failed**, fresh database, this exact head.

Broader spot-check: `tests/security/` (RLS isolation across all domains) + `tests/backend/jobs/test_worker.py` + `tests/backend/mainai/test_worker_live_lease_expiry_takeover.py` + `tests/backend/test_mainai_execution_recovery.py` + `tests/backend/test_database_recovery.py` — **152 passed, 0 failed** (150.86s).

## Recovery results — PASS (spot-check, not full 12-scenario re-audit)

Full-stack restart (both processes killed and restarted) and the worker/lease-expiry/recovery suite above. Consistent with Phase E's full audit; nothing new found or expected to be found at rehearsal scope.

## Outstanding defects

**None found in this rehearsal.** No code changes were needed — everything exercised either passed for real or hit the same, already-documented infra boundary.

## Release recommendation

**Ready for independent review and staging.** Not yet ready for unsupervised founder production use, specifically because:

1. **No real local-model path has ever been exercised in any sandbox available to this integration effort** — everything up to that boundary is verified correct and fails closed properly, but a live conversation with real model output has never actually happened. This is the single largest gap between "verified" and "usable."
2. **Z.ai Security Kernel R21–R24 remains genuinely unreachable** (re-confirmed by Stream 5) — if that delivery contains security properties beyond what's already integrated, they are not present in this candidate.
3. **Independent third-party review** (the explicit point of PR #478's review package) has not yet happened — everything here is self-verified by the same lineage of agents that built it.
4. The founder-wide (non-goal-scoped) authorization question (task #61) is still open by design (fail-closed), not resolved.

None of the above are defects in what was built — they are honest boundaries of what this sandbox and this session can verify alone.
