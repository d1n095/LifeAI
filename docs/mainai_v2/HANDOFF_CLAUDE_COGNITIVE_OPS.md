# Handoff — MainAI Cognitive Efficiency / Systemic Debugging / Situational Awareness / Information Lifecycle / Repo-Backup Intelligence Candidate

Durable checkpoint, written so a FRESH session (after context reset) has the correct CURRENT
canonical state without depending on any prior session's own conversational summary.
SESSION RESET != PROGRAM RESET.

**Written:** 2026-09-12.

## CURRENT OBJECTIVE

Give MainAI operational self-awareness: know what is already happening, avoid duplicate work
and duplicate founder messages, debug systemically rather than file-by-file, fragment/
reassemble/compress/deduplicate information without losing critical state, and know the real
difference between "committed" and "backed up." Built directly on top of the frozen Research,
Truth & Advisory Intelligence candidate, closing its three disclosed P1s along the way.
**Complete as of this handoff.** Does not modify the Cognitive Control Plane (`c4d336d`) or
rewrite the Research ledger's existing functions (only additive read-helpers were appended).

## EXACT SHA

- **Frozen candidate SHA:** `43ca3c79a491bc406b44ec90dc88766e754dcb4f`
- **Branch:** `claude/mainai-v2-sovereign`
- **Worktree:** `/Users/dennistorildson/Documents/LifeAI-worktrees/claude-mainai-v2`
- **Base this program branched from:** `b136858` (Research, Truth & Advisory Intelligence tip)
- **Working tree at freeze time:** clean (only the pre-existing, unrelated nested Codex
  worktree `codex-runtime-p0-handoff/` shows as untracked -- not part of this program, not
  touched by it).
- **Remote status at freeze time:** 6 commits ahead of `origin/claude/mainai-v2-sovereign`
  (`a942f88`) -- COMMITTED != BACKED UP, per this round's own `repo_backup_intelligence`
  finding. Not pushed; remote write requires explicit founder authorization.
- This is the FROZEN candidate, ready for independent review. Any follow-on program continues
  from this exact SHA on a new branch/worktree, never by further editing this one.
- See `docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md` for the full architecture
  decision and reuse audit.

## WHAT IS IMPLEMENTED

**One additive migration** (`0074_mainai_cognitive_ops`): `mainai_ops_founder_communications`
(owner-scoped RLS, append-only via the reused migration-0038 trigger) + a GIN index on the
existing `mainai_research_evidence_links.provenance` column.

**New package `backend/app/mainai_cognitive_ops/`** (16 modules) -- see the reconciliation doc
for the full module list. Summary of the 5 program threads:

- **Situational awareness / anti-duplication**: `situational_awareness.py`,
  `duplication_control.py`.
- **Founder communication discipline**: `founder_communication_ledger.py` (durable),
  `founder_anti_repetition.py` (pure decision layer).
- **Systemic debugging / compatibility / impact**: `systemic_debugging.py`,
  `compatibility_graph.py`, `change_impact.py`.
- **Information lifecycle**: `information_lifecycle.py`, `context_packaging.py`,
  `self_optimizing_context.py`, `compression.py`, `hot_warm_cold.py`, `indexing.py`.
- **Repo/backup intelligence**: `repo_backup_intelligence.py` (REAL, read-only git
  introspection via fixed-argv `subprocess`, never `shell=True`).

**Additive extension to `app.mainai_research.research_ledger`**: three new read-only functions
(`list_investigations`, `get_investigation`, `list_hypotheses_for_investigation`) -- no
existing function in that module was modified.

**Closes all three Research/Truth/Advisory P1s** from `HANDOFF_CLAUDE_RESEARCH_TRUTH_ADVISORY.md`:
1. Cross-investigation auto-reopen -- `research_reopen_trigger.py`.
2. Durable investigation graph decision + real validation -- `research_graph_provenance.py`
   (decision: keep JSONB, add GIN index + structural invariants, documented why).
3. Resource Intelligence -> Provider Economics bridge -- `provider_economics_bridge.py`.

## WHAT IS NOT IMPLEMENTED / KNOWN LIMITATIONS

- No live Dev Director / Continuous Supervision / Personal Recall adapter feeds
  `situational_awareness.py` yet -- it reasons over caller-supplied snapshots (typed `Protocol`
  seams disclosed in `adapters.py`, matching every prior round's honesty pattern).
- `compatibility_graph.py` is pure/in-memory; a caller supplies nodes/edges (no automatic
  derivation from real import graphs or DB schema introspection yet).
- `research_reopen_trigger.score_relevance()` is a keyword-overlap (Jaccard) heuristic, not
  semantic/NLP matching -- disclosed, not hidden, and proven to correctly reject weak-overlap
  cases by test.
- `repo_backup_intelligence.py` reads only already-fetched local remote-tracking refs -- it
  never performs a live `git fetch`; "remote SHA" reflects state as of the last fetch, which is
  the intended REMOTE EXISTS != REMOTE IS CURRENT distinction, not an oversight.

## REAL FINDING FROM THIS ROUND

Running this program's own `repo_backup_intelligence.snapshot_remote_sync_state()` against this
worktree found the branch 5 commits ahead of `origin/claude/mainai-v2-sovereign` (Cognitive
Control Plane + Research/Truth/Advisory candidates existed only locally) with an otherwise
clean tree. `assess_backup_risk()` correctly flagged this as material. Reported to the founder;
not auto-pushed (this program never grants itself remote-write authority).

## TEST EVIDENCE

**58/58 new `mainai_cognitive_ops` tests + 1 new `research_ledger` read-helper test, all
passing**, real local Postgres 16, migrations clean to head `0074_mainai_cognitive_ops`.
Covers (mapped to the founder's own §25 scenarios): busy-agent non-assignment, idle-agent
assignment, independent-examiner duplication allowed, accidental duplication flagged,
already-reported suppression, material-change reporting, same-recommendation-no-new-evidence
suppression, exact-duplicate linking with full provenance retention, near-but-distinct facts
NOT deduplicated, compression-quality-check catching missing critical fields, a REAL fresh-
session reconstruction against this project's own `HANDOFF_CLAUDE_RESEARCH_TRUTH_ADVISORY.md`
file, downstream-contract-break detection via blast radius, unexpected-impact detection,
self-optimization acceptance/rejection on quality-vs-efficiency tradeoffs, stale-index
detection, local-commit-missing-on-remote detection (against a real throwaway git repo, not
this project's own checkout), dirty-working-tree detection, no-upstream-branch flagging,
unrelated-evidence reopening a real investigation, weak-evidence NOT reopening one, a CLOSED
investigation never being reopened by the trigger, graph-provenance validation (including
real rejection of "controls"/"conspires_with" relationship kinds), and Resource-Intelligence-
derived provider-economics signals correctly reporting UNKNOWN (never zero/unlimited) on thin
history.

Two real bugs were found and fixed by these tests before they ever reached a caller: a
self-referential append-only-trigger violation in the communication ledger (same class of bug
as the earlier `knowledge_ingestion.py` fix), and a `.strip()` call that silently corrupted
`git status --porcelain` parsing.

Broader regression across `mainai_vision`/`mainai_research`/`mainai_cognitive_ops`/
`project_entities`/`resource_intelligence`/`work_candidates`/`capability_reality`: run this
session (see final report for exact count). `ruff check`: clean. `python -m compileall`: clean.

## KNOWN P0

None found.

## KNOWN P1

None found beyond the disclosed, intentional scope limits above.

## NEXT REVIEW REQUIRED

Independent review by a party that did not build it (BUILDER != FINAL EXAMINER).

## DO-NOT-REPEAT

- Do not rebuild `app.mainai_research.research_ledger`'s investigation/hypothesis store -- this
  round only ADDED three read-only functions to it.
- Do not have `repo_backup_intelligence.py` ever call a mutating git subcommand
  (push/commit/merge/reset/checkout/rebase) -- a structural-purity test asserts this; it is a
  hard architectural boundary, not a style preference.
- Do not silently push unpushed commits found by `assess_backup_risk()` -- report and let the
  founder decide (PUSH/BACKUP != MERGE; REMOTE WRITE != DEPLOY AUTHORITY).
