# Founder Alpha Integration Manifest

Authoritative map of what is actually in the Founder Alpha release candidate, where it
came from, and its current status. Built by walking real git ancestry and the GitHub API
at the exact head below — not memorized or inferred from prior reports. Re-verify against
live git/GitHub state before trusting this on a later head; it describes one point in time.

**Candidate head:** `9938fd25fda6828204c76181379dc92871c5bf55`
(branch `claude/founder-alpha-phase3-staging`, PR #471, base `integration/founder-alpha-20261121` @ `0adb108fa5145ad6b320950fb029bfaf96f003b4`)

## PR-level provenance

| PR | Source agent | Head SHA | Status on this head | Evidence |
|----|----|----|----|----|
| #459 | Freebuff | `dd69cb30` | **Incorporated** — ancestor | `git merge-base --is-ancestor dd69cb30... HEAD` → true |
| #460 | Freebuff | `d2d2ccc1` | **Incorporated** — ancestor | same check → true; SFA-1 fix it introduced is present (see below) |
| #461 | Freebuff | `a7ed6d88` | **Incorporated** — ancestor | same check → true; `backend/scripts/ops/db_recovery.py`, `test_database_recovery.py` present |
| #463 | Codex | `dbd7680d` | **Incorporated, not as a direct merge** — imported as a source delta into #464/R15 ("applied without merging branches" per #464's own body) | Not a git ancestor (confirmed: `is-ancestor` → false, expected). Content verified present directly: `backend/app/providers/zai_provider.py`, `zai_eval_provider.py`, `docs/MODEL_ROUTING_HARDENING.md`, `gemma3:4b` entry in `catalog.py`, both of #463's test files all exist at this head. |
| #464 | Codex | `86671468` | **Incorporated** — ancestor (same commit as #467's head) | `is-ancestor` → true |
| #467 | coordinator (on Codex's R15 branch) | `86671468` | **Incorporated** — same head as #464, reconciled into staging by PR #470 | `is-ancestor` → true |
| #468 | Claude | `1204a00c` | **Incorporated** — ancestor | `is-ancestor` → true; `verify_active_lease()`, `local_only_mode` present |
| #469 | Claude | `f0a517a8` | **Incorporated, superseded mechanism kept** — ancestor, but PR #470 chose the base's `proposal_worktree.py` (git-worktree isolation) over #469's own filesystem-copy functions for the actual isolation mechanism, while porting #469's two real findings onto it | `is-ancestor` → true; `_create_proposal_workspace`/`_proposal_workspace_path`/`_cleanup_proposal_workspace` are **not present as functions** (grep confirms zero hits outside one explanatory comment) — `proposal_worktree.py` is the live mechanism. No duplicate/dead implementation left behind. |
| #470 | Claude | `3c759ad2` | **Incorporated** — direct ancestor, this is the R15+#468+#469 reconciliation | `is-ancestor` → true |
| #471 (this candidate) | Claude | `9938fd25` | **Current head** | — |

**Not integrated — confirmed BLOCKED, not assumed:**

| Claimed delivery | SHA | Status |
|---|---|---|
| Z.ai R20 | `6b725450410941f9482c74142a56f711d075f940` | Not a reachable git object anywhere in this repo (confirmed by the Phase 3 fork) |
| Z.ai v2 (earlier delivery) | `a1a0e0737edf14e6429f422b39c412e598447892` | Confirmed BLOCKED by PR #460 and re-confirmed by an earlier audit. **Correction, already flagged in PR #470**: commit `a1a0e073` ("feat(acceptance): Founder Acceptance Suite...") *is* present via R15's own ancestry — a different commit with a similar-looking short hash prefix than the one actually being searched for; the full-SHA `a1a0e0737edf...` object itself remains unreachable. |
| DeepSeek v4 | — | Zero footprint in this repo at any point |
| MiniMax "Workbench Source Fencing" | `204cdd4f` | No branch or reachable object anywhere |

## Real defects found and fixed during reconciliation (not pre-existing, not environmental)

All already landed in #470/#471's commit history (see those PRs' own descriptions for full detail); listed here for the manifest's completeness:

1. Two-head migration fork (`0080` chained onto `0078` instead of `0079`) — fixed, single 89-migration chain.
2. Two independently-written SFA-1 (`alembic_version` privilege) fixes silently fighting on every boot — kept the stricter.
3. `chat_with_fallback()`'s lease assertion wrongly denying pre-existing `job_id`-alone callers — fixed to match the function's documented contract.
4. Egress disclosure-ledger write ordered before the selection-policy check — reordered.
5. Stale `down_revision="0078"` test assertion, missing `CONFIG_MATRIX` entry, a test's manual `DELETE` rejected by the append-only audit trigger, a cleanup-timing bug in a new test, a test reading `alembic_version` through the restricted role — all fixed (PR #470).
6. `blueprint.py` left on the older `app.rag.prompt_fencing` module after the R15 merge, and `verify_architecture_contract.py`'s `single_prompt_fence` check silently FAILing since that same merge with nobody noticing — both fixed (Phase 3 / PR #471).
7. `test_full_cognitive_acceptance_r10.py`'s embedding fixture returning an identical vector for every document (~40% intermittent failure rate) — fixed the fixture, not the assertions (Phase 3 / PR #471).

## Open items carried forward (not resolved by this manifest)

- **Task #61** — founder-wide (non-goal-scoped) authorization primitive shape is an undecided architecture question. `lesson_conflicts.detect_conflict()` stays fail-closed until decided. Not a bug.
- **Cosmetic only**: `app/rag/untrusted_content.py`'s `_defang_payload()` double-escapes when a forged marker nests inside another. Containment property verified intact.
- **Independent third-party review** of the security properties in #468/#469/#470/#471 has not been obtained — no mechanism in this session to trigger it.

## Duplicate/parallel-implementation check (Phase A scope)

Checked the one known candidate for this (PI-1d's two independent isolation mechanisms, #469 vs. the base's `proposal_worktree.py`) — confirmed no duplicate code survives; #469's own workspace functions are absent from the tree, only `proposal_worktree.py` remains live. No other duplicate/parallel implementation of the same responsibility found during this review.
