# MainAI Cognitive Efficiency + Systemic Debugging + Situational Awareness + Information Lifecycle + Repo/Backup Intelligence -- Architecture Decision

Program delivered as one coherent candidate, built on top of the frozen Research, Truth &
Advisory Intelligence candidate (`b136858`) without modifying its existing behavior (only
additive read-helper functions were appended to `research_ledger.py`; every prior function in
that module is untouched). Does not modify the Cognitive Control Plane (`c4d336d`) at all.

## §0 -- Audit before building

Before writing any new module, the existing repo was checked for overlap:

- `app.agent_coordination` already owns the real agent/work-assignment registry
  (`AgentWorkAssignment`, `WorkAssignmentRole`) -- this program's `situational_awareness.py`
  reasons over a caller-supplied `AgentState` snapshot rather than re-implementing a registry.
- `app.resource_intelligence` already owns cost/quota/efficiency truth
  (`cost_bridge`, `quota`, `efficiency_profile`) -- `provider_economics_bridge.py` composes
  these real functions rather than re-deriving the numbers.
- `app.mainai_research.research_ledger` already owns the investigation/hypothesis/evidence
  ledger -- `research_reopen_trigger.py` composes its existing (and three newly, additively
  added) read functions rather than a second store.
- No existing package in this repo covers founder-communication-delta suppression,
  compatibility-graph blast-radius analysis, information-tier/temperature modeling, or
  read-only git repo/backup introspection -- these are genuinely new and form the bulk of the
  new `app.mainai_cognitive_ops` package.

## Package layout

`backend/app/mainai_cognitive_ops/` (16 modules):

- `types.py` -- shared vocabulary (`AgentState`, `ProgramStatus`, `WorkItem`,
  `DuplicationVerdict`, `InformationTier`, `InformationTemperature`,
  `CommunicationDeltaVerdict`, `DebugStage`, `RemoteSyncState`).
- `situational_awareness.py` -- AGENT EXISTS != AVAILABLE, BUSY != ASSIGNABLE, SUBTASK COMPLETE
  != PROGRAM COMPLETE, KNOWN ACTIVE WORK != NEW TASK.
- `founder_communication_ledger.py` -- durable (migration 0074), append-only founder
  communication ledger.
- `founder_anti_repetition.py` -- pure decision layer over the ledger: ALREADY REPORTED !=
  REPORT AGAIN, etc.
- `duplication_control.py` -- cross-agent duplication scoring; independent-examiner
  duplication stays an explicitly allowed, distinct verdict from accidental waste.
- `systemic_debugging.py` -- the full SYMPTOM -> ... -> LEARN pipeline as a gated `DebugTrace`;
  "fixed" cannot be claimed until every stage is recorded.
- `compatibility_graph.py` + `change_impact.py` -- pure producer/consumer graph, blast-radius,
  and estimated-vs-actual impact verification (mirrors `mainai_research.investigation_graph`'s
  own technique).
- `information_lifecycle.py` -- fragment/defragment/deduplicate, provenance always retained.
- `context_packaging.py` -- CodeDebug/Research/Legal/FounderDecision packages, each validating
  its own required fields.
- `self_optimizing_context.py` -- strategy-change acceptance gated on quality first, efficiency
  second.
- `compression.py` -- `RecoveryCheckpoint` (every §19 field) + reconstructability check +
  a REAL parser for this project's own `docs/mainai_v2/HANDOFF_*.md` convention.
- `hot_warm_cold.py` -- temperature classification/transition; never touches a truth field.
- `indexing.py` -- retrieval-effectiveness measurement, stale-index detection.
- `repo_backup_intelligence.py` -- REAL, read-only git introspection (fixed argv,
  `shell=False`, local remote-tracking refs only).
- `research_reopen_trigger.py`, `research_graph_provenance.py`, `provider_economics_bridge.py`
  -- close the three P1s below.
- `adapters.py` -- typed `Protocol` seams for Dev Director / Continuous Supervision / Personal
  Recall, honestly disclosed as not-yet-real on this branch.

## Migration 0074

Adds `mainai_ops_founder_communications` (owner-scoped RLS, append-only via the reused
`intelligence_governance_deny_mutation()` trigger from migration 0038) and a GIN index on the
already-existing `mainai_research_evidence_links.provenance` column. No existing table altered.

## Closing the three Research/Truth/Advisory P1s

**P1 #1 -- Cross-investigation auto-reopen** (`research_reopen_trigger.py`): keyword-overlap
relevance scoring (`score_relevance`, a documented Jaccard heuristic, not NLP) against every
ACTIVE/SATURATED_FOR_NOW investigation (never CLOSED, matching `reopen_investigation()`'s own
terminal-state rule); only candidates above `RELEVANCE_REOPEN_THRESHOLD` (0.35) are reopened.
Proven both directions by test: a genuinely related discovery reopens; an unrelated one does
not.

**P1 #2 -- Durable investigation graph** (`research_graph_provenance.py`): decision made to
KEEP the graph inside the existing `evidence_links.provenance` JSONB rather than add a new
relational table -- the graph's nodes/edges are, by construction, always derived from already-
evidenced facts already stored there; a second table would either duplicate that storage or
hold un-evidenced claims. Migration 0074's GIN index makes `provenance @> {...}` containment
queries index-supported. `validate_graph_provenance_payload()` is real, structural validation
(required keys per actor/relationship/money-flow, and a hard rejection of "controls"/
"conspires_with" relationship kinds -- RELATIONSHIP != CONTROL).

**P1 #3 -- Resource Intelligence -> Provider Economics** (`provider_economics_bridge.py`):
`derive_provider_economics_signal()` calls the real `agent_efficiency_profile()` and
`provider_quota_remaining()` and maps their `MetricEnvelope`s onto a
`ProviderEconomicsSignal` -- `missing_data=True` maps to `None`, never `0.0` or "unlimited"
(UNKNOWN != ZERO, MISSING != FREE). No new cost/quota ledger.

## Real bugs found and fixed during this round

1. `founder_communication_ledger.py`'s first draft updated an OLD row's `superseded_by_id` on
   supersession -- the append-only trigger (migration 0038's
   `intelligence_governance_deny_mutation()`) rejects ANY update, and the design was wrong for
   the same reason `knowledge_ingestion.py`'s self-referential `supersedes_item_id` bug (fixed
   earlier in the Research round) was wrong: supersession belongs on the NEW row's own
   `supersedes_id`, pointing backward, never a mutation of history. Fixed the migration column
   and the ledger function before this was ever exercised against a real trigger.
2. `repo_backup_intelligence._run_git()` originally used `.strip()` on git's raw stdout, which
   silently ate the leading status-code space on the first line of `git status --porcelain`
   output -- corrupting `get_dirty_files()`'s own path parsing (`"a.txt"` became `".txt"`).
   Caught by a real subprocess-backed test against a throwaway git repo (not this project's own
   checkout). Fixed to `rstrip("\n")` only.

## Real, concrete finding surfaced by this program on its own worktree

Running `snapshot_remote_sync_state()` against this very worktree at build time found: branch
`claude/mainai-v2-sovereign` was 5 commits ahead of `origin/claude/mainai-v2-sovereign`
(including the entire Cognitive Control Plane and Research/Truth/Advisory candidates) with a
clean working tree otherwise -- i.e. `assess_backup_risk()` correctly flagged
`material=True`, "COMMITTED != BACKED UP", before this round's own commit was made. Reported to
the founder directly rather than silently auto-pushed (PUSH/BACKUP != MERGE; this program never
grants itself remote-write authority).

## What remains (honest P1s)

- `situational_awareness.py`/`duplication_control.py` reason over caller-supplied snapshots
  only -- there is no live Dev Director/agent-coordination adapter feeding them yet (see
  `adapters.py`'s disclosed `Protocol` seams).
- `compatibility_graph.py` is pure/in-memory, same documented choice as
  `mainai_research.investigation_graph` -- no auto-derivation from real import graphs/schema
  yet; a caller supplies nodes/edges.
- `score_relevance()` in `research_reopen_trigger.py` is a keyword-overlap heuristic, not
  semantic/NLP matching -- disclosed, not hidden.
