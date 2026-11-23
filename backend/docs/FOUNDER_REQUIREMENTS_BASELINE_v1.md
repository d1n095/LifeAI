# Founder Requirements Baseline v1.0.0 — the 27 locked requirements (2026-11-18)

**Purpose:** one authoritative, versioned traceability matrix for the 27 founder
requirements (directive 2026-11-18 §2). It follows the repo's single-source-of-truth
discipline ("Grundprincip"): every row points at its evidence home instead of restating
designs. It EXTENDS `docs/LIFE_FOUNDER_REQUIREMENTS_INDEX.md` (PR #451) — that index owns
the R1-R10 core-requirement prose and the audit-finding codes; this matrix owns the
stable IDs, per-requirement status and verification evidence for all 27.

**Status vocabulary (mandatory distinctions):**
`EXISTING / IMPLEMENTED_UNVERIFIED / VERIFIED / PARTIAL / MISSING / BLOCKED / DEFERRED`.
**SAID ≠ STORED ≠ PLANNED ≠ IMPLEMENTED ≠ VERIFIED.** A status of VERIFIED below always
means: real code + executed tests; where the verifier was the builder (this lane), the
row says so — independent human acceptance is tracked separately.

**Verification key:**
- `REAL-PG battery` = clean single-pass run on real PostgreSQL 17 (pgvector) + real Redis
  7.2 built from source, branch `zai/alpha-knowledge-blueprint` @ `1e2cfdc8` (+ acceptance
  fix commit), results in `download/ALPHA_REALPG_RESULTS.txt`.
- `FAS` = Founder Acceptance Suite (`backend/tests/founder_acceptance/`), 9 passed /
  1 evidence-backed skip.
- `CI` = GitHub Actions evidence (PR #443 vertical slice, PR #447 vps-compose-verify
  17/37 incl. REAL Ollama embed/chat, PR #458 pinned-candidate CI).

| ID | Requirement | Owner | Implementation evidence | Verification evidence | Dependencies | Status |
|---|---|---|---|---|---|---|
| FR-01 | Requirement Traceability | Z.ai | this baseline; `docs/BRANCH_REGISTRY.md`; audit `backend/docs/FOUNDER_ALPHA_KNOWLEDGE_BLUEPRINT_AUDIT.md` | every capability row cites file:line/PR/SHA; SHAs verified via bundle `git ls-remote` | composition keeps registry updated | **VERIFIED** (docs + SHA-verified artifacts) |
| FR-02 | Contract-First Integration | Z.ai | `tools/integration/freebuff_integration.py` (gate, S1-S6 fail-closed); PR #450 draft-PR contract | gate validation-only run vs current candidate `89140ba8`: 0 migration-graph conflicts, kill-switch signature match, surfaces enumerated (download/FREEBUFF_GATE_89140ba8_VALIDATION.md) | current candidate artifacts from each lane | **VERIFIED** (gate executed; candidate production still fail-closed by design) |
| FR-03 | Knowledge Integrity | Z.ai | immutable `memory_source_units` (migration 0037 triggers, `uq_msu_owner_identity` parity), find-or-create service | REAL-PG battery: `test_memory_source_units` 42/42, `test_message_source_units` 22/22 | — | **VERIFIED** |
| FR-04 | Recovery & Observability | Codex/Claude + Z.ai | `mainai_execution` recovery_takeover; `import_job` retry model | PR #443 crash→takeover→PR-once (CI); FAS scenario 9; `test_founder_recovery(_edge)` 15+7 on REAL-PG | — | **VERIFIED** (core); live-UI status surface = PARTIAL (FR-08) |
| FR-05 | Founder Acceptance Testing | Z.ai | `backend/tests/founder_acceptance/test_founder_acceptance_suite.py` (10 executable scenarios) | FAS executed on REAL-PG: 9 passed / 1 evidence-backed skip; independent HUMAN acceptance pending | founder sign-off | **VERIFIED (executed by builder; founder sign-off = open)** |
| FR-06 | Cognitive Architecture & Independent Reasoning | Codex + Freebuff | `app/mainai_execution/` planner/verify; provider registry | provider real-invoke fail-closed verified (R3); reasoning quality needs model leg | first_cycle authorization; #458 CI green | **PARTIAL** |
| FR-07 | Deep Context & Memory | Z.ai | chat write-time provenance (dbdc2cb3) + message ordinals + project memory | REAL-PG: `test_message_sequence` 42/42, `test_chat_source_capture` 6/6; FAS 1 | — | **VERIFIED** |
| FR-08 | Knowledge Reconciliation | Z.ai | `ActiveTruthStatus` (superseded/disputed), `source_relationships` (supersedes/contradicts) | FAS scenario 3 (superseded excluded from current, preserved in DB); trust-engine tests | — | **VERIFIED** (mechanism); automatic contradiction detection = model leg (FR-14) |
| FR-09 | Cross-Domain Discovery | Z.ai | `source_relationships.derived_from` owner-validated; hybrid retrieval | FAS scenario 4 (cross-project edge, real PG) | automatic discovery = P3/FR-14 | **PARTIAL** (manual/graph-edge path verified; no automatic cross-domain discovery yet) |
| FR-10 | Architecture Intelligence | Z.ai | `app/blueprint_readiness` cross-reference view (dbdc2cb3) | REAL-PG + FAS 6/7 incl. the `_VERIFIED` contract fix (`verified_by_regression_test`); unit tests 4/4 | upstream blueprint join at composition | **VERIFIED** (view); "intelligence" judgments remain evidence-derived, not model-driven |
| FR-11 | Living Blueprint | Codex (upstream) + Z.ai (attachment) | upstream `blueprint_drafts` stack (B-lane) + readiness view as attachment point | upstream tests on FETCH_HEAD only; composition pending | composition step 2 (reconciliation §3) | **BLOCKED** (composition awaiting founder-approved merge) |
| FR-12 | Cognitive Quality Gate | Codex + Z.ai | `verify_task` + planner validation; PR #454 (empty verification_plan rejected for AI repo_edit); VS-1 open | REAL-PG: startup_checks 17/17; consult suites 35+15+17+9+11 | VS-1 (empty-plan passes by convention) per PR #451 §2 | **PARTIAL** (genuine open finding named) |
| FR-13 | Evidence-Driven Evolution | Z.ai | `engineering_lessons` + readiness verdicts + regression_test links | FAS 6 (verified vs unverified distinguished); `_VERIFIED` bug found+fixed by FAS | — | **VERIFIED** |
| FR-14 | Explicit cognitive behavior: verify missing context, challenge conclusions, discover cross-domain connections, require evidence before architectural changes (expands FR-09/12/13) | Codex + Freebuff + Z.ai | acceptance criteria: (a) missing-context check before answers, (b) challenge w/ alternatives, (c) cross-domain connections, (d) evidence gate on architecture changes — (c) has a real edge+retrieval path (FAS 4), (d) has readiness+lessons+verify gates (FAS 6/7, REAL-PG); (a)+(b) are model-driven | provider leg fail-closed (verified); real-model evidence = PR #447 real Ollama embed/chat, PR #458 | first_cycle authorization; model leg | **PARTIAL** — mechanisms verified, cognitive driver gated; no redundant subsystem created |
| FR-15 | Independent Parallel Engineering | Founder/Z.ai process | lanes: Z.ai/Freebuff/Codex/Claude with exact-SHA checkpoints; gate-mediated Freebuff composition | this delivery: 3-agent evidence (Z.ai bundle verified; Freebuff gate run; Codex/Claude PRs cited) | coordination discipline (Merge-regel) | **PARTIAL** (process working; independent REVIEW of Z.ai work still pending — no agent's self-report counts) |
| FR-16 | Capability Registry | Codex (upstream 0080) + Z.ai | provider registry (`app/providers/registry.py`); verification registry in composed candidate A | registry tests; A-lane not composed | composition | **PARTIAL** (Z.ai-side verified; A-lane registry blocked on composition) |
| FR-17 | Continuous Architecture Reconciliation | Z.ai | `docs/BRANCH_REGISTRY.md` + this file + `MIGRATION_GRAPH_RECONCILIATION_20261118.md` + disposable-PG ops scripts (`backend/scripts/ops/`) | reconciliation executed against the CURRENT graph (not history); REAL-PG alembic roundtrip+composition OK | repeat after each composition step | **VERIFIED** (process + executable tooling) |
| FR-18 | Adaptive Reasoning & Compute | Codex + Z.ai | consult layer budgets/policy/workforce/metrics (5 suites), adaptive depth in provider layer | REAL-PG consult suites green (87 tests) | model leg for end-to-end adaptation | **PARTIAL** (cost/policy layer verified; runtime adaptive depth unproven) |
| FR-19 | Proactive Knowledge Discovery | Claude (PR #395 open) + Z.ai | `github_evidence_candidates` (PR #440 test fix); discovery wiring = PR #395 (open) | per PR #451 §2: wiring still open | PR #395 merge | **PARTIAL** |
| FR-20 | Memory Lifecycle Management | Codex + Z.ai | erasure chains (0021-0023 Z.AI lane; 0084-0086 composed candidate A), immutable originals + backfill runs | REAL-PG: MSU lifecycle triggers 42/42; erasure tests in lane tests | composition for A-lane lifecycle | **VERIFIED** (Z.AI-lane core); A-lane erasure = BLOCKED (composition) |
| FR-21 | Agent Orchestration & Governance | Codex/Claude | `mainai_execution` cancellation/takeover/verification; supervision chain in A | PR #443 vertical (CI); REAL-PG founder-recovery suites; AV-1a/2a findings open per PR #451 | A composition; AV findings | **PARTIAL** (core verified; named open findings) |
| FR-22 | Source-Level Provenance | Z.ai | write-time S1C capture at all 3 message-write sites + idempotent backfill | REAL-PG: capture 6/6, MSU subtype+triggers 22/22; FAS 1 | — | **VERIFIED** |
| FR-23 | Project & Product Knowledge Graph | Z.ai | documents+chunks+sources+relationships (pgvector hybrid; no second engine) | FAS 2/4 on REAL-PG; REAL-PG retrieval suites | graph-style traversal = P3 (no framework without evidence) | **PARTIAL** (persistence+retrieval verified; graph traversal queries not built yet) |
| FR-24 | Security & Privacy by Design | All + Z.ai | RLS + restricted `mainai_app` role (s1a policy); egress gates (PR #456 open); token handling; PR #452 worktree-token fix (open) | REAL-PG: `test_rls_isolation` 18/18, `ensure_app_role` 9/9; FAS 8 (cross-owner read blocked; TRUNCATE refused); **hardening observation:** s1a floor grants app role DELETE on `alembic_version` (reported, not silently accepted) | PROV-1, VS-1, egress PRs remain open per PR #451 §2 | **PARTIAL** (core verified; named open risks) |
| FR-25 | Epistemic State Management | Z.ai | `ActiveTruthStatus`, `memory_truth_claims` (0063), claim-level trust (0007) | FAS 3; REAL-PG battery includes claim tables' migrations | trust scoring = model leg | **PARTIAL** (state machine verified; claim confidence scoring needs model leg) |
| FR-26 | Contextual Decision Intelligence | Z.ai | `context_resolver` (26 tests), consult policy/budgets | REAL-PG green; decision-quality = model leg | FR-14 model leg | **PARTIAL** |
| FR-27 | End-to-End Knowledge Coverage | Z.ai | ingestion→chunking→index→hybrid retrieval→citation path; readiness coverage summary | FAS 2 (import+cite) + REAL-PG RAG suites; coverage METRICS across all knowledge = P3 | full-coverage measurement | **PARTIAL** (vertical proven end-to-end on real services; organization-wide coverage metrics not yet built) |

**Legend of verification provenance:** all REAL-PG/FAS executions were performed by the
Z.ai lane (builder) in this sandbox — they are reproducible via
`backend/scripts/ops/bootstrap_real_pg.sh` + `verify_real_pg_phase3.sh` +
`pytest tests/founder_acceptance/`. Independent verification paths: CI evidence (cited),
Freebuff gate (separate tool), and founder/human acceptance (open, FR-05).

## P3 — Smallest safe increments (no new frameworks without concrete advantage)

1. **Graph-style traversal over existing tables** (FR-23/FR-09): recursive CTE over
   `source_relationships` + readiness joins — pure SQL, no GraphRAG framework; evaluate a
   GraphRAG-style pipeline ONLY after measuring recall/precision of the hybrid+edge model
   on real corpora (benchmark exists: `test_billing_benchmark` pattern for cost; extend to
   retrieval quality).
2. **Archival/working memory separation** (FR-07/FR-20): the schema already separates
   immutable `memory_source_units` (archival) from chat/project context (working) — the
   increment is a promotion/recall policy (archived unit → working context on relevance),
   implemented as an owner-scoped query layer, not new storage.
3. **Durable agent workflows** (FR-04/FR-21): `mainai_execution` already has
   checkpoint/resume/takeover; the increment is the missing live-status surface (FR-08's
   PARTIAL half) consuming existing `MainAITaskEvent` rows.
4. **Model-leg enablement** (FR-06/12/14/18/25/26): a single switch — founder-authorized
   provider activation per the existing fail-closed envelope; then run FAS scenario 5's
   model leg and the adaptive-depth benchmarks. This is the highest-leverage increment and
   is BLOCKED only by `first_cycle_go`.
