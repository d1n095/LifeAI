# Integration Instructions — Cognitive Core → Freebuff operational branch (Round 12)

**Cognitive HEAD:** `zai/alpha-runtime-reliability` (see `R12_HANDOFF_REPORT.md` in the release package for the exact final commit + checksum)
**Freebuff HEAD:** reported `c59cbc8` @ migration 0079
**Zero-merge guarantee:** this branch does NOT merge itself; every step below is Freebuff-executed and reviewable.

## Step 1 — Transfer & verify
Copy `lifeai-cognitive-core-r12-release/` into your environment; run `bash VERIFY_AND_IMPORT.sh <target>`. All 7 verification points must pass before anything else. Checksums: `SHA256SUMS.txt`.

## Step 2 — Read the two specs
1. `docs/COMPATIBILITY_SPEC_R12.md` — ten integration surfaces, each CONFIRMED (test-verified) or ASSUMPTION (one verification step on your side).
2. `docs/INTEGRATION_MANIFEST_R11.md` — module-level delta, additive API changes, Freebuff conflict table.

## Step 3 — Migration planning (the one real decision)
Your branch: …0079. Cognitive chain: …0074 → 0075 → 0076 → 0077. Choose ONE:
- **Path A (recommended):** renumber the cognitive 0075/0076/0077 → 0080/0081/0082, re-chaining `down_revision` (0074 → 0080 → 0081 → 0082). Migration BODIES need no edits; ORDER matters (the 0077-content CHECK-widening must come after 0075-content's table creation). Your 0075-0079 remain untouched.
- **Path B:** rebase your 0075-0079 on top — only if none of your migration bodies references another revision id in SQL (your side verifies).
Verify either path: `alembic upgrade head` on a scratch DB + `alembic history` shows ONE linear chain. Then update the one-line head assertion in `tests/backend/test_integration_contracts_r11.py::test_contract_migration_chain_head_and_core_tables` to the merged head.

## Step 4 — Prompt-fencing unification (the one security decision)
Your shared prompt-fencing module + the canonical `app/rag/untrusted_content.py` must become ONE boundary. Acceptance: run `tests/backend/test_integration_contracts_r11.py` (C1-C7) against your module. Check C4 FIRST (marker smuggling — the R12-fixed escape hatch). Preferred shape: your module becomes the implementation behind `build_untrusted_context_block`/`frame_untrusted_source_content`; the cognitive call sites import it unchanged.

## Step 5 — Bring-up checks (production-shaped, all runnable before any merge)
```bash
cd backend
# deterministic cognitive suites (no model quota):
pytest tests/backend/test_integration_contracts_r11.py \
       tests/backend/test_continuity_realistic_r12.py \
       tests/backend/test_lifecycle_and_state_r12.py \
       tests/backend/test_research_intelligence_r12.py \
       tests/backend/test_controlled_learning_r11.py -q
# real-model suites (z-ai CLI or your Ollama runtime via provider_config; quota-aware):
pytest tests/backend/test_model_gate_r11.py tests/backend/test_conversation_acceptance_r12.py -q
```
Quota/availability failures skip honestly — report them as environmental, never as passes.

## Step 6 — Living Core renderer hookup (yours)
Consume `app/cognitive_state/contract.py`: `derive_cognitive_state(db, owner_id).to_dict()` → versioned events (schema_version 1.0). States are operational metaphors; do NOT render them as emotions. The closed state set and privacy envelope are enforced by `tests/backend/test_lifecycle_and_state_r12.py` — keep those green if you extend.

## Step 7 — What stays forbidden
- No autonomous privileged actions (none exist; proposals never execute — structural).
- No auto model promotion (provider swaps are admin-path only; the gate's activation is run-scoped and restored).
- `first_cycle_go=false` remains until YOUR startup review passes.
