# Freebuff Compatibility Specification — Cognitive Core (Round 12)

**Cognitive branch:** `zai/alpha-runtime-reliability` @ `df9150b4` (chain: R9 `fd413518` → R10 `9b0cb7ac` → R11 `df9150b4`)
**Operational branch (Freebuff):** reported local HEAD `c59cbc8`, PostgreSQL migration **0079**, working founder workflow, existing prompt-fencing module.
**Method:** every "CONFIRMED" claim below is verified by an executed test in this repository (`tests/backend/test_integration_contracts_r11.py` and the R11/R12 suites). Every "ASSUMPTION" is flagged and needs one verification step in Freebuff's environment. No replacement interfaces are invented.

## 1. Cognitive Core module imports (CONFIRMED on the cognitive branch)
New modules and their only intra-repo dependencies — all resolve inside `backend/app`:
- `app/mainai_evals/` (`cognitive_gate.py`, `model_gate.py`) → `app/models/cognitive_gate.py`, `app/providers/*`
- `app/providers/zai_provider.py` → `app/providers/base.py` only. **Evaluation-only, NOT registered in the default registry** — production behavior unchanged unless an admin inserts a `provider_config` row with `provider='zai'`.
- `app/mainai_execution/improvement_proposals.py` + `app/routers/improvement_proposals.py` → `app/models/improvement_proposal.py`, `app/models/audit.py`. Router wired in `app/main.py` (one import + one `include_router`).
- `app/rag/untrusted_content.py`, `app/rag/intellectual_adversary.py`, `app/rag/scientific_reasoning.py`, `app/rag/graph_instructions.py` (modified) — import graph documented in `docs/INTEGRATION_MANIFEST_R11.md` §1.
**Verification step for Freebuff:** `python -c "import app.main"` after merge resolves the two-line `main.py` delta (conflict risk LOW, additive).

## 2. Alembic migrations 0075–0077 vs 0079 (CONFLICT ANALYSIS — decision needed, two sanctioned paths)
Cognitive chain: …0074 → **0075** (cognitive_gate_runs + case results; founder-wide, NOT RLS — same precedent as 0013) → **0076** (mainai_improvement_proposals; ENABLE+FORCE RLS owner-scoped) → **0077** (widens `ck_cognitive_gate_runs_dataset_version` to `model_gate_%`).
Freebuff: 0079 exists. The chains DIVERGE at revision numbering, not at content — no cognitive migration touches a Freebuff table.
- **Path A (renumber, recommended, zero code edits to migration bodies):** renumber cognitive 0075/0076/0077 → 0080/0081/0082 and re-chain `down_revision` pointers (0074 → 0080 → 0081 → 0082). Migration bodies are table-creation/ALTER statements with no revision-name references inside SQL. Freebuff's 0075-0079 stay untouched. Verify: `alembic upgrade head` on a fresh DB + `alembic history` shows ONE linear chain.
- **Path B (rebase Freebuff's onto cognitive):** keep cognitive numbering, renumber Freebuff's 0075-0079 → 0078-0082. Only if Freebuff's 0075-0079 do not reference each other's revision ids inside SQL bodies (their side must confirm — ASSUMPTION).
- 0077's CHECK-widening is idempotent-safe: if applied before 0075, it fails (no table) — renumbering must preserve ORDER (0075-content first, 0076-content, 0077-content last).
**Verification step:** run the migration chain on a scratch DB from both paths; the R11 acceptance suite asserts head ≥ 0077 (`test_contract_migration_chain_head_and_core_tables`) — update the assertion constant to the merged head after renumbering (one line).

## 3. Database ownership and RLS (CONFIRMED)
- Contract: session-scoped GUC `app.current_user_id` set via **`SET LOCAL`** (dies at commit) + `FORCE ROW LEVEL SECURITY` on owner-scoped tables. Every service call sets it per transaction — callers MUST use `app/request_context.py`'s contextvar pattern (see any R11 test).
- Owner-scoped (RLS): `mainai_improvement_proposals`, `knowledge_claims`, `claim_evidence_metadata`, `document_chunks`, `life_problem_decisions`, … Founder-wide (no RLS, founder-account-gated access only): `cognitive_gate_runs`, `cognitive_gate_case_results`.
- **ASSUMPTION about Freebuff:** your session management already sets `app.current_user_id` per request (your RLS tables exist since earlier rounds). Verify with one query: `SELECT current_setting('app.current_user_id', true)` inside any request handler.

## 4. Persistent memory and decision storage (CONFIRMED)
- `life_problem_decisions`: supersession via `supersedes_decision_id` (append-only; prior rows never edited; content preserved verbatim); `decision_superseded` audit row carries `{prior_decision, new_decision, superseded_decision_id, superseding_decision_id, ...}`.
- Provenance chain: claim → chunk → source → evidence-metadata stamp; `trace_evidence_chain` + `quality_metadata_for_claims` are the read APIs.
- **No second memory system exists and none may be added** — integration must reuse these tables.

## 5. Model-provider interfaces (CONFIRMED)
- Single seam: `LLMProvider` ABC (`chat`, `embed`, `is_configured`) + `registry.resolve_active(db, role)` reading `provider_config` rows (role `chat`/`embedding`) — the ADMIN configuration path. Swapping providers never touches routers/RAG code.
- Verification regime for a new provider: the R11 model-gate pattern (`app/mainai_evals/model_gate.py`) — run-scoped activation, majority-of-3 trials, honest quota skips, full restore asserted. **No automatic promotion/replacement.**
- Freebuff's installed Ollama runtime: ASSUMED reachable from the operational environment (absent in the cognitive sandbox by design). The adapter interface accepts it without code change; a `provider_config` row (`provider='ollama'`) is the switch.

## 6. Retrieval and evidence provenance (CONFIRMED)
- Retrieval: hybrid vector (pgvector cosine) + ILIKE text channel, owner-scoped, deleted-source excluded. **No distance floor** — best-effort ranking; the R11 relevance gate (`graph_instructions.top_hit_text`) governs the expensive prompt lines.
- Evidence metadata: `record_claim_evidence_metadata` with a CLOSED declared-axis vocabulary (study_design, sample_size, study_setting, population, exposure_duration, outcome_followup, dose_response_documented, interactions, conflicts_of_interest, competing_explanations, causal_claim, causal_claim_supported_by_design, research_topic, declared_via). Unknown axes are reported, never guessed.

## 7. Background jobs and cancellation (CONFIRMED on cognitive side)
- The cognitive branch adds NO job types. The existing supervisor lease (`app/development_supervisor/lease.py`) is fenced-UPDATE based; cancellation = lease release. **ASSUMPTION:** Freebuff's operational lane owns all job scheduling; the cognitive lane's only runtime work is on-demand (gate runs, evidence-chain computation).

## 8. Founder-only authorization (CONFIRMED)
- TWO layers, both tested: (1) `require_founder` checks THE founder ACCOUNT (`founder@lifeos.local`) — founder-ROLE-but-other-account users get 403 "MainAI är endast tillgänglig för grundarkontot."; (2) service-level checks raise `ForbiddenDecision` before any query (improvement proposals).
- No execution path exists from proposals to privileged actions (structural: the module cannot import any authoritative writer — verified by source inspection + end-to-end integrity test).

## 9. Audit logging (CONFIRMED)
- Table `audit_log`; cognitive vocabulary additions: `decision_superseded`, `improvement_proposal_decision`, `improvement_proposal_implemented`, `improvement_proposal_reverted`. `detail` is JSON text; no secrets. Freebuff's existing actions are untouched — additive only.

## 10. Prompt-fencing and untrusted source handling (CONTRACT — the unification point)
- Cognitive canonical fence: `app/rag/untrusted_content.py` — one policy line + paired markers, used by BOTH `chat.py` and `workbench.py` (import-graph asserted).
- The **C1-C7 contract** (in `tests/backend/test_integration_contracts_r11.py`) is implementation-agnostic: C1 policy precedes every frame; C2 payload strictly inside markers; C3 rule rides every frame; C4 marker-smuggling defanged + attempt flagged; C5 single canonical source; C6 empty hits → honest fallback; C7 EVERY chunk framed (no suspicion heuristics).
- **Unification requirement:** Freebuff's shared prompt-fencing module must EITHER (a) become the implementation behind `build_untrusted_context_block`/`frame_untrusted_source_content` and pass C1-C7 — the cognitive call sites import it unchanged, or (b) keep two modules but prove both satisfy C1-C7 with C5 narrowed to "one implementation per assembled prompt". Option (a) is preferred: ONE boundary, not two.
- **ASSUMPTION:** Freebuff's module frames the same surface (retrieved chunks in system prompts). Verify by running the C-suite against their module; their CVE-equivalent to check FIRST is C4 (marker smuggling — the R12-fixed escape hatch).
