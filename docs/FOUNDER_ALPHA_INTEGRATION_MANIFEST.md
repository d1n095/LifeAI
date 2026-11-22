# Founder Alpha Integration Manifest

Authoritative map of what is actually in the Founder Alpha release candidate, where it
came from, and its current status. Built by walking real git ancestry and the GitHub API
at the exact head below — not memorized or inferred from prior reports. Re-verify against
live git/GitHub state before trusting this on a later head; it describes one point in time.

**Candidate head (Phase A, PR #471/#472):** `9938fd25fda6828204c76181379dc92871c5bf55`
(branch `claude/founder-alpha-phase3-staging`, PR #471, base `integration/founder-alpha-20261121` @ `0adb108fa5145ad6b320950fb029bfaf96f003b4`)

**Phase B adds no new commits to the application tree** — see the Phase B section below for why (both claimed outstanding deliveries were confirmed BLOCKED or already-merged, not new code to bring in). This document itself is updated on branch `claude/founder-alpha-phase-b-integration` from PR #472's head `c4d32c0b55d045443b155750f317165197ae51f1`.

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

## Phase B — outstanding agent deliveries (re-checked at `c4d32c0b55d045443b155750f317165197ae51f1`, PR #472)

**Z.ai Security Kernel R21–R24, claimed SHA `8ea29a6b341ead92e0e621b2e8865071f239a84c`: confirmed BLOCKED.**
Verified, not assumed:
- GitHub API `GET /repos/d1n095/LifeAI/commits/8ea29a6b...` → `422 No commit found for SHA`.
- `git ls-remote origin` across all 1258 refs (heads, tags, PRs) → no ref points at this SHA or anything near it; no `zai/*` branch beyond the already-known, already-superseded `zai/cognitive-core-r12-release-cc4d2804`.
- Filesystem-wide search (`find / -iname "*.bundle"`, plus targeted greps for the literal SHA and for "R21"–"R24" security-kernel references) found no bundle, patch, or tarball artifact anywhere in this sandbox — only pre-existing test fixtures for the already-integrated R12-era `zai_provider.py`.
- **Precise import requirement**: this delivery does not exist anywhere this session can reach. It needs to be delivered as a fetchable git ref (branch/tag) on `origin`, or as an actual bundle/patch file placed somewhere in this environment — not just referenced by SHA in a directive. Nothing was fabricated or assumed integrated.

**Freebuff's latest completed product functionality: nothing new.**
All 6 known branches re-checked by exact head SHA — `freebuff/local-ai-integration-20261118` (`89140ba8`), `freebuff/ci-readiness-20261118` (`94367545`), `freebuff/founder-alpha-integrated-20261118` (`dd69cb30`), `freebuff/founder-alpha-final-gate-20261118` (`d2d2ccc1`), `freebuff/founder-alpha-ops-recovery-20261118` (`a7ed6d88`), `transfer/freebuff-r15-20261119` (`16a6bd30`) — all 6 confirmed direct ancestors of this head via `git merge-base --is-ancestor`. A full remote branch listing found no Freebuff/transfer branch beyond these 6. Nothing to integrate.

**Alembic revision `0090`: does not exist anywhere.**
The live chain is `0001`→`0089`, single head, confirmed by `alembic upgrade head` on a fresh database. No `0090` file in `backend/alembic/versions/`, no remote branch or ref mentioning it, nothing granting a dedicated grant-issuer role under that revision. Reported plainly rather than invented — if `0090` is delivered later, it layers onto `0089` as the new head.

**Authorization lifecycle: re-verified intact, nothing new to integrate.**
Since none of the three claimed deliveries above had anything to actually bring in, this phase added no code. The full authorization-lifecycle test surface was re-run against a fresh database + Redis on this exact head to confirm nothing regressed: `test_chat_with_fallback_central_authorization_boundary.py` (7 tests — the central-egress negative-authorization suite from #468), `test_egress_policy.py` (9), `test_lease_clock_domain.py` (4), `test_worker_live_lease_expiry_takeover.py` (1), `test_supervisor_goal_lease.py` (9), `test_operator_lease_effect_time_race.py` (4), `test_rag_ingest_egress.py` (2) — **37 passed, 0 failed**.
