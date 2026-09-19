# MainAI Resource Intelligence — Round 2 Addendum (Integration-Readiness Pass)

Addendum to `MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md` (the binding Round 1 architecture
decision, not superseded — read that document first). This round takes the Round 1 candidate
from "ready for independent review" toward a stronger integration-ready candidate, per an
explicit founder directive to close specific named gaps before Codex's independent review
begins. It does **not** reopen or re-litigate any Round 1 decision; every addition below is new
surface area, composed with the existing package the same way Round 1 composed with
`app.agent_coordination`/`app.provider_spend`/`app.founder_memory`.

## 0. What Round 1's own handoff already named as gaps

`docs/mainai_v2/HANDOFF_CLAUDE_RESOURCE_INTELLIGENCE.md`'s "WHAT IS NOT IMPLEMENTED" section
named two gaps explicitly, both closed in this round:

- "No cross-provider quota-remaining tracking (only cost/token ceilings via the existing
  `provider_spend`/`workforce.cost` this package bridges to, never modifies)" → `quota.py`.
- The architecture decision's own §1.6 named `cost-to-finish estimate` as one of `decision.py`'s
  intended real signals, never actually wired into the Part 2 implementation → `cost_projection.py`
  + `decision.py`'s new branch 3.

## 1. New modules

1. **`quota.py`** — read-only bridge to `ProviderSpendAuthorization`'s own ceilings
   (`max_cost_usd`/`max_requests`/`max_prompt_tokens`/`max_completion_tokens` vs.
   `spent_*`+`reserved_*`). Deliberately does NOT reuse
   `app.provider_spend.service.get_current_provider_spend_authorization()` — that function
   takes a `SELECT ... FOR UPDATE` row lock and can itself flip an authorization to a terminal
   status, a real (if minor) mutation this package's own doctrine forbids. `quota.py` does its
   own plain, unlocked SELECT instead, matching `cost_bridge.py`'s own established precedent.
2. **`cost_projection.py`** — pure (no `db`, matching `decision.py`'s own shape) forward cost
   projections built on the real `app.providers.pricing.estimate_cost()` table:
   `estimated_cost_to_finish()` (per-accepted-commit cost × a caller-supplied remaining-commits
   estimate, refusing to project from a still-PROVISIONAL profile), and
   `reset_session_cost_estimate()`/`handoff_cost_estimate()`/`compact_cost_estimate()`. The
   reset/handoff pair models re-reading the durable SESSION CHECKPOINT (small, already
   summarized) — deliberately NOT the full raw `context_used_tokens` a compact would need to
   read — because discarding the bulky context in favor of the compact checkpoint is the entire
   point of a reset; the quality/risk cost of that trade is `founder_attention.py`'s concern,
   never blended into the dollar figure.
3. **`founder_attention.py`** — the one resource this whole package otherwise never priced in.
   A hand-picked, documented ordinal (`NONE/LOW/MEDIUM/HIGH`) per `ContextLifecycleAction`,
   exhaustively classified (every action, `KeyError` on an unclassified one — never a silent
   default that could under-report a real interruption). FOUNDER ATTENTION != DOLLAR COST is
   structural: this module never touches a dollar figure, `cost_projection.py` never touches an
   attention level.
4. **`supervision_compat.py`** — a field-shape translator FROM a
   `mainai_supervision_telemetry`-shaped plain `dict` (Codex's Continuous Supervision
   candidate, SHA `a7df7f90dba9f8bc993005b2cce1d4c8cb7dcec4`) INTO this package's own telemetry
   kwargs. Confirmed (again, this round) that candidate lives on a completely separate,
   unmerged branch lineage — this module never imports anything from it and is not imported by
   it; it exists purely so neither side has to invent a third, incompatible telemetry shape if
   both land on trunk. Translation only — never a second write path; the caller decides whether
   to persist.

## 2. Extensions to existing modules

- **`telemetry.py`**: `MAX_TELEMETRY_SAMPLE_AGE_SECONDS` (900s) — `context_utilization()`/
  `estimated_time_to_context_limit()` now refuse to report a confident value from a sample
  older than this (STALE TELEMETRY != CURRENT TELEMETRY), returning `missing_data=True`
  instead of a possibly-ancient reading. Simultaneous samples with different
  `context_used_tokens` values are now disclosed via `uncertainty` as CONFLICTING_TELEMETRY
  rather than silently resolved.
- **`efficiency_profile.py`**: `accepted_commit_rate`/`rework_rate` now populate the
  previously-always-`None` `trend` field — an older-half vs. recent-half split (by count, not a
  fixed calendar window), `None` (not a guessed "stable") below `MIN_HALF_SIZE_FOR_TREND` (3)
  assignments per half, matching ONE_RUN != LONG_TERM_PROFILE's own discipline applied to
  trend specifically.
- **`decision.py`** — five new, additive branches (existing branches unchanged in behavior for
  every previously-passing call shape except one, noted below):
  - Branch 0: `provider_quota_critical` → CHECKPOINT (if critical unsummarized state) or
    CHANGE_PROVIDER — finally giving that enum value a real caller.
  - Branch 1c (new): `critical_unsummarized_state` at ELEVATED (not yet HIGH) utilization →
    CHECKPOINT immediately, no hysteresis (CONTEXT-LOSS RISK escalates ahead of the acute bar
    because a checkpoint is cheap/low-attention).
  - Branch 1d (was 1c): the pre-existing proactive SPLIT_JOB now requires
    `consecutive_elevated_observations >= MIN_CONSECUTIVE_FOR_ELEVATED_ACTION` (2) —
    **behavior change**: a single elevated reading with `task_remaining_size="large"` no longer
    splits immediately; it did in Round 1. This is the round's own explicitly-requested
    DECISION STABILITY / hysteresis property, not a regression — the one existing test this
    touched was updated to pass `consecutive_elevated_observations=2` explicitly, and a sibling
    test proves the single-reading (default) case now correctly falls through to
    CONTINUE_CURRENT_SESSION instead. CRITICAL/HIGH branches are deliberately NOT hysteresis-gated
    — waiting for a second confirming reading on an already-nearly-full window is never the
    right trade.
  - Branch 3 (new): a quantified `cost_to_finish_current` vs. `cost_to_finish_alternative`
    comparison (> `COST_SWITCH_MARGIN_FRACTION` = 20% cheaper) recommends the caller-supplied
    `alternative_action` — CHEAPEST MODEL != CHEAPEST OUTCOME: this compares the real projected
    total (which `cost_per_accepted_commit` already amortizes rework cost into), never a raw
    per-token price.
- **`scheduler.py`**: `provider_quota_remaining()` wired in for real (via each assignment's own
  real `goal_id`), feeding `provider_quota_critical` into `propose_resource_action()`. A
  DEFER caused by `wip_at_limit` is relabeled MOVE_SUBTASK when a real, different, registered
  agent has spare WIP capacity right now (checked against the same
  `all_agents_runtime_snapshot()` already fetched once, not re-queried per assignment) —
  `decision.py` itself cannot see across agents and stays pure; this cross-agent visibility is
  this composition layer's own job. `cost_projection.py`'s reset/handoff/compact estimates are
  deliberately **NOT** wired into this layer: they require a real `provider`/`model` pair, and
  `CoordinationAgent.adapter_kind` is a CLI/API/internal CHANNEL type (`AgentAdapterKind`),
  never an LLM provider name — no real, non-guessed signal exists here today. This is a known,
  documented limitation, not a silent gap, matching Round 1's own precedent for
  `critical_unsummarized_state`/`task_remaining_size`.

## 3. What this round does NOT do

- Does not touch `dev_director`, the verified runtime (`1951ccef...`), the verified Continuous
  Supervision candidate (`a7df7f9...`), `#245`, or Personal Recall.
- Does not grant this layer authority — every new branch still returns `authorized=False`;
  `decision.py` remains zero-`db`, zero-I/O (re-verified: the existing AST-based structural
  purity test re-parses the current source and passed unchanged).
- Does not modify `app.agent_coordination`, `app.provider_spend`, `app.workforce.cost`,
  `app.capability_reality`, `app.execution_envelopes`, or `app.mainai_executive` — reads and
  composes only.
- Does not self-certify. Per the existing role-separation plan, Codex remains the intended
  independent reviewer of this whole candidate (Round 1 + this addendum) once its own
  Continuous Supervision candidate's review outcome is settled.

## 4. Test evidence

141 `resource_intelligence`-scoped tests passing (Round 1's 89 baseline unchanged in outcome
except the one hysteresis-affected split test discussed above + 1 sibling; ~52 new), run
against real local Postgres 16, migrations 0001→0071 clean, single head. Scoped regression
across `agent_coordination`/`provider_spend`/`execution_envelopes`/`continuity`/`judgment`/
`priority`/`capability_reality`: 94 passing, zero regressions. `ruff check`: clean.
`python -m compileall`: clean. `git diff --check`: clean.
