# MainAI — Long-Horizon Planning

Founder requirement, verbatim (2026-08-30): MainAI ("hon") should think much farther ahead than
the current task, maintaining multiple horizons — NOW (next action), NEAR (next 3-10 steps),
MID (next 10-50 dependencies/blockers), LONG (future consequences dozens/hundreds of actions
out). This is PLANNING, never authority — future plans may be generated freely inside
authorized planning boundaries, but future effects still require current, durable authority at
effect time. When reality changes, re-plan; never blindly follow an obsolete plan. Separately:
when the founder later thinks "we should probably include X too", MainAI should often already
have identified X — a bounded missing-item generation mechanism, never endless speculation.

Companion to `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` (§2's executive scan is the
NOW/NEAR-horizon instantiation of this document's more general horizon model) and
`docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md` (§4's memory-driven replan trigger and this
document's §3 reality-changed replan trigger are the same underlying mechanism, two trigger
sources).

---

## 1. What already exists — this is composition, not a new planner

| Requirement | Existing primitive | Verdict |
|---|---|---|
| "Planning != authority; future effects still need current authority at effect time" | `app.execution_envelopes` — `propose_execution_scope()` vs `authorize_execution_scope()`, the latter ALWAYS requiring the caller's own explicit assertion, never copying the proposal, re-verified at EFFECT time by `_require_live_execution_authority()` (re-reads the CURRENT active envelope on every single Operator effect, not just at planning time) | **This is the canonical doctrine the entire horizon model below inherits verbatim** — the single most important existing primitive for this document. |
| "Re-plan when reality changes" | `app.mainai_execution.replan` — `find_replan_trigger()` (a permanently-failed task in the active plan) → `trigger_replan()` (fresh `propose_plan_via_ai()` + `create_plan()`, which handles supersession/stale-task-cancellation), now bounded by tonight's `MAX_AUTO_REPLANS = 3` | **Reuse directly as the NOW/NEAR re-plan trigger.** Single-shot today (react to one failure, replace the whole plan) — MID/LONG horizons need staged extension, not replacement (§3). |
| Dependency graph the "next N blockers" horizon walks | `app.mainai_execution.graph.recompute_task_readiness()` + `MainAITaskDependency` edges | **Reuse the edge table; the multi-hop horizon traversal itself does not exist yet** — today's readiness check is a single flat pass, not a lookahead. |
| "Generate well-justified follow-up work, bounded, never endless" | `WorkCandidate` + `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` §2's bounded executive scan | **Reuse directly** — LONG-horizon missing-item generation is the SAME mechanism at a wider scan radius, not a different one. |
| "Verified-done" gate a plan can trust before considering a horizon closed | `app.mainai_execution.final_report.record_final_report()` — refuses to close a goal until every task is genuinely terminal | **Reuse directly**, unchanged. |
| Multi-stage/staged planning generally | `app.safe_planner` — no horizon concept found; `app.strategy_synthesis`/`app.strategy_evaluation` — compare/promote EXISTING strategies, don't project forward | **Genuinely new** — no existing analog for multi-horizon lookahead itself. |

**Net effect:** the authority doctrine and the re-plan trigger and the bounded-generation
primitive all already exist and are directly reusable. What's new is exactly one thing: a
staged, horizon-bucketed VIEW over planning data that doesn't exist as a query today, plus the
policy for how deep each horizon is allowed to look and how a plan gets invalidated when
reality outruns it.

---

## 2. The four horizons, precisely defined

```python
class PlanningHorizon(str, Enum):
    NOW  = "now"    # the single next action -- a MainAITask already `ready`, about to be
                     # dispatched, OR the next Driver step within an already-running task.
    NEAR = "near"    # the next 3-10 steps -- MainAITasks already in the CURRENT active
                     # MainAIPlan, not yet `completed`, walkable via a single
                     # recompute_task_readiness()-shaped pass (existing, unchanged).
    MID  = "mid"     # the next 10-50 dependencies/blockers -- NOT yet real MainAITasks in
                     # every case; a bounded MULTI-HOP traversal of MainAITaskDependency edges
                     # plus `WorkCandidate.dependencies` (candidates not yet authorized), and
                     # docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md §2's own relevance
                     # scan results not yet turned into tasks at all.
    LONG = "long"    # consequences dozens/hundreds of actions out -- NEVER concrete tasks.
                     # Represented ONLY as durable, low-confidence WorkCandidate rows
                     # (priority="LATER"/"OPTIONAL") or FounderMemoryNote (note_type="goal")
                     # entries -- narrative/directional, explicitly not a committed plan.
```

**Critical asymmetry, stated explicitly because it is the entire safety property of this
document:** NOW and NEAR are backed by REAL, already-`create_plan()`-persisted `MainAITask`
rows — durable, authority-relevant, subject to the full execution/verification/finalize chain.
MID is a mix of real dependency edges (durable) and not-yet-authorized `WorkCandidate` rows
(durable DATA, zero authority). LONG is ALWAYS planning-only data with zero authority
implication, by construction — there is no mechanism anywhere in this document, and there must
never be one added later, that lets a LONG-horizon item skip NEAR's own `authorize_execution_
scope()`/`grant_task_approval()` gates on its way to becoming a real effect.

---

## 3. Re-plan when reality changes — extending, not replacing, `replan.py`

### 3.1 Today's mechanism (unchanged, reused as the NOW/NEAR trigger)

`find_replan_trigger()` fires on exactly one condition: a permanently-`failed` task in the
active plan. This remains correct and sufficient for the NOW/NEAR horizons — a plan that's
already failing needs an immediate, full replan, not a staged one.

### 3.2 New trigger sources, same mechanism, MID/LONG-scoped

`trigger_replan()` itself (propose → create_plan, with supersession) does not need to change.
What's new is WHEN it fires, for MID/LONG-horizon reasons beyond task failure:

```python
def find_horizon_replan_trigger(db, *, goal: MainAIGoal) -> ReplanTrigger | None:
    """Superset of find_replan_trigger() -- checks the SAME failed-task condition first
    (unchanged priority), then additionally:

    1. A MID-horizon dependency edge now points at a task/candidate that no longer exists
       or was cancelled -- the plan's own lookahead is now stale (reality moved).
    2. A new, higher-confidence WorkCandidate has been authorized for the SAME goal
       (docs/MAINAI_INSPECTABLE_MEMORY_CONTRACT.md §4's memory-driven trigger) whose
       dependencies overlap the current active plan's own remaining tasks -- the plan should
       incorporate it rather than run two disconnected sub-plans for one goal.
    3. A LONG-horizon item was promoted into a real WorkCandidate.authorized_goal_id link to
       THIS goal (a founder decided a "someday" item is now relevant) -- same as #2, just a
       different origin.

    Each of these still produces exactly one create_plan() call, exactly the same supersession
    mechanics as today -- multiple trigger SOURCES, one trigger MECHANISM, no parallel replan
    pathway."""
```

`MAX_AUTO_REPLANS` (tonight's own fix) applies uniformly regardless of which trigger source
fired — a goal that keeps getting re-triggered for ANY reason, real or not, still cannot loop
forever. This is not weakened by adding new trigger sources; if anything it becomes more
important, since there are now more ways to trigger a replan.

### 3.3 "Do not blindly follow an obsolete 100-step plan"

There is no such thing as a "100-step plan" as a single durable object in this architecture,
by design — NEAR is bounded to the current `MainAIPlan`'s own tasks (typically single digits),
MID and LONG are explicitly NOT committed plans (§2). A plan can therefore never become
"obsolete" in the sense of a long precomputed sequence silently diverging from reality — each
NEAR-horizon `MainAIPlan` version is short-lived by construction, superseded wholesale by
`create_plan()` the moment §3.1 or §3.2 fires, never patched incrementally. "Re-plan instead of
blindly following a stale plan" is therefore not a new behavior to build; it is what
`create_plan()`'s existing full-supersession model already does on every trigger — the work in
this document is entirely about MID/LONG horizons never being mistaken for committed plans in
the first place.

---

## 4. "If I think of adding it, she should already have"

### 4.1 Mechanism — MID/LONG-horizon executive scan

Directly reuses `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` §2's orchestration
(`active_context` expansion + `autonomous_gap`-shaped bounding + `WorkCandidate` output), run
at TWO different radii:

```python
class HorizonScanBounds:
    NEAR = ExecutiveScanBounds(max_candidates_per_scan=10, max_scan_depth=2, max_elapsed_seconds=60)
    MID  = ExecutiveScanBounds(max_candidates_per_scan=25, max_scan_depth=4, max_elapsed_seconds=180)
    LONG = ExecutiveScanBounds(max_candidates_per_scan=10, max_scan_depth=6, max_elapsed_seconds=180)
    # LONG's own candidate cap stays LOW despite the widest scan depth -- deliberately: a wide,
    # shallow-confidence scan should surface FEWER, better-justified long-range items, not more
    # noise; MID is where the bulk of legitimate near-term follow-up work is expected to live.
```

`max_scan_depth` is `active_context`'s own existing hop-distance concept
(`ActiveContextMember.activation_path` already records the graph-expansion path) — no new
traversal primitive, just a wider bound and a different `priority` assigned to the output
(NEAR-scoped scan output defaults `priority="NOW"`/`"NEAR"`; MID-scoped defaults
`"NEAR"`/`"LATER"`; LONG-scoped defaults `"LATER"`/`"OPTIONAL"`, per
`docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` §2.3's vocabulary).

### 4.2 When this actually runs

Not on every tick — that would violate "do not create endless speculative tasks." Triggers:
- **NEAR scan**: every time `create_plan()`/`trigger_replan()` runs (i.e., every real
  plan/replan event already fires it once, free — no new tick needed).
  Ceiling: `list_unreviewed_work_candidates()` count for this goal.
- **MID scan**: on goal AUTHORIZATION (once, at `authorize_work_candidate()`/`create_goal()`
  time) and on every REPLAN (§3), never on ordinary task completion — MID-horizon items don't
  need re-deriving every time a single task finishes.
- **LONG scan**: explicitly founder-triggered only, for V1.1 — an unattended, unbounded-
  frequency LONG scan is exactly the "endless speculative tasks" failure mode the founder's
  own spec warns against; earn automatic LONG scanning later, once NEAR/MID scanning has a
  track record of not producing noise (see `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md`
  §8's own V1.1/V2 sequencing logic, applied here).

---

## 5. Failure/recovery behavior

- A horizon scan (any radius) that crashes mid-generation leaves zero partial `WorkCandidate`
  rows — same single-transaction-per-scan discipline as
  `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` §7.
- A MID/LONG-horizon `WorkCandidate` whose referenced dependency later gets cancelled/deleted
  is not silently dropped — `dependencies: JSONB` pointing at a now-gone id is a real, visible
  inconsistency; §3.2 trigger #1 is specifically what detects and resolves this on the next
  replan pass, not a background sweep.
- If `find_horizon_replan_trigger()` itself throws, the SAME per-goal isolation
  `_advance_mainai_execution_replan()` already provides (log + rollback + continue, never
  crash the wider tick) applies unchanged — no new error-handling pattern needed, this
  function is a superset caller into the same try/except shape.

---

## 6. V1 / V1.1 / V2 classification

**V1 minimum required:** none. Long-horizon planning is a capability-quality improvement over
an already-correct, already-shipped execution chain (NOW/NEAR already work today via
`replan.py`'s existing single-shot mechanism) — it does not block V1's execution-autonomy
scope.

**V1.1:**
- §3.2's additional replan trigger sources (still uses the existing, tested `trigger_replan()`
  mechanism — low implementation risk, meaningfully closes the "plan doesn't notice new
  relevant work" gap).
- §4.1's NEAR-radius scan (already effectively specified by
  `docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md` §2, this document just names its
  bounds).

**V2:**
- MID-radius scanning (§4.1/§4.2) — genuinely new multi-hop traversal logic over
  `MainAITaskDependency`, needs its own correctness/performance validation before running
  automatically on every goal authorization.
- LONG-horizon representation and founder-triggered scanning (§4.2) — lowest urgency, most
  speculative value, explicitly founder-gated even once built.

**Explicit non-goal, permanently:** LONG-horizon items becoming self-authorizing. No future
increment of this document may ever let a LONG-horizon `WorkCandidate` skip
`authorize_execution_scope()`/`grant_task_approval()` — restated from §2 because it is the one
invariant this entire document exists to protect, and the only part of it that must never be
revisited under "how do we ship this faster."
