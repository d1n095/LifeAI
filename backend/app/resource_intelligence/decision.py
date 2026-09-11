"""MainAI Resource Intelligence -- Part 2: `propose_resource_action()`, the context-lifecycle
decision engine. See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md §1.6 for the
architecture decision this module implements.

`propose_resource_action()` is a PURE function -- no `db: Session` parameter anywhere in its
signature, no import of sqlalchemy, no I/O of any kind. This matches
`app.mainai_executive.judgment.decide_judgment()`'s own proven pure-function shape exactly (see
that module's own docstring for why this is a deliberately STRONGER guarantee than
"advisory-only, structurally cannot mutate"). `ResourceActionRecommendation.authorized` is
always `False` -- this function recommends, it never itself compacts, resets, checkpoints, hands
off, or changes a real session's model/provider. RESOURCE_OPTIMIZATION != AUTHORITY, exactly the
way `app.execution_envelopes.propose_execution_scope()` keeps `propose_*` structurally incapable
of writing the authority table -- the difference here (per the reconciliation doc's own §0) is
that there is no `authorize_*` counterpart at all: the real COMPACT/RESET/HANDOFF action is
performed by a human or the harness itself, entirely outside this system.

Deliberately NOT LLM-first, matching `decide_judgment()`'s own proven deterministic-policy shape
-- the decision table below is evaluated as an explicit, ordered if/elif waterfall (the FIRST
matching rule wins), and every branch is documented with the exact real signal(s) that drove it.

INSUFFICIENT DATA != ALL CLEAR, enforced structurally: `context_utilization`/`time_to_limit`
missing (`missing_data=True`) never falls through to a confident-sounding
`CONTINUE_CURRENT_SESSION` -- the final fallback branch explicitly checks for this and routes to
`KEEP_CURRENT_AGENT` with a reason that says data is missing, not an "all clear" (see the last
branch below). Every returned `ResourceActionRecommendation.signals` dict cites the actual
`MetricEnvelope` values (via `_cite()`) that drove the branch -- inspectable by construction,
mirroring `JudgmentDecision.signals`'s own requirement."""

from __future__ import annotations

from typing import Any

from app.resource_intelligence.efficiency_profile import is_provisional
from app.resource_intelligence.types import ContextLifecycleAction, MetricEnvelope, ResourceActionRecommendation

# -- Thresholds -- documented, hand-picked starting points (no historical data exists yet to fit
# them against), same conservative-default convention this program already established
# (`app.mainai_executive.judgment.CONFIDENCE_BAR` et al., `wip_awareness.default_wip_limit()`).

# A context window >=80% full is the point past which a single large tool call or a long model
# turn can plausibly exhaust the remaining headroom before a caller gets another chance to act --
# high enough that a session at 60-79% is not yet flagged (still comfortable working room), low
# enough to leave real margin before the window is actually exhausted.
CONTEXT_UTILIZATION_HIGH_PCT = 80.0
# Below HIGH but still worth a proactive nudge (SPLIT_JOB) when a lot of new work remains.
CONTEXT_UTILIZATION_ELEVATED_PCT = 60.0
# Past this point a COMPACT is unlikely to buy enough room back to be worth the risk of doing it
# under time pressure -- RESET_SESSION (a clean restart from a checkpoint) is the safer choice.
CONTEXT_UTILIZATION_CRITICAL_PCT = 95.0
# Five minutes: enough wall-clock time for one more checkpoint write or a short tool call, not
# enough to safely keep working normally if the burn rate holds.
TIME_TO_LIMIT_LOW_SECONDS = 300.0
# An agent whose (established, not provisional) accepted-commit rate is below half is worse than
# a coin flip at reaching a real completed/verified outcome -- a real, defensible "this agent is
# struggling" bar, not a guess at a "good" number.
POOR_ACCEPTED_COMMIT_RATE_BAR = 0.5
# An agent whose (established) rework rate is >= 40% needed correction on 2 out of every 5
# assignments before landing -- a real signal the MODEL (not necessarily the whole agent
# identity) may be the issue, distinct from an outright low accepted-commit rate.
HIGH_REWORK_RATE_BAR = 0.4
# A cost-to-finish comparison below this margin is treated as noise, not a real advantage --
# switching agent/model/provider has real transition cost and risk this package does not fully
# price in (see cost_projection.py's own disclosed uncertainty), so only a comfortably larger
# projected saving is worth recommending a switch over. CHEAPEST MODEL != CHEAPEST OUTCOME: this
# bar exists precisely so a marginally-cheaper-on-paper alternative cannot outrank the agent
# already doing the work.
COST_SWITCH_MARGIN_FRACTION = 0.2
# An ELEVATED (not yet HIGH) proactive signal must repeat at least this many consecutive ticks
# before this module acts on it -- DECISION STABILITY: a single noisy reading at the elevated
# boundary must not flip the recommendation away from CONTINUE/KEEP_CURRENT_AGENT and back on
# the very next tick. Does NOT apply to the acute HIGH/CRITICAL branches (1a/1b) below -- those
# thresholds are already conservative, and CRITICAL specifically must never be dampened: waiting
# for a second confirming reading on a window that is ALREADY nearly full is the wrong trade.
MIN_CONSECUTIVE_FOR_ELEVATED_ACTION = 2


def _cite(name: str, envelope: MetricEnvelope | None) -> dict[str, Any]:
    """One compact, inspectable citation of a real `MetricEnvelope` -- every branch below cites
    the exact envelope(s) it read, mirroring `JudgmentDecision.signals`'s own requirement that a
    decision is never opaque."""

    if envelope is None:
        return {name: None}
    return {
        name: {
            "value": envelope.value,
            "unit": envelope.unit,
            "missing_data": envelope.missing_data,
            "sample_size": envelope.sample_size,
            "uncertainty": envelope.uncertainty,
        }
    }


def propose_resource_action(
    *,
    context_utilization: MetricEnvelope,
    time_to_limit: MetricEnvelope,
    idle_seconds: MetricEnvelope | None = None,
    productive_seconds: MetricEnvelope | None = None,
    blocked_seconds: MetricEnvelope | None = None,
    efficiency_profile: dict[str, MetricEnvelope] | None = None,
    wip_at_limit: bool = False,
    task_remaining_size: str | None = None,
    critical_unsummarized_state: bool = False,
    provider_quota_critical: bool = False,
    cost_to_finish_current: MetricEnvelope | None = None,
    cost_to_finish_alternative: MetricEnvelope | None = None,
    alternative_action: ContextLifecycleAction | None = None,
    consecutive_elevated_observations: int = 1,
) -> ResourceActionRecommendation:
    """Deterministic decision table, evaluated as an explicit, ordered if/elif waterfall (same
    shape as `decide_judgment()`) -- the FIRST matching rule wins:

      0. `provider_quota_critical=True` (see `quota.quota_critical()`) -- a harder stop than a
         full context window: no further tokens can be consumed against THIS provider at all,
         whereas a full window can still be compacted/reset on the same provider.
         `critical_unsummarized_state=True` still wins -> CHECKPOINT (NEVER LOSE CRITICAL STATE
         overrides even a quota stop); otherwise -> CHANGE_PROVIDER.
      1. Acute context-lifecycle branches (only evaluated when BOTH `context_utilization` and
         `time_to_limit` are actually known -- an unknown metric can never satisfy a >= / <=
         comparison honestly, so these branches simply do not fire on missing data; see branch 6
         for what happens instead):
         a. `context_utilization >= CONTEXT_UTILIZATION_HIGH_PCT` AND
            `time_to_limit <= TIME_TO_LIMIT_LOW_SECONDS` AND `critical_unsummarized_state=True`
            -> CHECKPOINT. Never lose critical state -- checkpointing comes before COMPACT/RESET
            whenever there is something critical and not yet durably summarized.
         b. Same high-utilization/low-time-to-limit combination WITHOUT critical unsummarized
            state -> COMPACT, or RESET_SESSION when `context_utilization >=
            CONTEXT_UTILIZATION_CRITICAL_PCT` (a window that full is unlikely to recover enough
            room from a compact alone to be worth the risk under time pressure).
         c. `context_utilization >= CONTEXT_UTILIZATION_ELEVATED_PCT` (but not yet HIGH) with
            `critical_unsummarized_state=True` -> CHECKPOINT, immediately, no hysteresis --
            CONTEXT-LOSS RISK escalates ahead of the acute HIGH bar precisely because a
            checkpoint is cheap and low-attention (see `founder_attention.py`); there is no
            reason to wait for the problem to become acute before taking the safe, low-cost
            action. Never fires when (c) already fired above.
         d. `context_utilization >= CONTEXT_UTILIZATION_ELEVATED_PCT` (but not yet HIGH) with
            `task_remaining_size == "large"` and NOT (c) -> SPLIT_JOB, proactively, before the
            problem becomes acute -- but ONLY when `consecutive_elevated_observations >=
            MIN_CONSECUTIVE_FOR_ELEVATED_ACTION` (DECISION STABILITY: unlike CHECKPOINT, a
            SPLIT_JOB is disruptive enough to plan around that a single noisy elevated reading
            should not trigger it; the caller is expected to pass how many CONSECUTIVE ticks
            have shown this same elevated condition). Below that count, this falls through to
            CONTINUE_CURRENT_SESSION with a reason that explicitly says so -- never silently
            dropped.
      2. `wip_at_limit=True` -> DEFER. Mirrors `decide_judgment()`'s own WIP-over-limit ->
         INCUBATE/DEFER precedent (MORE_CONTEXT != BETTER_CONTEXT_FOREVER's sibling invariant
         here: pushing more work at an agent that is already at its WIP limit is never the
         answer, regardless of how healthy its context metrics look).
      3. A quantified cost-to-finish comparison, when the caller supplies both
         `cost_to_finish_current` and `cost_to_finish_alternative` (each a real, non-provisional
         `MetricEnvelope`) plus the `alternative_action` it would mean recommending:
         `cost_to_finish_alternative` cheaper than `cost_to_finish_current` by more than
         `COST_SWITCH_MARGIN_FRACTION` -> `alternative_action` (e.g. HANDOFF or CHANGE_MODEL).
         CHEAPEST MODEL != CHEAPEST OUTCOME: a model with a lower per-token price but a high
         rework rate can easily have a HIGHER `cost_to_finish` once `efficiency_profile.
         cost_per_accepted_commit` amortizes its own wasted rework spend across accepted
         commits only -- this branch compares the REAL projected total, never raw per-token
         price. Symmetrically, a more expensive, more capable model with a lower rework rate
         can have a LOWER total `cost_to_finish` and is preferred here on that basis, never
         penalized for its higher sticker price. MOST CAPABLE MODEL != BEST MODEL FOR EVERY
         TASK either way -- this is a real cost comparison, not a capability ranking.
      4. A consistently poor, ESTABLISHED (never provisional -- see `efficiency_profile.
         is_provisional()`) efficiency profile for this agent/task_type combination biases away
         from continuing with this agent: `accepted_commit_rate < POOR_ACCEPTED_COMMIT_RATE_BAR`
         -> HANDOFF (the agent itself is underperforming); otherwise
         `rework_rate >= HIGH_REWORK_RATE_BAR` -> CHANGE_MODEL (a narrower "this model on this
         provider needs correction too often" signal, distinct from an outright low
         accepted-commit rate).
      5. Nothing above fired. If `context_utilization` or `time_to_limit` is itself
         `missing_data=True`, this is NEVER silently treated as "all clear" -> KEEP_CURRENT_AGENT
         with a reason that explicitly says data is missing. Only when both are genuinely known
         AND healthy does this fall through to the confident default, CONTINUE_CURRENT_SESSION.

    Every returned `signals` dict cites the real `MetricEnvelope`/boolean/string values that
    drove the branch, via `_cite()` -- never opaque."""

    signals: dict[str, Any] = {}
    signals.update(_cite("context_utilization", context_utilization))
    signals.update(_cite("time_to_limit", time_to_limit))
    signals["wip_at_limit"] = wip_at_limit
    signals["critical_unsummarized_state"] = critical_unsummarized_state
    signals["task_remaining_size"] = task_remaining_size
    signals["provider_quota_critical"] = provider_quota_critical
    signals["consecutive_elevated_observations"] = consecutive_elevated_observations

    context_known = not context_utilization.missing_data and context_utilization.value is not None
    ttl_known = not time_to_limit.missing_data and time_to_limit.value is not None

    # 0. Provider quota critical -- a harder stop than a full context window.
    if provider_quota_critical and critical_unsummarized_state:
        return ResourceActionRecommendation(
            ContextLifecycleAction.CHECKPOINT,
            "provider_quota_critical=True with critical_unsummarized_state=True -- never lose "
            "critical state, checkpoint before the provider quota forces a stop regardless",
            signals,
        )
    if provider_quota_critical:
        return ResourceActionRecommendation(
            ContextLifecycleAction.CHANGE_PROVIDER,
            "provider_quota_critical=True -- this provider's remaining quota is critically low; "
            "recommend changing provider before it is exhausted mid-task, regardless of how the "
            "context metrics look",
            signals,
        )

    # 1. Acute context-lifecycle branches.
    if context_known and ttl_known:
        util = float(context_utilization.value)
        ttl = float(time_to_limit.value)
        high_util = util >= CONTEXT_UTILIZATION_HIGH_PCT
        low_ttl = ttl <= TIME_TO_LIMIT_LOW_SECONDS
        elevated_util = util >= CONTEXT_UTILIZATION_ELEVATED_PCT and not high_util

        if high_util and low_ttl and critical_unsummarized_state:
            return ResourceActionRecommendation(
                ContextLifecycleAction.CHECKPOINT,
                f"context_utilization={util:.1f}% (>= {CONTEXT_UTILIZATION_HIGH_PCT}%) and "
                f"time_to_limit={ttl:.0f}s (<= {TIME_TO_LIMIT_LOW_SECONDS}s) with "
                "critical_unsummarized_state=True -- checkpoint before compacting/resetting so "
                "critical state is never lost",
                signals,
            )
        if high_util and low_ttl:
            critical = util >= CONTEXT_UTILIZATION_CRITICAL_PCT
            action = ContextLifecycleAction.RESET_SESSION if critical else ContextLifecycleAction.COMPACT
            return ResourceActionRecommendation(
                action,
                f"context_utilization={util:.1f}% (>= {CONTEXT_UTILIZATION_HIGH_PCT}%) and "
                f"time_to_limit={ttl:.0f}s (<= {TIME_TO_LIMIT_LOW_SECONDS}s), no critical "
                f"unsummarized state -- {'reset (utilization >= ' + str(CONTEXT_UTILIZATION_CRITICAL_PCT) + '%, a compact is unlikely to recover enough room)' if critical else 'compact to recover headroom'}",
                signals,
            )
        if elevated_util and critical_unsummarized_state:
            return ResourceActionRecommendation(
                ContextLifecycleAction.CHECKPOINT,
                f"context_utilization={util:.1f}% (>= {CONTEXT_UTILIZATION_ELEVATED_PCT}%, below "
                f"the acute {CONTEXT_UTILIZATION_HIGH_PCT}% bar) with critical_unsummarized_state="
                "True -- CONTEXT-LOSS RISK: checkpoint proactively now rather than waiting for "
                "the acute bar; a checkpoint is cheap and costs no founder attention, unlike "
                "waiting risks losing state that cannot be reconstructed",
                signals,
            )
        if elevated_util and task_remaining_size == "large":
            if consecutive_elevated_observations >= MIN_CONSECUTIVE_FOR_ELEVATED_ACTION:
                return ResourceActionRecommendation(
                    ContextLifecycleAction.SPLIT_JOB,
                    f"context_utilization={util:.1f}% (>= {CONTEXT_UTILIZATION_ELEVATED_PCT}%, below "
                    f"the acute {CONTEXT_UTILIZATION_HIGH_PCT}% bar) with task_remaining_size='large', "
                    f"sustained over {consecutive_elevated_observations} consecutive observations "
                    f"(>= {MIN_CONSECUTIVE_FOR_ELEVATED_ACTION}) -- split the remaining work "
                    "proactively before utilization becomes acute",
                    signals,
                )
            return ResourceActionRecommendation(
                ContextLifecycleAction.CONTINUE_CURRENT_SESSION,
                f"context_utilization={util:.1f}% with task_remaining_size='large' would warrant "
                f"SPLIT_JOB, but only {consecutive_elevated_observations} consecutive elevated "
                f"observation(s) (< {MIN_CONSECUTIVE_FOR_ELEVATED_ACTION} required) -- DECISION "
                "STABILITY: not yet acting on a possibly-noisy single reading",
                signals,
            )

    # 2. WIP over limit -- mirrors decide_judgment()'s own WIP-over-limit precedent.
    if wip_at_limit:
        return ResourceActionRecommendation(
            ContextLifecycleAction.DEFER,
            "wip_at_limit=True -- this agent is already at its WIP limit; deferring rather than "
            "adding more pressure regardless of how the context metrics look",
            signals,
        )

    # 3. Quantified cost-to-finish comparison (CHEAPEST MODEL != CHEAPEST OUTCOME).
    signals.update(_cite("cost_to_finish_current", cost_to_finish_current))
    signals.update(_cite("cost_to_finish_alternative", cost_to_finish_alternative))
    if (
        cost_to_finish_current is not None and not cost_to_finish_current.missing_data and cost_to_finish_current.value is not None
        and cost_to_finish_alternative is not None and not cost_to_finish_alternative.missing_data and cost_to_finish_alternative.value is not None
        and alternative_action is not None
        and cost_to_finish_current.value > 0
    ):
        savings_fraction = (cost_to_finish_current.value - cost_to_finish_alternative.value) / cost_to_finish_current.value
        if savings_fraction > COST_SWITCH_MARGIN_FRACTION:
            return ResourceActionRecommendation(
                alternative_action,
                f"cost_to_finish_alternative=${cost_to_finish_alternative.value:.4f} is "
                f"{savings_fraction:.0%} cheaper than cost_to_finish_current=${cost_to_finish_current.value:.4f} "
                f"(> {COST_SWITCH_MARGIN_FRACTION:.0%} margin) -- CHEAPEST MODEL != CHEAPEST OUTCOME: this "
                "compares the real projected total, not a per-token price; recommend the cheaper path",
                signals,
            )

    # 4. Established (never provisional) poor efficiency profile.
    if efficiency_profile:
        accepted = efficiency_profile.get("accepted_commit_rate")
        rework = efficiency_profile.get("rework_rate")
        signals.update(_cite("accepted_commit_rate", accepted))
        signals.update(_cite("rework_rate", rework))
        if accepted is not None and not accepted.missing_data and accepted.value is not None and not is_provisional(accepted.sample_size):
            if accepted.value < POOR_ACCEPTED_COMMIT_RATE_BAR:
                return ResourceActionRecommendation(
                    ContextLifecycleAction.HANDOFF,
                    f"established accepted_commit_rate={accepted.value:.2f} (< {POOR_ACCEPTED_COMMIT_RATE_BAR}, "
                    f"sample_size={accepted.sample_size}) -- this agent is underperforming on this "
                    "task_type; recommend handing off rather than continuing to assign it here",
                    signals,
                )
            if rework is not None and not rework.missing_data and rework.value is not None and not is_provisional(rework.sample_size) and rework.value >= HIGH_REWORK_RATE_BAR:
                return ResourceActionRecommendation(
                    ContextLifecycleAction.CHANGE_MODEL,
                    f"established rework_rate={rework.value:.2f} (>= {HIGH_REWORK_RATE_BAR}, "
                    f"sample_size={rework.sample_size}) despite an acceptable accepted_commit_rate="
                    f"{accepted.value:.2f} -- this model on this provider needs correction too "
                    "often; recommend a model change before a full handoff",
                    signals,
                )

    # 5. Nothing above fired -- honest default. Missing data is NEVER a confident all-clear.
    if not context_known or not ttl_known:
        return ResourceActionRecommendation(
            ContextLifecycleAction.KEEP_CURRENT_AGENT,
            "context_utilization and/or time_to_limit are missing_data=True -- there is not "
            "enough telemetry to assess this session's context lifecycle; keeping the current "
            "agent for now, but this is NOT a confident all-clear, it is an explicit "
            "acknowledgment that the key inputs are unknown",
            signals,
        )

    return ResourceActionRecommendation(
        ContextLifecycleAction.CONTINUE_CURRENT_SESSION,
        f"context_utilization={context_utilization.value:.1f}% and time_to_limit="
        f"{time_to_limit.value:.0f}s are both known and neither is concerning, wip_at_limit=False, "
        "and no efficiency-profile signal biases toward a different action -- continue",
        signals,
    )
