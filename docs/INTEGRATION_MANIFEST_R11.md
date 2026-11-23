# Integration Manifest — Cognitive Core, Round 10+11 artifact

**Branch:** `zai/alpha-runtime-reliability` @ `9b0cb7aca863fb3048a19b1f6fc50732a8617554`
**Artifact:** `lifeai-alpha-runtime-reliability-9b0cb7ac.bundle` (complete history, plain `git clone` works — HEAD symref included; sha256 in `SHA256SUMS.txt`) and `…-tree.tar.gz` (byte-identical to `git archive HEAD`).
**Base for integration:** Round 9 `fd413518` (already shared with the operational lane) → R10 delta is the 7 commits `2d951026..9b0cb7ac` (see `git log fd413518..9b0cb7ac`).

## 1. New and modified modules

| Path | Change | Purpose |
|---|---|---|
| `backend/app/mainai_evals/__init__.py`, `cognitive_gate.py` | NEW | Cognitive Regression Gate: dataset loader, production-path executor contract, `compare_gate_runs` policy interface, run persistence |
| `backend/app/models/cognitive_gate.py` | NEW | ORM for migration 0075 tables |
| `backend/app/models/improvement_proposal.py` | NEW | ORM for migration 0076 table |
| `backend/app/mainai_execution/improvement_proposals.py` | NEW | Controlled-initiative service (propose/decide/implement/revert; no execution path) |
| `backend/app/routers/improvement_proposals.py` | NEW | Founder-gated router, wired in `app/main.py` (`include_router(improvement_proposals.router)`) |
| `backend/app/rag/intellectual_adversary.py` | NEW | Bounded evidence-driven review; rides evidence-chain endpoint |
| `backend/app/rag/scientific_reasoning.py` | NEW | Topic science composition + bounded investigation policy |
| `backend/app/problem_learning/service.py` | MODIFIED | `record_decision` supersession now writes a `decision_superseded` audit row (append-only in effect; content never edited) |
| `backend/app/routers/library.py` | MODIFIED | evidence-chain response gains `adversarial_review` field (fail-open); module logger added |
| `backend/app/schemas.py` | MODIFIED | `ClaimEvidenceChainOut.adversarial_review: dict` |
| `backend/app/rag/graph_instructions.py` | MODIFIED | GRAFKONTEXT block gains the science+policy line; landscape/rows fetched once and SHARED with the science analysis (12→9 statements) |
| `backend/app/models/__init__.py` | MODIFIED | registers the two new model modules |
| `backend/app/main.py` | MODIFIED | router wiring only (one import + one include_router) |
| `evals/cognitive_gate/cases_v1.json` | NEW | FIXED, versioned dataset (13 cases / 13 capability areas) |
| `docs/COGNITIVE_CORE_MATRIX_R10.md` | NEW | 16-capability matrix with measured evidence |
| `docs/COGNITIVE_REQUIREMENTS_REGISTRY_R9.md` | UNCHANGED | extended by the matrix, not edited |

## 2. Database migrations (append-only chain: …0074 → 0075 → 0076)

- **0075** `cognitive_gate_runs` + `cognitive_gate_case_results` — founder-wide evidence tables (NOT RLS-protected, same precedent as `provider_verification_checks`, migration 0013). CHECK constraints on execution/status; run row FK-cascades to case rows.
- **0076** `mainai_improvement_proposals` — owner-scoped, **ENABLE+FORCE RLS** with the standard `app.current_user_id` isolation policy; CHECK constraints on source/status; UNIQUE(owner, duplicate_key, status) for open-duplicate suppression; FK → users ON DELETE CASCADE.
- Downgrade path provided for both. No existing table is altered. Alembic chain verified: `0074 → 0075 → 0076` on a fresh database.

## 3. API changes

- NEW `POST/GET /api/mainai/improvement-proposals` (+ `/decision`, `/implemented`, `/reverted`), all `require_founder`; response carries `execution_attempted=false` structurally.
- EXTENDED `GET /api/library/claims/{id}/evidence-chain` response: new `adversarial_review` object (additive — existing consumers unaffected; fail-open `{reviewed: false, error: "review_unavailable"}` on internal failure).
- No existing endpoint changed signature or semantics. No breaking changes.

## 4. Dependencies

- **None added.** Everything uses existing deps (SQLAlchemy, FastAPI, stdlib). No new Python packages, no node modules.

## 5. Required configuration

- No new settings. Endpoints/reuse existing: `DATABASE_URL`, `APP_DATABASE_URL`, `MAINAI_APP_PASSWORD`, `REDIS_URL`.
- Eval dataset is repo-fixed (`evals/cognitive_gate/cases_v1.json`); dataset version mismatch refuses to run by design.

## 6. Runtime services

- PostgreSQL (as before; migrations 0075/0076 apply on `alembic upgrade head`).
- Redis (as before).
- No new processes, workers, or ports. The gate runs on demand (CLI/test), not as a daemon.

## 7. Tests (all green; suites runnable independently)

- `tests/backend/test_cognitive_gate.py` (6) — dataset integrity, full deterministic run, mutation proof, honest `skipped_no_model`, persistence roundtrip, policy blocking semantics.
- `tests/backend/test_intellectual_adversary.py` (8) — no manufactured objections, evidence-ref objections, counterevidence symmetry, overstated confidence, audit swings, broken-chain guard, production ride, owner scoping.
- `tests/backend/test_decision_memory_integrity.py` (7) — what+why persistence, append-only audited supersession, 3-deep chain, idempotency, explicit supersede, stale evidence, authority markers, restart.
- `tests/backend/test_scientific_reasoning.py` (6) — axis counts, confounding gap, missing-evidence, all four policy outcomes, prompt ride, owner scoping.
- `tests/backend/test_full_cognitive_acceptance_r10.py` (2) — the 12-step chain + owner isolation.
- Plus the pre-existing suites untouched.

## 8. Security-sensitive changes

- Two NEW founder-gated surfaces (proposals router) — RLS FORCE + founder dependency; no execution path exists in code (structural, not instructional).
- `adversarial_review` on the evidence-chain response: read-only computation, fail-open, owner-scoped, no raw exception text surfaced (fixed `review_unavailable` marker).
- Migration 0076 grants: standard owner RLS; app role privileges unchanged elsewhere (no GRANT changes in either migration — boot-time reassertion paths in `app/rls.py` untouched).
- Prompt-injection framing: UNCHANGED by R10 (see Phase 7 fencing contract in the R11 report for the unified-boundary note with Freebuff's shared module).
- Audit additions: `decision_superseded` rows carry both decision ids + rationale fields; no secrets in detail fields.

## 9. Potential conflicts with Freebuff's implementation

| Area | Risk | Resolution guidance |
|---|---|---|
| `main.py` router wiring | LOW — additive two lines | trivial rebase; no ordering dependency |
| `alembic` head | MEDIUM — if Freebuff also shipped 0075/0076 | our revisions are 0075/0076; if taken, renumber ours (chain-only change; no SQL edits needed) |
| `problem_learning/service.py` | LOW — one added audit block inside `record_decision` | conflict-free unless Freebuff rewrote that function |
| `library.py` / `schemas.py` | LOW — additive field | additive; merges cleanly |
| Prompt-fencing | DELIBERATE OVERLAP — R9's untrusted framing vs Freebuff's shared fencing module | Phase 7 contract: one boundary, framing calls delegate to the shared module when present; see R11 report's fencing section |
| `docs/` | NONE | new files only |

## 10. Transfer status

- The bundle imports into a clean repository with a plain `git clone` (verified: HEAD checks out at `9b0cb7ac`, `fd413518` is an ancestor, fsck clean).
- **NOT confirmed received by Freebuff** — no transfer channel/acknowledgment exists in this sandbox. The artifact is ready for the authorized operational lane to pull; transfer verification is that lane's acknowledgment, not ours to claim.
