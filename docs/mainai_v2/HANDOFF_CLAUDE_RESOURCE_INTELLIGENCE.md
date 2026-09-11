# Handoff — MainAI Resource Intelligence Candidate

Durable checkpoint, written specifically so a FRESH session (after context reset) has the
correct CURRENT canonical state without depending on any prior session's own conversational
summary. `SESSION RESET != PROGRAM RESET`: everything below is meant to survive that reset.
If anything in this document conflicts with an older conversational recap, memory note, or
summary anywhere else — **this document, verified fresh against git at the timestamp below,
wins.** Verify the SHAs yourself before trusting even this file if it is more than a few days
old.

**Written:** 2026-09-11 (verify current date before trusting timestamps in this doc as fresh)

## CURRENT OBJECTIVE

Deliver a MainAI Resource Intelligence + Context Lifecycle + Cost/Quota + Agent Efficiency
foundation as an isolated, non-wired architecture layer, ready for independent review — same
BUILDER != FINAL EXAMINER discipline as every other MainAI V2 sub-program this session.
Objective is COMPLETE as of this handoff. No further Resource Intelligence implementation
work is queued.

## EXACT SHA

- **Candidate tip (on `claude/mainai-v2-sovereign`):** `8af2c8924ee672a8fc87efbbb01685f48c833ba4`
- **Merge commit (the actual code):** `97a621a`
- **Independent adversarial pass:** `ff729b2`
- **Part 2 (decision/efficiency/scheduler):** `02e57eb`
- **Part 1 (telemetry/cost_bridge/checkpoint):** `a6ed099`
- **Architecture decision doc:** `4d49e1f`
- **Base this program branched from:** `68a063e` (sovereign tip immediately after the Founder
  Reasoning/Judgment merge)
- **Worktree:** merged and cleaned up — the dedicated worktree/branch
  (`claude/mainai-v2-resource-intelligence`) was deleted after merge, per this program's own
  standard cleanup. Current work lives only on `claude/mainai-v2-sovereign`, in worktree
  `/Users/dennistorildson/Documents/LifeAI-worktrees/claude-mainai-v2`.
- **Worktree clean:** YES (one unrelated, pre-existing untracked artifact,
  `codex-runtime-p0-handoff/` — a nested-worktree mistake from an EARLIER, unrelated round
  [the Codex runtime P0 examiner handoff], not part of this program, not new uncommitted work).

## WHAT IS IMPLEMENTED

New package `backend/app/resource_intelligence/` (7 modules + 1 additive migration, no other
schema changes):
- `types.py` — `ContextLifecycleAction` enum, `ResourceActionRecommendation`, `MetricEnvelope`
  (structural METRIC != TRUTH contract), `unknown_metric()`.
- `telemetry.py` — durable per-attempt telemetry samples (migration `0071`,
  `agent_resource_telemetry_samples`, keyed to the real `AgentDispatchExecution.attempt_id`);
  `context_utilization()`, `estimated_time_to_context_limit()`, `idle_productive_blocked_time()`
  (the last one DERIVED at read time from real `AgentWorkAssignmentEvent`/
  `AgentDispatchExecution` history, never a stored counter).
- `cost_bridge.py` — read-only bridge from `AgentWorkAssignment` to real
  `ProviderSpendUsageEvent` rows (`cost_for_assignment()`, `tokens_for_assignment()`,
  `cost_per_accepted_commit()`); `populate_agent_outcome_cost_fields()` is the first real
  populator of `app.agent_coordination.service.build_agent_outcome_payload()`'s previously-dead
  `cost_tokens`/`cost_usd`/`duration_seconds` fields.
- `session_checkpoint.py` — `AgentSessionCheckpoint` (the founder's own named fields:
  current_objective/current_status/exact_sha/open_p0/open_p1/what_was_tried/what_failed/
  what_passed/next_action/do_not_repeat/authority_boundaries/unresolved_questions/
  critical_session_only_facts/...), persisted via the SAME mechanism
  `app.mainai_executive.continuity` already uses (`FounderMemoryNote` + `supersedes_note_id`
  chain) — no new table.
- `efficiency_profile.py` — genuinely new statistical layer: N-observation
  (`MIN_SAMPLE_SIZE_FOR_ESTABLISHED = 5`), recency-weighted (30-day exponential half-life)
  running profile per agent/task_type — `accepted_commit_rate`, `rework_rate`,
  `cost_per_accepted_commit`, `context_efficiency`. `ONE_RUN != LONG_TERM_PROFILE` enforced
  structurally via `is_provisional()`, not just documented.
- `decision.py` — `propose_resource_action()`: pure (no `db`, no I/O, matches
  `app.mainai_executive.judgment.decide_judgment()`'s exact shape), `authorized` always
  `False`. Returns one of `CONTINUE_CURRENT_SESSION/COMPACT/CHECKPOINT/RESET_SESSION/HANDOFF/
  SPLIT_JOB/MOVE_SUBTASK/CHANGE_MODEL/CHANGE_PROVIDER/DEFER/KEEP_CURRENT_AGENT`.
- `scheduler.py` — `next_best_resource_allocation()`: read-only composition of
  `app.agent_coordination.runtime_view.all_agents_runtime_snapshot()` (real WIP/availability)
  with per-assignment `propose_resource_action()` calls, ranked by weighted-sum + soft-cap
  scoring (matching `app.mainai_executive.priority.score_priority()`'s own discipline).

## WHAT IS NOT IMPLEMENTED

- No actual execution of any recommended action (COMPACT/RESET/HANDOFF/etc.) — advisory only,
  by design (`RESOURCE_OPTIMIZATION != AUTHORITY`).
- No live wiring into any real Claude Code/Cursor/Codex session — nothing calls this package
  from outside its own tests today (same "isolated, non-wired foundation" status as every
  other MainAI V2 sub-program pending review).
- `critical_unsummarized_state`/`task_remaining_size` signals have no real caller-supplied
  source anywhere in the codebase yet — `scheduler.py`'s own composition leaves them at honest
  defaults (`False`/`None`) rather than guessing from a proxy; documented as a known limitation
  in that module's own docstring, not a silent gap.
- No historical UI/founder-facing surface for any of this data (read-path functions only).
- No cross-provider quota-remaining tracking (only cost/token ceilings via the existing
  `provider_spend`/`workforce.cost` this package bridges to, never modifies).

## TEST EVIDENCE

**89/89 passing**, independently re-run by me directly against real local Postgres 16 (not
just trusted from the building forks' self-reports):
- 30 — Part 1 builder tests (`test_resource_intelligence_{telemetry,cost_bridge,
  session_checkpoint}.py`)
- 28 — Part 2 builder tests (`test_resource_intelligence_{efficiency_profile,decision,
  scheduler}.py`)
- 31 — my own independent adversarial pass (`test_resource_intelligence_longitudinal_
  scenarios.py`, 8 end-to-end scenarios; `test_resource_intelligence_self_attack.py`,
  full 7-module authority/metric-fabrication/divide-by-zero sweep)

Scoped regression across `agent_coordination`, `provider_spend`, `execution_envelopes`,
`mainai_executive` (continuity/judgment/priority), `capability_reality`: **all passing, zero
regressions**, run at two separate checkpoints (after Part 1, after the full merge).

`ruff check`: clean on every new/changed file. `python -c "import app.main"`: clean.
`alembic heads`: single head (`0071`), verified applying cleanly against real Postgres.

**Bugs found during build (both confirmed fixed, not just claimed):**
1. **State-machine transition ordering** — my OWN adversarial test helper attempted
   `running → reviewing` directly, which `app.agent_coordination.service.ALLOWED_TRANSITIONS`
   correctly rejects (the real path requires `running → ready_for_review → reviewing`). This
   was a bug in my test authoring, not in the library — confirms the real state machine's own
   guard is doing its job. Fixed by correcting the test helper.
2. **Timestamp ordering** — my OWN adversarial test captured a shared `now` timestamp BEFORE
   either of two telemetry-sample inserts, then assigned it to the SECOND sample — making the
   second (explicitly-timestamped) sample appear chronologically EARLIER than the first
   (auto-timestamped) one, which flipped `context_utilization()`'s "latest sample" selection
   and produced a wrong reading in my own test, not in the library. Fixed by timestamping both
   samples explicitly and in the correct order.

Neither bug reached the merged candidate — both were caught and fixed during my own
independent-pass authoring, before that pass's commit.

## KNOWN P0

None found.

## KNOWN P1

None found. (Two DESIGN notes, not defects: `agent_resource_telemetry_samples` deliberately
has no deny-mutation trigger, unlike sibling append-only tables — Part 1's builder verified a
trigger would break an existing account-erasure cascade; and `cost_bridge`'s goal/task join is
only as precise as `ProviderSpendUsageEvent.task_id`'s own bare-UUID, unconstrained-FK
precision, disclosed via `MetricEnvelope.uncertainty` whenever it applies, never silently.)

## INTEGRATION SEAMS

- Reads `app.agent_coordination` (`CoordinationAgent`, `AgentWorkAssignment`,
  `AgentWorkAssignmentEvent`, `AgentDispatchExecution`, `all_agents_runtime_snapshot()`) —
  never writes to any of its tables.
- Reads `app.provider_spend` (`ProviderSpendUsageEvent`, settled rows only) — never calls a
  mutating function (`reserve_*`/`settle_*`/`release_*`).
- Writes only its own new table (`agent_resource_telemetry_samples`) and `FounderMemoryNote`
  rows (via the real `record_founder_memory()`, same mechanism `continuity.py` already uses)
  for session checkpoints.
- `decision.py`/`scheduler.py` are the intended eventual integration point for a real scheduler
  loop (e.g. a future `dev_director`-style continuous loop, once that program is itself past
  independent review) — not wired to one today.

## NEXT REVIEW REQUIRED

Independent review of this candidate by a party that did not build it — per the role-separation
plan already in motion: **Claude built Resource Intelligence → Codex reviews Resource
Intelligence**, mirroring **Codex built Continuous Supervision → Claude reviews Continuous
Supervision**. Neither review has started as of this handoff.

## DO-NOT-REPEAT

- Do not re-run the Part 1/Part 2 build forks again — the candidate is complete and frozen at
  the SHA above. Any further Resource Intelligence work is REVIEW, not (re)implementation,
  until a review returns findings requiring fixes.
- Do not assume the Codex runtime is still broken or still being fixed — **it is not.** See
  "Confirmed untouched / current state of sibling programs" below; this exact point was flagged
  as stale-context risk in the directive that produced this handoff.
- Do not recreate `docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md` — it already
  exists and is the binding architecture decision; read it before proposing any change to this
  package's design.

## AUTHORITY BOUNDARIES

- **RESOURCE INTELLIGENCE != AUTHORITY** — confirmed structurally, not just documented:
  `decision.py` has zero `db` access and zero I/O (verified by `ast`-based test); every
  `ResourceActionRecommendation.authorized` is hardcoded `False`; no function in the package
  (swept across all 7 modules via `ast`) imports `create_work_assignment`,
  `authorize_execution_scope`, `transition_status`, any mutating `provider_spend` function, or
  `record_capability_observation`.
- **RESOURCE SCHEDULER OUTPUT != EXECUTION AUTHORITY** — `scheduler.py`'s
  `next_best_resource_allocation()` returns a ranked list of recommendations only; it never
  calls anything that dispatches, claims, or reassigns real work.
- **METRIC != AUTHORITY** — every metric-returning function returns a `MetricEnvelope`
  (value/unit/definition/source/method/missing_data/uncertainty/...), never a bare number a
  caller could mistake for ground truth without provenance; `missing_data=True` is a first-class,
  structurally-enforced outcome, never silently defaulted to zero.

### Confirmed untouched / current state of sibling programs (verified fresh via git, this session)

- **Codex "autonomous execution runtime"**: independently examined by Claude at frozen SHA
  `c1883262902622cc72871ab473808ca4969e1a87` → verdict `INDEPENDENT_ATTACK_FAIL` (2 P0s:
  certification bypass, protected-ref bypass) → handed back to Codex via PR #247 →
  **Codex fixed it → new SHA `1951ccef16f6e165092cf85e0bd545505b69f8b1`, reported
  `INDEPENDENT_VERIFICATION_PASS`.** This Resource Intelligence round's own diff was verified
  (`git diff --stat 1951ccef..HEAD -- substrate.py production_adapter.py`) to be on a
  completely separate, unmerged branch lineage from the runtime candidate — confirmed via
  `git merge-base --is-ancestor` in both directions — so the large diff that check produces is
  ordinary branch divergence, not evidence of anything being deleted or modified.
  **The runtime is not "still being fixed" — that phase is over. Do not carry forward any
  older recap that says otherwise.**
- **`dev_director`** (frozen SHA `ab1c0ce03a7a0f7b11f2f716304f9e1235a54d3e`): confirmed
  untouched (`git diff --stat` against current HEAD, scoped to `app/dev_director/`, empty).
  Still awaiting independent review — unchanged status.
- **`#245`** (branch `claude/final-blocker-closeout`, SHA
  `818dfb732da47901eb5ae06ffdd9c829fe00c4c5`): confirmed unchanged, re-verified this session.
- **Personal Recall** (SHA `024835547850035667c3d77383fd75699ceab178`): confirmed a separate,
  unmerged lane — not an ancestor of, and not touched by, this branch.
- **Codex "Continuous Supervision" candidate** (SHA
  `a7df7f90dba9f8bc993005b2cce1d4c8cb7dcec4`, commit "Add supervision resource telemetry
  seam"): confirmed a separate, unmerged lane (`git merge-base --is-ancestor` false in both
  directions); confirmed it does not touch `backend/app/resource_intelligence/` in either
  direction. Still under active build by Codex, not yet reviewed by anyone — unchanged status.
- **Founder Reasoning/Judgment** (merged into this same sovereign branch earlier, commit
  `4814d78`): unchanged since its own prior handoff — still awaiting independent review.

## PLANNED NEXT SEQUENCE (not yet started, for the record)

1. A fresh session (context reset, per this handoff's own reason for existing) independently
   reviews Codex's Continuous Supervision candidate (`a7df7f90...`).
2. Once Continuous Supervision passes independent review, Codex becomes the natural independent
   reviewer for THIS candidate (Resource Intelligence) — clean role separation, neither party
   reviewing its own work.
3. `dev_director` and Founder Reasoning/Judgment still separately await their own independent
   reviewers — not blocked on 1/2, just not yet scheduled.
