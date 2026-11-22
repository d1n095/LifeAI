# Founder Alpha — Knowledge & Living Blueprint: Gap Audit (2026-11-18)

Scope: the five foundation areas for a working **private MainAI** on LifeAI, audited
against the working tree of `zai/engine-hardening` @ `f9ddf0be` plus the upstream
founder-alpha lane (`codex/founder-alpha-approved-precomposition`, shallow ref
`FETCH_HEAD`) and Freebuff's new head (`freebuff/local-ai-integration-20261118` @
`6c537742`, read-only). Status vocabulary: **IMPLEMENTED** (code exists) / **TESTED**
(automated coverage exists) / **PARTIAL** / **DESIGN-ONLY** / **MISSING**. Branch with
the fixes from this audit: `zai/alpha-knowledge-blueprint` (see §6).

## Area 1 — Lossless ingestion (conversations, messages, documents, books, files, archives) with immutable source references, resumability, isolation

| Capability | Status | Evidence |
|---|---|---|
| Chat message capture, lossless (commit-before-provider, honest failure rows, retry idempotency) | **IMPLEMENTED + TESTED** | `backend/app/routers/chat.py` (module docstring; `_attempt_assistant_reply` failure branch persists `MessageStatus.failed`); `backend/app/models/conversation.py` (`Message`, DB-assigned `sequence_number`, migration 0030 trigger); tests `backend/tests/backend/chat/test_chat_message_persistence.py` (`test_provider_fails_before_producing_output_saves_user_message`), `chat/test_message_sequence.py` (40+ tests incl. concurrency + backfill resume) |
| DB-assigned immutable message ordinals | **IMPLEMENTED + TESTED** | migration `0030`; `test_new_message_is_numbered_by_the_database_without_application_code`, `test_an_assigned_sequence_number_cannot_be_changed`, `test_two_concurrent_inserts_into_one_conversation_never_share_an_ordinal` |
| Immutable provenance core (S1A/S1C `memory_source_units`, sha256, trigger-enforced) | **IMPLEMENTED + TESTED (PG)** | `backend/app/models/memory_source_unit.py`, `backend/app/rag/memory_source.py`, `backend/app/rag/message_source.py`, migration `0037` triggers; `rag/test_memory_source_units.py` (42 tests), `rag/test_message_source_units.py` (20) — **all PG-host-blocked in this sandbox, run in `.github/workflows/verify.yml`** |
| Message provenance at write time (not only via backfill) | **PARTIAL → FIXED on this branch** | was backfill-only (`app/rag/backfill/message_source.py:80` sole caller); chat now calls the same service at all three write sites (`app/routers/chat.py` `_capture_message_source`, commit `dbdc2cb3`); `tests/backend/test_chat_source_capture.py` (6 tests incl. backfill convergence) |
| Document/file ingestion (pdf/docx/txt/md/json/html/csv/mp3/mp4), durable storage, checksum idempotency | **IMPLEMENTED + TESTED** | `app/routers/library.py::import_package` (60 MB cap), `app/rag/library_import.py` (`run_import_job`, granular status pipeline), `app/storage/local_fs.py` (content-addressed, fsync); `rag/test_library_import.py`, `rag/test_media_import.py`, `tests/backend/test_documents_upload_durable_delivery.py` |
| Archive (ZIP) security: nested depth 3, zip-bomb defense, traversal defense, magic bytes | **IMPLEMENTED + TESTED** | `app/rag/zip_import.py`; `rag/test_zip_import_security.py` (40 tests), `rag/test_zip_import_capacity.py` (2500-file capacity) |
| Resumability (lease claim/reclaim, transient retry, orphan reconciliation, backfill resume) | **IMPLEMENTED + TESTED** | `app/jobs/lease.py::claim_next_job` (SKIP LOCKED), `app/worker.py` (`_reconcile_orphaned_documents`), `app/rag/library_import.py::_resume_incomplete_document`; `jobs/test_worker.py`, `jobs/test_job_lock.py`, `rag/test_library_import.py` resume tests, `rag/test_message_source_units.py::test_backfill_crash_between_batches_resumes_without_losing_or_duplicating` |
| Isolation (FORCE-RLS on every ingestion table, egress gate, erasure) | **IMPLEMENTED + TESTED (PG)** | `app/rls.py` registry + drift test `test_rls_policy_registry.py`; `tests/security/test_rls_isolation.py`; egress: `app/egress_policy/service.py`, `mainai/test_egress_policy.py` |
| Formats: epub/mobi/odt/xlsx/pptx, OCR, tar/7z/rar archives; ≥2 GB books | **MISSING / DESIGN-ONLY** | no extractors exist (`app/rag/extract.py` table); `docs/LARGE_FILE_UPLOAD_PLAN.md` is plan-only; audio/video inside archives deliberately skipped (`app/rag/media_import.py` docstring) |
| External conversation-history import (source_import_batches pipeline) | **MISSING** | `app/models/source_import_batch.py` columns exist (`discovered_conversations/discovered_messages`), no pipeline populates them |
| Normative S1C spec doc | **INTENTIONALLY ABSENT from this branch (not a blocker)** | `docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md` is cited ~15× (e.g. `app/models/source_import_batch.py:26`, `app/rag/corpus_batch.py:1`) but its absence is deliberate per `docs/BRANCH_REGISTRY.md:1942` — the doc lives on PR #60's design branch, and the hardening lessons it prescribes persist machine-readably in `engineering_lessons` via `scripts/mainai/seed_life_source_foundation_hardening_lessons.py`. No reconstruction needed; reconcile doc location at composition time. (Correction 2026-11-18b: the earlier version of this audit mislabeled this as MISSING/blocker, and also listed `MAINAI_CONTEXT_BUNDLE.md` as missing — it exists at the repo ROOT, cited by name from `app/rag/library_import.py:417`.) |

## Area 2 — Structured extraction & reconciliation (ideas, decisions, products, components, builds, projects, requirements; provenance, history, contradictions)

| Capability | Status | Evidence |
|---|---|---|
| Entity model + aliases + interpretation proposals | **IMPLEMENTED + TESTED** | `app/models/project_entities.py` (`ProjectEntity`, `ProjectEntityAlias`, `InterpretationProposal`), migration `0056` (owner-anchored composite FKs, commit `32d9fae8`) |
| Deterministic reconciliation (SAME/partial-overlap/contradicts/supersedes; aliases; never collapse contradictions) | **IMPLEMENTED + TESTED** | `app/concept_reconciliation/service.py` (commit `717b82fe`, migration `0064`); `mainai/test_concept_reconciliation.py` incl. `test_contradicts_never_collapses`, `test_partial_overlap_does_not_collapse_to_same` |
| Idea → work candidate with explicit authorization, provenance-preserving dismissal/supersession | **IMPLEMENTED + TESTED** | `app/work_candidates/service.py` (commit `b257af71`, migrations `0055`/`0065`); `test_work_candidates.py` (12 tests) |
| Candidate learning signals, staged entity resolution | **IMPLEMENTED + TESTED** | `app/models/candidate_learning_signal.py` (commit `1a929609`, migration `0066`); `test_candidate_learning_signals.py` |
| Contradiction/history vocabulary (supersedes_* FKs, `ActiveTruthStatus`, contradicts edges, append-only events) | **IMPLEMENTED + TESTED** | `app/models/claim_relationship.py` (`contradicts`/`supersedes`), `app/models/document.py:110` (`historical/superseded/disputed`), `app/models/founder_memory.py` (`supersedes_note_id`), `app/rag/trust.py::detect_conflicts`; `rag/test_trust_engine.py` |

## Area 3 — Living, versioned blueprint (requirements ↔ architecture ↔ deps ↔ resources ↔ implementation locations ↔ GitHub code/PRs ↔ tests ↔ verification ↔ next actions)

| Capability | Status | Evidence |
|---|---|---|
| Versioned, approval-gated blueprint drafts (immutable after approval, RLS, DB triggers, session_user identity guard) | **UPSTREAM-ONLY** | `FETCH_HEAD`: `app/blueprint/service.py` (`generate_blueprint_draft` refuses ungrounded drafts), `app/models/blueprint_draft.py`, `app/routers/blueprint.py`, migrations `0070_blueprint_drafts`/`0071_blueprint_approval_integrity`/`0072_blueprint_approve_identity`; upstream tests `test_blueprint_draft.py`, `test_blueprint_approval_security.py` (9), `test_founder_alpha_blueprint_e2e.py` (10). **Zero occurrences locally** — requires the migration renumbering in `backend/docs/MIGRATION_RECONCILIATION.md` before composition |
| Cross-reference view (knowledge ↔ code/PRs ↔ tests ↔ verification ↔ next actions) | **MISSING → IMPLEMENTED + TESTED on this branch** | new `app/blueprint_readiness/service.py` + `GET /api/workbench/blueprint-readiness` (commit `dbdc2cb3`), aggregating documents/`project_sources`/`project_branch_pr_statuses`/`engineering_lessons`/`agent_tasks`; `tests/backend/test_blueprint_readiness.py`. The upstream blueprint joins via `source_document_ids` — no second engine, no new tables |
| Local closest ancestor (founder-labeled AI conclusions with provenance edges) | **IMPLEMENTED + TESTED** | `app/routers/workbench.py::save_result` (`_LABEL_MAPPING`, `SourceRelationship.derived_from` owner-validated) |

## Area 4 — Fast, accurate, permission-scoped retrieval for MainAI; unverified AI interpretations never authoritative

| Capability | Status | Evidence |
|---|---|---|
| Owner-scoped vector + hybrid keyword retrieval, soft-deleted sources excluded, RLS-enforced | **IMPLEMENTED + TESTED (PG)** | `app/rag/retrieve.py` (owner_id required, egress-gated, commit `87450e61`), `app/rag/vector_store.py::search/hybrid_search`; `rag/test_search_failure_boundary.py`, `tests/security/test_rls_isolation.py` |
| Trust engine (confidence caps on non-active sources; `contradicts` → conflict; no_basis unrescuable; certain only via independent corroboration) | **IMPLEMENTED + TESTED** | `app/rag/trust.py` (commit `7905f9e7`); `rag/test_trust_engine.py` |
| Authority vocabulary + non-automatic promotion (candidate signals; `authority="ai_interpretation"` ceiling; promotion is explicit) | **IMPLEMENTED + TESTED** | `app/models/founder_memory.py:26` (`FOUNDER_MEMORY_AUTHORITIES`), `app/founder_memory_signals/service.py:1-14` (structural rule), mirrored in `active_context.py`, `capability_reality.py`, `diagnosis.py`, `project_entities/service.py`, `safe_planner/service.py` (`NON_AUTHORITATIVE_KINDS`); `test_candidate_learning_signals.py` |
| MainAI cannot write memories via HTTP; consult surface reads no memory/documents | **IMPLEMENTED + TESTED** | all memory routes founder-gated (`routers/memory.py`, `routers/inspectable_memory.py`); `test_openai_consult_mainai.py::test_openai_consult_package_performs_no_memory_or_document_reads` |
| Deterministic intent context resolver (never infers psychological state) | **IMPLEMENTED + TESTED** | `app/context/resolver.py` (commit `5c63f14e`); `test_context_resolver.py` (27 tests) |
| Restricted runtime role derivation fail-closed (empty `MAINAI_APP_PASSWORD`, no URL echo) | **PARTIAL → FIXED on this branch** | fix existed only on unmerged `zai/fix-ensure-app-role-derivation-fail-closed` @ `bbf32c52` (not an ancestor; 75 files behind); cherry-picked as `560f0528` |
| Candidate-signal promotion via HTTP (founder review UI path) | **MISSING (low priority)** | promotion exists only as a service function; no endpoint |
| Project-memory retrieval accuracy (Jaccard keyword, no embeddings) | **PARTIAL (acknowledged)** | `app/project_memory.py::retrieve_relevant_context` docstring — deliberate no-new-infrastructure choice |

## Area 5 — Controlled conversion of approved blueprint items into agent tasks (independent verification, explicit authorization for consequential actions)

| Capability | Status | Evidence |
|---|---|---|
| Idea → goal conversion (single authorized path, founder-supplied `authorized_by`, row-locked, refuses re-review) | **IMPLEMENTED + TESTED** | `app/work_candidates/service.py::authorize_work_candidate` (`:205`); `test_work_candidates.py` |
| Approval gate before any job creation; append-only grants | **IMPLEMENTED + TESTED** | `app/mainai_execution/approval.py` (`requires_approval`, `require_task_approval`, `grant_task_approval` — commit `cf6d5c5b` fixes silent AUTO-inheritance); `test_agent_dispatch_foundation.py` (23 tests incl. `test_dispatch_gate_approval_required_blocks_via_the_real_gate`) |
| Dispatch envelope (allowed paths, branch+worktree for writes, capability match) | **IMPLEMENTED + TESTED** | `app/agent_coordination/dispatch.py::evaluate_dispatch_readiness` (commit `d957615a`); `test_dispatch_assignment_authority_envelope_cannot_escape_allowed_paths` |
| Consequential-effect refusal (deploy/merge/delete/purchase/production write; UNKNOWN ≠ NO effect) | **IMPLEMENTED + TESTED** | `app/workforce/provider_worker.py:280`, `app/workforce/activation_gates.py:25` (8 named gates incl. `pr_218_consequential_confirmation`, `verification_independence`); `test_workforce_ops.py::test_unknown_external_effect_blocks_retry` |
| Independent verification (verify step separate from executor; structured plans; CI wait pinned to durable SHA; unrecognized check conclusions fail closed) | **IMPLEMENTED + TESTED** | `app/mainai_execution/verify.py`, `app/mainai_execution/ci_wait.py` (commit `2541ac92`); `test_mainai_execution_ci_wait.py` |
| Task → PR (approved verdict + recorded passing tests required; auto-merge always blocked in this slice) | **IMPLEMENTED + TESTED** | `app/agent_orchestration.py::prepare_github_pr` (`:289-359`, `_require_approved_with_test`), `app/integrations/github_client.py` (commit `692c9d79`, no merge/force-push; `github_write_enabled` default False) |

## Freebuff coordination (read-only, no branch modified)

- Freebuff's head now EXISTS: `freebuff/local-ai-integration-20261118` @ `6c537742` —
  "batched native-width Ollama embeddings, query prefix, honest local readiness",
  branched from the **upstream founder-alpha lane** (its tree contains
  `0069_embedding_space_identity`), i.e. it composes with upstream, not with the Z.AI
  lane.
- The deterministic ingestion gate (`tools/integration/freebuff_integration.py` on
  `zai/integration-prep` @ `e69e4d95`) hard-gates candidate commit `e392022`;
  ancestry vs `6c537742` is NOT verifiable from this sandbox (shallow fetch) — the gate
  itself must do the full-fetch ancestry check at composition time.
- Coordination stance unchanged: the gate remains the ONLY merge path; no Freebuff
  branch is modified; their local-AI work (Ollama embeddings) is complementary to the
  private-MainAI goal and should compose via the gate after the founder-alpha migration
  renumbering.

## 6. What this audit's branch implements (`zai/alpha-knowledge-blueprint`)

| Commit | Content |
|---|---|
| `560f0528` | cherry-pick of `bbf32c52`: fail-closed `MAINAI_APP_PASSWORD` derivation, no credential echo |
| `dbdc2cb3` | write-time message provenance (all 3 chat write sites, fail-open), blueprint readiness cross-reference view (+ founder-gated endpoint), dialect-robust source find-or-create, `uq_msu_owner_identity` model parity |
| (this doc) | `docs/FOUNDER_ALPHA_KNOWLEDGE_BLUEPRINT_AUDIT.md` |

New tests: `tests/backend/test_chat_source_capture.py` (6), `tests/backend/test_blueprint_readiness.py` (4).
Regression: all sqlite-runnable suites green; 105 PG-host-blocked tests are pre-existing
(`postgres` hostname unavailable in sandbox) and run in `.github/workflows/verify.yml`
(disposable postgres:16 + redis:7).

## Shortest path to a working private MainAI

1. **Compose the lanes** (founder-approved merge only): apply the renumbering in
   `backend/docs/MIGRATION_RECONCILIATION.md` (0069-0072 → 0074-0077), bringing the
   upstream blueprint stack (`blueprint_drafts` + approval guards) and Freebuff's
   local-embedding work into one tree; the readiness view here is the attachment point.
2. **Run `.github/workflows/verify.yml`** (disposable PG+Redis) to unlock the 105
   PG-blocked suites locally, including the provenance/immutability trigger tests.
3. **Doc-location reconciliation (small, was mislabeled a blocker earlier)**: the S1C
   spec lives on PR #60's design branch (intentional per `docs/BRANCH_REGISTRY.md:1942`);
   `MAINAI_CONTEXT_BUNDLE.md` exists at the repo root. At composition, consolidate doc
   locations — no content reconstruction is needed; the S1C hardening lessons are already
   persisted machine-readably in `engineering_lessons`.
4. **Then**, in order: enable local embeddings for retrieval (Freebuff composition),
   turn on the founder's candidate-signal review surface (HTTP promotion endpoint, low
   priority), and only after independent verification of the composed system consider
   `first_cycle_go`.
