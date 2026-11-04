"""Recursive Falsification Engine. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

MAINAI SHOULD TRY TO DESTROY ITS OWN CONCLUSIONS. SURVIVING FALSIFICATION -> HIGHER CONFIDENCE.
FAILURE TO FIND COUNTEREVIDENCE != PROOF. DO NOT PRETEND STRONG EVIDENCE IS 50/50.

Pure: no `db`, no I/O -- a caller persists each round's outcome via
`research_ledger.update_hypothesis_confidence()`/`set_hypothesis_status()` separately (this
module only computes what the NEXT confidence/status should be from a round's real outcome,
never writes anything itself)."""

from __future__ import annotations

from dataclasses import dataclass

# Documented, hand-picked constants (no historical data exists yet to fit these against).
SURVIVE_CONFIDENCE_STEP = 0.08  # confidence gain per round survived, diminishing via the cap below
MAX_CONFIDENCE_FROM_SURVIVAL_ALONE = 0.9  # surviving falsification alone cannot reach certainty
CONTRADICTION_CONFIDENCE_FLOOR = 0.15  # a real, unresolved contradiction caps confidence low
MARGINAL_GAIN_FLOOR = 0.02  # below this per-round confidence delta, further rounds have low marginal value


@dataclass(frozen=True)
class FalsificationRoundResult:
    survived: bool
    counterevidence_found: bool
    new_confidence: float
    new_hypothesis_status: str  # HypothesisStatus.value
    marginal_gain: float
    reason: str
    diminishing_returns: bool = False


def run_falsification_round(
    *,
    current_confidence: float,
    round_number: int,
    survived: bool,
    counterevidence_found: bool,
    real_contradiction_present: bool = False,
) -> FalsificationRoundResult:
    """One round: an attempt was made to falsify the current best hypothesis (attack the
    strongest counter-hypothesis, inspect primary evidence, test causal order, etc. -- those
    real attempts are the caller's own responsibility, e.g. `causal_reasoning.py`/
    `words_vs_actions.py`; this function only integrates the round's own real OUTCOME into a
    confidence update).

    Order (first match wins):
      1. A REAL, unresolved contradiction survived this round -> confidence is capped at
         `CONTRADICTION_CONFIDENCE_FLOOR` regardless of prior support volume, status ->
         CONTRADICTED. MINORITY CLAIM != SUPPRESSED TRUTH's sibling here: a real contradiction
         is never outvoted by prior confidence.
      2. The hypothesis SURVIVED this round's genuine falsification attempt -> confidence rises
         by `SURVIVE_CONFIDENCE_STEP`, capped at `MAX_CONFIDENCE_FROM_SURVIVAL_ALONE` (surviving
         falsification rounds alone is never treated as certainty), status ->
         SURVIVED_FALSIFICATION.
      3. The hypothesis did NOT survive (a genuine counter-explanation fit better) -> confidence
         drops meaningfully, status stays ACTIVE (not yet CONTRADICTED unless a real
         contradiction was found -- see (1)).
      4. `counterevidence_found=False` alone (this round simply failed to find any
         counterevidence) is explicitly NEVER treated as proof -- if this is the only signal
         (no explicit survive/fail outcome), confidence does not move.
    """

    if real_contradiction_present:
        return FalsificationRoundResult(
            survived=False, counterevidence_found=True, new_confidence=min(current_confidence, CONTRADICTION_CONFIDENCE_FLOOR),
            new_hypothesis_status="contradicted",
            marginal_gain=abs(current_confidence - min(current_confidence, CONTRADICTION_CONFIDENCE_FLOOR)),
            reason="a real, unresolved contradiction was found this round -- capped low regardless of prior support volume",
        )

    if survived:
        new_confidence = min(MAX_CONFIDENCE_FROM_SURVIVAL_ALONE, current_confidence + SURVIVE_CONFIDENCE_STEP)
        gain = new_confidence - current_confidence
        return FalsificationRoundResult(
            survived=True, counterevidence_found=counterevidence_found, new_confidence=new_confidence,
            new_hypothesis_status="survived_falsification", marginal_gain=gain,
            reason=f"survived round {round_number}'s genuine falsification attempt -- confidence rises but is capped at {MAX_CONFIDENCE_FROM_SURVIVAL_ALONE} from survival alone",
            diminishing_returns=gain < MARGINAL_GAIN_FLOOR,
        )

    new_confidence = max(0.0, current_confidence - (2 * SURVIVE_CONFIDENCE_STEP))
    return FalsificationRoundResult(
        survived=False, counterevidence_found=counterevidence_found, new_confidence=new_confidence,
        new_hypothesis_status="active",
        marginal_gain=current_confidence - new_confidence,
        reason=f"did not survive round {round_number}'s falsification attempt -- a genuine counter-explanation fit at least as well",
    )


def failure_to_find_counterevidence_is_not_proof(*, counterevidence_found: bool) -> bool:
    """Explicit, named helper matching the founder's own invariant verbatim -- returns True
    (never treat as proof) whenever counterevidence was NOT found. Trivial by construction: the
    point is that no caller anywhere in this package computes the opposite."""

    return not counterevidence_found
