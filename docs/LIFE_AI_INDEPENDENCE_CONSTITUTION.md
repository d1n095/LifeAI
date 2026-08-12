# Life — AI Independence Constitution

**Status:** Architecture-only, produced in the same discovery pass as
`docs/LIFE_CANONICAL_ARCHITECTURE.md`. No code written. This document is the durable, named
statement of the founder's §0 "grundlag" — it exists so future sessions (human or MainAI) can
check a decision against it without re-deriving the principle from a conversation.

**Provisional note (2026-08-11 correction):** the law itself (§1) does not depend on the
founder's full external corpus and is not provisional. The AI-dependency matrix it cites (§4,
sourced from `docs/LIFE_CANONICAL_ARCHITECTURE.md` §G) IS part of the provisional bootstrap map
and may be refined once the full corpus is ingested — see that document's status note. Nothing
here blocks acting on the law now; it only means the specific subsystem classifications in §4
should be re-checked, not re-derived from scratch, once the final Canonical Architecture ships.

**Relationship to a different, similarly-named existing concept — read this before anything
else:** `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` §4.4/§6.7 already designs something called
"MainAI:s konstitution" (P7A/P7B, `governance_documents`). **That is a different thing from this
document, on purpose, and the two must not be merged into one:**

| | This document (AI Independence Constitution) | P7A/P7B `governance_documents` |
|---|---|---|
| What it governs | The structural boundary between Life Core and LifeAI — what must never require AI | What behavioral rules MainAI itself follows (imported policy documents, activated by the founder) |
| Who "writes" it | Architecture passes like this one, approved by the founder | The founder, by uploading/approving a document through Life Library |
| Enforcement mechanism | DB privileges, module-dependency rules, CI checks (this document, §3) | A two-gate approval flow + a `status=active` prompt-fragment injection (already designed, not built) |
| Mutability | Changes require an explicit architecture-review pass like this one | Changes are the whole *point* of the mechanism — founder uploads a new/revised policy document |

P7A/P7B remains the correct design for "founder-authored rules MainAI's *prompts* obey." This
document is the correct place for "which subsystems are structurally forbidden from requiring
AI at all," which is a code/schema/privilege property, not a prompt property. Building P7A/P7B
later should reference this document, not duplicate it.

---

## 1. The law

**Life must function without MainAI/LifeAI and without any external AI/LLM.** MainAI is
intelligence, monitoring, control, and orchestration *on top of* Life — never a foundation Life
requires to function. External LLM providers are replaceable tools/teachers MainAI may use, not
MainAI itself, and never Life itself.

This is not a new invention — `docs/MAINAI_ARCHITECTURE.md` §1 already states this almost
verbatim (System Core / MainAI / External models, written by a prior session before this
mandate existed). This document adopts that principle as binding, gives it a name and a home,
and — the part that was previously missing — makes it **structurally checkable**, not just
prose.

## 2. What "AI-independent" actually means, precisely

A subsystem is AI-independent when:

1. Its own module never imports `app/providers/` (directly or transitively).
2. Its correctness/availability does not degrade when every provider in
   `provider_verification_checks` reports `unreachable`/`invalid_key`.
3. Its data (reads and writes) never passes through an LLM call as a required step — an LLM may
   *enrich* the data (e.g. embeddings on top of raw stored text) but the raw operation must
   succeed without that enrichment.

This is exactly the test already implicitly proven by two pieces of real code in this repo,
which should be treated as the reference pattern for everything else in §4:

- `app/rag/vector_store.py`'s `hybrid_search(vector=None)` — a tested, real fallback to text
  search when the embedding provider is unavailable (PR #18). This is AI-independence *already
  achieved*, not theoretical.
- The durable worker/job runtime (`app/jobs/`, `app/worker.py`) — leasing, heartbeat, retry,
  crash recovery contain zero AI calls, proven across V0.1-V0.3's entire crash-recovery demo
  matrix.

## 3. Enforcement — structural, not prompt policy

The founder's mandate is explicit that this must be "tekniskt framtvingat av storage/DB
permissions, inte bara promptpolicy." Three concrete, cheap mechanisms, all following patterns
that already exist elsewhere in this codebase (not invented for this document):

1. **Module-dependency check.** A CI-enforceable rule (same shape as the existing
   `test_storage_local_fs.py` AST-allowlist tests that already check write-path discipline):
   `app/storage/`, `app/rls.py`, `app/deps.py`, `app/jobs/` (the runtime, not individual
   handlers), and `app/worker.py`'s tick-scheduling code must never import `app/providers`.
   Cheap to write, cheap to keep green, catches the regression class this document exists to
   prevent.
2. **DB privilege floor.** Exactly the model S1A already established for provenance tables
   (`REVOKE`, `SECURITY DEFINER`, verified via `has_table_privilege`/`has_function_privilege`
   queries at every boot, not just at migration time) extends naturally to a future Source Vault
   invariant (see `docs/LIFE_SOURCE_VAULT_AND_MEMORY_ARCHITECTURE.md`) — `mainai_app` should
   hold no privilege that would let *any* code path, AI-driven or not, silently rewrite a
   canonical original.
3. **Capability declaration.** Once the capability registry (`docs/LIFE_CANONICAL_ARCHITECTURE.md`
   §H) exists, every capability must declare `local_available`. A capability that claims
   `local_available=true` but calls a provider is a testable contradiction — a unit test can
   assert the claim against the module-dependency check in point 1.

None of these three are built yet. All three are additive, all three follow an existing proven
pattern in this codebase, and none require a rewrite.

## 4. The AI-dependency matrix (authoritative copy)

See `docs/LIFE_CANONICAL_ARCHITECTURE.md` §G for the full matrix with evidence per subsystem.
Summary: storage, auth/permissions/audit, jobs/queue/worker/retry runtime, archive
extraction/checksums/dedup, file-type detection, chunking, MainAI's own orchestration mechanics
(locking/leasing/checkpointing/dead-worker detection) are **already AI-free today, in practice**
— the classification work in this document is naming and testing something largely already
true, not redesigning the system. The genuine gaps are: embedding (has a partial precedent to
generalize from), chat, claim extraction, MainAI's planner, and MainAI's replanning/
lesson-conflict detection — all five explicitly and permanently require AI by their nature (a
chat feature without any model is not a smaller chat feature, it's not chat), so the correct
target classification for all five is **AI_REQUIRED_TODAY**, not a bug to fix — the constitution
is about making sure nothing *else* silently joins that list by accident, not about eliminating
these five.

## 5. What this document does not decide

- It does not decide *whether* a local/on-device model (Ollama or otherwise) becomes the
  fallback for the five AI-required subsystems above — that's a real, separate, larger
  engineering decision (`MAINAI_ARCHITECTURE.md` §1's "Local MainAI Capability Layer" target),
  correctly left as FUTURE in `docs/LIFE_REQUIREMENT_TRACEABILITY.md`.
- It does not decide governance-document content (P7A/P7B, §0 above) — that's founder-authored
  policy text, not architecture.
- It does not change any code. It is the checkable statement against which future code changes
  should be reviewed — "does this new capability need to call `app/providers/`, and if so, is
  that genuinely inherent to what it does, or did we just take the easy path."
