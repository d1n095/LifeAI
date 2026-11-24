"""Context reconciliation and intent detection (Workstream E).

One pure function answers: given what is already known, what may LifeAI do with a newly
discovered piece of context? The outcome vocabulary is REUSE / LINK / DOCUMENT / COMPARE /
PROPOSE / ASK / WAIT / REJECT.

Fail-closed rules, in priority order:
  ambiguity or missing evidence -> ASK (never guess, never start work)
  belongs to another project   -> LINK
  active assignment            -> WAIT (no duplicate assignments)
  capability already exists    -> REUSE
  prior decision rejected      -> REJECT
  prior decision postponed     -> WAIT
  superseded by a later record -> LINK
  founder decision required    -> ASK
  similar, not identical       -> COMPARE
  no action requested          -> DOCUMENT
  otherwise                    -> PROPOSE

No outcome grants execution authority: `Decision.execution_authorized` is always False.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Outcome(str, Enum):
    reuse = "reuse"
    link = "link"
    document = "document"
    compare = "compare"
    propose = "propose"
    ask = "ask"
    wait = "wait"
    reject = "reject"


@dataclass(frozen=True)
class ContextObservation:
    """Evidence supplied by the caller. Defaults are deliberately uninformative."""

    capability_exists: bool = False
    capability_evidence: tuple[str, ...] = ()
    active_assignment: str | None = None
    prior_decision: str | None = None  # approved | rejected | postponed | superseded
    superseded_by: str | None = None
    project_scope: str = "unknown"  # this_project | other_project | unknown
    evidence_sufficient: bool = True
    ambiguous: bool = False
    founder_decision_required: bool = False
    similarity_to_existing: float | None = None
    action_requested: bool = True
    missing_evidence: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Decision:
    outcome: Outcome
    reasons: tuple[str, ...]
    required_evidence: tuple[str, ...]
    next_step: str
    execution_authorized: bool = False


def reconcile(observation: ContextObservation) -> Decision:
    """Classifies the observation. Never authorizes execution."""
    if observation.ambiguous or not observation.evidence_sufficient:
        required = observation.missing_evidence or (
            "name the missing evidence before acting on this context",
        )
        return Decision(
            outcome=Outcome.ask,
            reasons=(
                "context is ambiguous or evidence is insufficient"
                if observation.ambiguous
                else "evidence is insufficient",
            ),
            required_evidence=required,
            next_step="ask the owner for the missing evidence; do not start work",
        )

    if observation.project_scope == "other_project":
        return Decision(
            outcome=Outcome.link,
            reasons=("the observation belongs to another project",),
            required_evidence=(),
            next_step="link it to that project's records; do not implement it here",
        )

    if observation.active_assignment:
        return Decision(
            outcome=Outcome.wait,
            reasons=(
                f"an active assignment already covers this: {observation.active_assignment}",
            ),
            required_evidence=(),
            next_step="wait for or join the active assignment; do not duplicate it",
        )

    if observation.capability_exists:
        evidence = observation.capability_evidence or ("capability reported as existing",)
        return Decision(
            outcome=Outcome.reuse,
            reasons=("the capability already exists",) + evidence,
            required_evidence=(),
            next_step="reuse the existing capability and cite its evidence",
        )

    if observation.prior_decision == "rejected":
        return Decision(
            outcome=Outcome.reject,
            reasons=("a previous decision rejected this direction",),
            required_evidence=(),
            next_step="do not pursue; cite the rejecting decision",
        )

    if observation.prior_decision == "postponed":
        return Decision(
            outcome=Outcome.wait,
            reasons=("a previous decision postponed this direction",),
            required_evidence=(),
            next_step="wait until the postponing decision is revisited",
        )

    if observation.superseded_by:
        return Decision(
            outcome=Outcome.link,
            reasons=(
                f"a later record supersedes this context: {observation.superseded_by}",
            ),
            required_evidence=(),
            next_step="link to the superseding record and follow it instead",
        )

    if observation.founder_decision_required:
        return Decision(
            outcome=Outcome.ask,
            reasons=("this needs a founder decision, not an agent choice",),
            required_evidence=("explicit founder approval for the proposed direction",),
            next_step="ask the founder; do not proceed on assumption",
        )

    if (
        observation.similarity_to_existing is not None
        and observation.similarity_to_existing >= 0.4
    ):
        return Decision(
            outcome=Outcome.compare,
            reasons=(
                f"similar to existing context (score {observation.similarity_to_existing:.2f}) "
                "but not proven identical",
            ),
            required_evidence=(),
            next_step="compare both originals side by side; similarity is not identity",
        )

    if not observation.action_requested:
        return Decision(
            outcome=Outcome.document,
            reasons=("knowledge-only observation; no action was requested",),
            required_evidence=(),
            next_step="record it against its project; do not create an assignment",
        )

    return Decision(
        outcome=Outcome.propose,
        reasons=("no existing capability, assignment, decision or duplicate covers this",),
        required_evidence=(),
        next_step="propose the smallest implementation; it still needs normal authorization",
    )
