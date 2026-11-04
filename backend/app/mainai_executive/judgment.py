"""Communicative/behavioral judgment -- WHAT TO SAY/DO NEXT, distinct from
`app.mainai_executive.attention.AttentionAction`/`decide_attention()` (WORK-continuity: whether
an in-flight goal pauses/resumes/supersedes). This is the reconciliation doc's real, confirmed
gap #1: "Communicative judgment (§7): SPEAK/CHALLENGE/PROPOSE_BIGGER/DEFER/INCUBATE/COMBINE/
KILL/ESCALATE/STAY_QUIET as a decision about WHAT TO SAY/DO NEXT."

`decide_judgment()` is a PURE function -- no `db: Session` parameter anywhere in its signature,
no import of sqlalchemy, no I/O of any kind. This is a deliberately STRONGER guarantee than
`kill_criteria.evaluate_kill_criteria()`'s own "advisory only, structurally cannot mutate"
pattern (that module takes a `db` and reads from it, proven never to write; this one cannot
touch a database at all, by construction, matching `attention.decide_attention()`'s own real,
already-proven pure-function shape exactly). Nothing here grants execution/spend/merge/deploy
authority -- `authorized` on the returned `JudgmentDecision` is always `False`; acting on a
returned action is entirely the caller's own, separate responsibility, routed through whatever
real, already-authorized function that action implies (e.g. a KILL recommendation still requires
a caller to separately invoke `app.work_candidates.service.dismiss_work_candidate()`/
`supersede_work_candidate()` or `app.life_intents.service.transition_intent()` -- this module
imports neither).

Deliberately NOT LLM-first, matching `attention.decide_attention()`'s own proven
deterministic-policy shape -- the reconciliation doc's own decision item 1 says exactly this:
"a real, deterministic (not LLM-first, matching attention.decide_attention()'s own proven
deterministic-policy shape) decide_judgment() function." An LLM-based judgment step for a
genuinely ambiguous case, if ever added, should live in a SEPARATE, narrow, optional,
fail-closed caller-side step (mirroring `app.mainai_execution.lesson_conflicts.detect_conflict()`
-- a real DB-touching async call, which this module explicitly stays clear of) -- never folded
into this function, since doing so would break the "decide_judgment structurally cannot mutate
and has zero I/O" guarantee this module exists to provide. This round deliberately does not add
that optional LLM step at all: the deterministic table below is already fully traceable/
inspectable on its own, and every branch below is documented with the exact incoming signal that
drove it -- this must never be an opaque decision.

AGREEMENT != GOOD REASONING, enforced structurally, not just documented: there is no branch
below whose fallback/default is SPEAK. The one and only branch with no other signal to justify
speaking returns STAY_QUIET (see `_bar` checks below) -- unconditional agreement with a weak,
unevidenced idea is never a code path here, founder-originated or not."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class JudgmentAction(str, Enum):
    SPEAK = "speak"
    CHALLENGE = "challenge"
    PROPOSE_BIGGER = "propose_bigger"
    DEFER = "defer"
    INCUBATE = "incubate"
    COMBINE = "combine"
    KILL = "kill"
    ESCALATE = "escalate"
    STAY_QUIET = "stay_quiet"


@dataclass(frozen=True)
class JudgmentDecision:
    action: JudgmentAction
    reason: str
    # Which real signals (by name, with their actual values) drove this decision -- inspectable
    # by construction, never opaque. Always includes at least the signal(s) the winning branch
    # actually checked; branches that were never reached along the way contribute nothing (a
    # caller wanting the FULL raw signal set already has it -- these are the ones that mattered).
    signals: dict[str, Any] = field(default_factory=dict)
    authorized: bool = False


# -- Thresholds -- all on the shared 0..1 confidence-adjacent scale this codebase already uses
# (Numeric(5,4) fields, PriorityFactors 0..1 floats). Documented, hand-picked starting points --
# same conservative-default convention as wip_awareness.default_wip_limit() and
# bounds.ExecutiveScanBounds -- not derived from measured data (none exists yet).
CONFIDENCE_BAR = 0.5
EVIDENCE_BAR = 0.5
CHALLENGE_STAKES_BAR = 0.4
PROPOSE_BIGGER_STAKES_BAR = 0.75
PROPOSE_BIGGER_CONFIDENCE_BAR = 0.75
HIGH_ATTENTION_COST_BAR = 0.75
LOW_URGENCY_BAR = 0.4
KILL_HIGH_CONFIDENCE_BAR = 0.8

_FRACTION_FIELDS = ("confidence", "evidence_strength", "urgency", "stakes", "founder_attention_cost")


def _validate_fraction(name: str, value: float) -> None:
    if not (0.0 <= value <= 1.0):
        raise ValueError(f"{name} must be within 0.0..1.0, got {value!r}")


def decide_judgment(
    *,
    confidence: float,
    evidence_strength: float,
    founder_originated: bool,
    urgency: float = 0.0,
    stakes: float = 0.0,
    founder_attention_cost: float = 0.5,
    wip_signal: tuple[bool, str] | None = None,
    kill_signal: Mapping[str, Any] | None = None,
    rejected_guard_signal: Mapping[str, Any] | None = None,
    has_new_evidence_since_rejection: bool = False,
    overlaps_existing_active_item: bool = False,
) -> JudgmentDecision:
    """Deterministic decision table, evaluated as an explicit, ordered if/elif waterfall (same
    shape as `attention.decide_attention()`) -- the FIRST matching rule wins, each one citing the
    real signal that drove it:

      1. `kill_signal` (real `kill_criteria.evaluate_kill_criteria()` output, when relevant) with
         `should_kill=True` overrides everything else -- continuing to weigh SPEAK/CHALLENGE/etc
         for something that should already be killed would itself be sunk-cost bias. High
         kill-signal confidence (>= KILL_HIGH_CONFIDENCE_BAR) -> KILL; lower confidence ->
         ESCALATE (a founder call, not an automatic kill).
      2. `rejected_guard_signal` (real `rejected_idea_guard.check_recently_rejected()` output)
         hit, with no new evidence since the earlier rejection -> STAY_QUIET. REJECTED IDEA
         NON-RESURFACING: a recently-rejected idea does not get re-raised just because it came up
         again, absent something genuinely new.
      3. `wip_signal` (real `wip_awareness.should_defer_new_work()` output) with
         `should_defer=True` -> INCUBATE (idea itself clears the confidence/evidence bar --
         preserve it, don't drop it) or DEFER (it doesn't). MORE PARALLELISM != MORE PROGRESS:
         a WIP-over-limit signal never leads to PROPOSE_BIGGER or SPEAK.
      4. `overlaps_existing_active_item=True` with confidence clearing the bar -> COMBINE, not a
         second, separate item.
      5. confidence/evidence below CONFIDENCE_BAR/EVIDENCE_BAR: founder-originated AND material
         stakes -> CHALLENGE (push back, don't just agree); otherwise -> STAY_QUIET. This is the
         structural "agreement is never the default" branch: there is no code path from "weak,
         unevidenced, low-stakes" to SPEAK.
      6. Otherwise the idea clears the bar to say something: MainAI-originated with high
         confidence AND high stakes -> PROPOSE_BIGGER (don't undersell a genuinely big
         opportunity as a small item); high `founder_attention_cost` with low `urgency` -> DEFER
         (clears the bar, but not worth interrupting right now); otherwise -> SPEAK.

    Every float parameter must be within 0.0..1.0 -- an out-of-range value raises immediately
    (fail closed on malformed input, never silently clamped, matching this codebase's own
    "honest unknown, never a fabricated score" convention elsewhere, e.g.
    `retrieval.rank_by_strength()`)."""
    for name in _FRACTION_FIELDS:
        _validate_fraction(name, locals()[name])

    # 1. kill_criteria -- overrides everything else.
    if kill_signal is not None and kill_signal.get("should_kill"):
        kc = float(kill_signal.get("confidence", 0.0))
        _validate_fraction("kill_signal.confidence", kc)
        signals = {"kill_signal": dict(kill_signal)}
        if kc >= KILL_HIGH_CONFIDENCE_BAR:
            return JudgmentDecision(
                JudgmentAction.KILL,
                f"kill_criteria should_kill=True at confidence {kc:.2f} (>= {KILL_HIGH_CONFIDENCE_BAR}) "
                "-- recommend killing via the real, existing dismiss_work_candidate()/"
                "supersede_work_candidate()/transition_intent(), never mutated here",
                signals,
            )
        return JudgmentDecision(
            JudgmentAction.ESCALATE,
            f"kill_criteria should_kill=True but confidence {kc:.2f} is below the kill bar "
            f"({KILL_HIGH_CONFIDENCE_BAR}) -- founder judgment required, not an automatic kill",
            signals,
        )

    # 2. rejected-idea guard -- REJECTED IDEA NON-RESURFACING.
    if (
        rejected_guard_signal is not None
        and rejected_guard_signal.get("is_likely_duplicate_of_rejected")
        and not has_new_evidence_since_rejection
    ):
        return JudgmentDecision(
            JudgmentAction.STAY_QUIET,
            "matches a recently-rejected idea/work-candidate "
            f"({rejected_guard_signal.get('reason', 'no reason given')}) and no new evidence has "
            "been presented since -- do not resurface without new evidence",
            {"rejected_guard_signal": dict(rejected_guard_signal), "has_new_evidence_since_rejection": False},
        )

    # 3. WIP over limit -- MORE PARALLELISM != MORE PROGRESS.
    if wip_signal is not None and wip_signal[0]:
        should_defer, wip_reason = wip_signal
        signals = {"wip_signal": {"should_defer": should_defer, "reason": wip_reason}}
        if confidence >= CONFIDENCE_BAR and evidence_strength >= EVIDENCE_BAR:
            return JudgmentDecision(
                JudgmentAction.INCUBATE,
                f"WIP over limit ({wip_reason}) but confidence ({confidence:.2f}) and evidence "
                f"({evidence_strength:.2f}) clear the bar -- preserve via idea_incubation instead "
                "of proposing active work right now",
                signals,
            )
        return JudgmentDecision(
            JudgmentAction.DEFER,
            f"WIP over limit ({wip_reason}) and confidence/evidence do not yet clear the bar "
            "-- defer",
            signals,
        )

    # 4. strategic-compression opportunity -- don't raise a duplicate/overlapping item.
    if overlaps_existing_active_item and confidence >= CONFIDENCE_BAR:
        return JudgmentDecision(
            JudgmentAction.COMBINE,
            f"overlaps an existing active item and confidence ({confidence:.2f}) clears the bar "
            "-- recommend combining via strategic_compression.compress_into_program() rather than "
            "raising a new, separate item",
            {"overlaps_existing_active_item": True, "confidence": confidence},
        )

    # 5. Weak confidence/evidence -- the structural "agreement is never the default" branch.
    if confidence < CONFIDENCE_BAR or evidence_strength < EVIDENCE_BAR:
        signals = {
            "confidence": confidence,
            "evidence_strength": evidence_strength,
            "founder_originated": founder_originated,
            "stakes": stakes,
        }
        if founder_originated and stakes >= CHALLENGE_STAKES_BAR:
            return JudgmentDecision(
                JudgmentAction.CHALLENGE,
                f"founder-originated with confidence ({confidence:.2f}) or evidence "
                f"({evidence_strength:.2f}) below the bar, but stakes ({stakes:.2f}) are material "
                f"(>= {CHALLENGE_STAKES_BAR}) -- challenge before agreeing, never rubber-stamp",
                signals,
            )
        return JudgmentDecision(
            JudgmentAction.STAY_QUIET,
            f"confidence ({confidence:.2f}) and/or evidence ({evidence_strength:.2f}) are below "
            f"the bar ({CONFIDENCE_BAR}/{EVIDENCE_BAR}) and nothing else here raises this above "
            "the threshold to say anything -- agreement/sycophancy is never the default",
            signals,
        )

    # 6. Clears the bar -- decide HOW to speak.
    signals = {
        "confidence": confidence,
        "evidence_strength": evidence_strength,
        "founder_originated": founder_originated,
        "stakes": stakes,
        "urgency": urgency,
        "founder_attention_cost": founder_attention_cost,
    }
    if (
        not founder_originated
        and stakes >= PROPOSE_BIGGER_STAKES_BAR
        and confidence >= PROPOSE_BIGGER_CONFIDENCE_BAR
    ):
        return JudgmentDecision(
            JudgmentAction.PROPOSE_BIGGER,
            f"MainAI-originated with high confidence ({confidence:.2f}) and high stakes "
            f"({stakes:.2f}) -- propose the bigger initiative, don't undersell it as a small item",
            signals,
        )
    if founder_attention_cost >= HIGH_ATTENTION_COST_BAR and urgency < LOW_URGENCY_BAR:
        return JudgmentDecision(
            JudgmentAction.DEFER,
            f"clears the bar to speak, but founder_attention_cost ({founder_attention_cost:.2f}) "
            f"is high and urgency ({urgency:.2f}) is low -- defer rather than interrupt now",
            signals,
        )
    return JudgmentDecision(
        JudgmentAction.SPEAK,
        f"confidence ({confidence:.2f}) and evidence ({evidence_strength:.2f}) clear the bar, and "
        "nothing biases toward a different action -- speak",
        signals,
    )


def judgment_as_dict(decision: JudgmentDecision) -> dict[str, Any]:
    return {
        "action": decision.action.value,
        "reason": decision.reason,
        "signals": decision.signals,
        "authorized": False,
    }
