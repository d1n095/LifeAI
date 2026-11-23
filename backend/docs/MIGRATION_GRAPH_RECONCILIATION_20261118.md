# Migration graph reconciliation — CURRENT state (2026-11-18, supersedes the §3 mapping in MIGRATION_RECONCILIATION.md)

**Why this addendum:** the earlier mapping (Z.AI 0069-0072 → 0074-0077, onto
`0073_provider_disclosure_local`) was computed against the founder-alpha lane as it stood
on 2026-11-18 morning. GitHub has since moved: `codex/founder-alpha-final-composed-candidate`
occupies 0069-0088, so **0074-0077 are taken**. The current dev tip `69dc5ea5` still has a
single head at **0068_workforce_ops** — every 0069+ lane is an unmerged fork.

## 1. The actual current graph (verified from fetched refs, 2026-11-18)

| Lane (branch @ tip) | Migrations | Content |
|---|---|---|
| **dev** `claude/det-kommer-mer-879lcm` @ `69dc5ea5` | 0001-0068, single head `0068_workforce_ops` | shared base |
| A. `codex/founder-alpha-final-composed-candidate` @ `2fbe20aa` | 0069-0088 | workforce authority, personal-recall outbox/delivery, mainai execution events, level-2 programs, supervision/fences/telemetry, founder boot, workforce mastery, verification registry, personal-recall activation/authority/erasure lifecycle, account-erasure authority/reauth/completion, entity-relationship vocabulary |
| B. `codex/founder-alpha-approved-precomposition` @ `7271f72d` | 0069-0073 | `0069_embedding_space_identity`, `0070_blueprint_drafts`, `0071_blueprint_approval_integrity`, `0072_blueprint_approve_identity`, `0073_provider_disclosure_local` — **the Founder Alpha blueprint stack** |
| C. Freebuff `freebuff/local-ai-integration-20261118` @ `89140ba8` (= PR #457) | based on B | local-AI/embedding work + `0069_embedding_space_identity` inherited |
| C2. Freebuff CI `freebuff/ci-readiness-20261118` @ `94367545` (= PR #458) | — | pinned local-AI candidate verified against real Ollama + pgvector in CI |
| D. `claude/import-security-integration-on-dev` @ `0b18aeb2` (PR #447 chain) | own 0069 fork | import-security integration (genuine Alembic fork, both lanes declare `down_revision = "0068"`) |
| E. `zai/alpha-knowledge-blueprint` @ `1e2cfdc8` | 0069-0072 | `0069_founder_email_recovery`, `0070_billing_foundation`, `0071_advertising_tables`, `0072_usage_idempotency` (Z.AI lane) |

All three of A/B/D/E (and C via B) fork at `0068` with **overlapping revision numbers but
disjoint objects**. Z.AI lane migrations are additive-only (CREATE TABLE / ADD COLUMN on
objects no other lane touches: `founder_email_change_requests`, `billing_*`, `usage_*`,
`revenue_ledger`, `ad_*`).

## 2. Freebuff compatibility evidence (gate, validation-only run vs current candidate)

`tools/integration/freebuff_integration.py` (from `zai/integration-prep` @ `e69e4d95`),
run 2026-11-18 in `--fixture` mode (validation only — **no integration candidate produced,
fail-closed as designed**; the gate's hard-pinned candidate `e392022` is stale, current
candidate is PR #457's `89140ba8`):

- shared ancestry with base `69dc5ea5` ✅ merge-based integration possible
- migration graphs: mine=69 revisions, theirs=72, 69 shared, **0 conflicts** ✅
- kill-switch signature `assert_not_killed()` matches — no adaptation needed ✅
- surfaces: 78 shared settings keys (verify no semantic drift), 24 candidate-only keys;
  routers/models: no candidate-only conflicts ✅
- **`openai_consult` collision**: candidate also defines the 8 consult modules — both lanes
  descend from shared ancestry; reconcile content-level differences at composition, do not
  silently overwrite either side

## 3. Recommended composition order (founder-approved merge only)

Invariants: ONE alembic head at every step; chain order WITHIN a lane preserved; parity
tests (`test_billing_engine.py`, `test_founder_recovery_edge.py`) updated in the same commit
as any renumbering; never squash/rewrite/drop migrations; disposable-PG verification after
each step (procedure: `backend/scripts/ops/bootstrap_real_pg.sh` +
`backend/scripts/ops/verify_real_pg_phase3.sh`).

1. **A** `codex/founder-alpha-final-composed-candidate` (0069-0088) onto dev — highest chain, most content.
2. **B** blueprint stack renumbered onto A's head: `0069_embedding_space_identity`→`0089_…`, `0070_blueprint_drafts`→`0090_…`, `0071_blueprint_approval_integrity`→`0091_…`, `0072_blueprint_approve_identity`→`0092_…`, `0073_provider_disclosure_local`→`0093_…`.
3. **E** Z.AI lane renumbered onto the result: `0069_founder_email_recovery`→`0094_…`, `0070_billing_foundation`→`0095_…`, `0071_advertising_tables`→`0096_…`, `0072_usage_idempotency`→`0097_…`. (This SUPERSEDES MIGRATION_RECONCILIATION.md §3's 0074-0077 target — those numbers are occupied by A's supervision chain.)
4. **D** import-security lane renumbered onto `0098+` (founder's merge-order call; the second 0069 fork).
5. **C** Freebuff via the gate LAST: re-run `freebuff_integration.py` against the post-composition base with the CURRENT candidate (`89140ba8` lineage); the gate produces the candidate only after its hard gate passes; its candidate-only revisions (if any beyond B's) renumber onto the resulting head. Exit 2 = stop and report; NO-GO is a valid outcome.

If the founder orders the lanes differently, the mapping shifts but the invariants and the
mechanical procedure (git mv + revision/down_revision edits + parity tests in the same
commit) do not change. Numbers in this table are the concrete proposal, not a promise: the
renumbering happens at composition time, by whoever performs the approved merge, on
disposable-PG-verified results.
