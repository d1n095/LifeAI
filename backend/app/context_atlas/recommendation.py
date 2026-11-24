"""Read-only engineering decision support over the verified system map (Phase 4).

Given a request and the *observed* facts about existing components, return one of REUSE /
IMPROVE / INVESTIGATE / WAIT / ASK / REPLACE / REJECT with cited evidence and explicit
alternatives. It must be able to say "do not build anything", and it must not agree with a
founder assumption just because it was stated.

No model call, no agent dispatch, no write. `execution_authorized` is always False, and a
REPLACE recommendation is never itself an instruction to replace: it is a comparison for a
human decision.

Capability matching uses a small curated alias table (below), not free-text inference. That
is deliberate: an alias table can be reviewed and corrected, whereas keyword guessing would
manufacture confidence the evidence does not support. Unmatched requests become ASK rather
than a fabricated match.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class RecommendationOutcome(str, Enum):
    reuse = "reuse"
    improve = "improve"
    investigate = "investigate"
    wait = "wait"
    ask = "ask"
    replace = "replace"
    reject = "reject"


# Curated alias -> component prefix. Extend only with evidence that the component exists.
CAPABILITY_ALIASES: dict[str, tuple[str, ...]] = {
    "memory": ("app.memory", "app.inspectable_memory", "app.project_memory", "app.memory_work_linkage"),
    "retrieval": ("app.rag", "app.routers.search"),
    "provenance": ("app.models.memory_source_unit", "app.models.document_source_unit", "app.models.source_relationship"),
    "documents": ("app.routers.documents", "app.routers.library", "app.routers.knowledge", "app.rag"),
    "agents": ("app.routers.agents", "app.agent_coordination", "app.models.agent_coordination"),
    "execution": ("app.mainai_execution", "app.routers.mainai_execution", "app.routers.mainai_jobs", "app.jobs"),
    "identity": ("app.context_atlas", "app.rag.trust"),
    "projects": ("app.routers.projects", "app.project_status", "app.routers.project_entities"),
    "chat": ("app.routers.chat", "app.rag"),
}


@dataclass(frozen=True)
class CapabilityFact:
    component: str
    present: bool
    wired: bool
    test_referenced: bool
    path: str | None = None
    endpoints: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineeringRequest:
    request_text: str
    capability_aliases: tuple[str, ...] = ()
    project_ref: str = "unknown"
    active_assignment: str | None = None
    conflicting_sources: bool = False
    unresolved_evidence: tuple[str, ...] = ()
    prior_decision: str | None = None  # approved | rejected | postponed | superseded
    superseded_by: str | None = None
    other_project: bool = False
    implies_existing_failure: bool = False
    root_cause_verified: bool = False


@dataclass(frozen=True)
class Alternative:
    option: str
    description: str
    advantages: tuple[str, ...] = ()
    disadvantages: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "option": self.option,
            "description": self.description,
            "advantages": list(self.advantages),
            "disadvantages": list(self.disadvantages),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class Recommendation:
    outcome: RecommendationOutcome
    current_state: str
    evidence: tuple[str, ...]
    root_problem: str
    alternatives: tuple[Alternative, ...]
    next_action: str
    founder_decisions: tuple[str, ...]
    uncertainty: str
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "current_state": self.current_state,
            "evidence": list(self.evidence),
            "root_problem": self.root_problem,
            "alternatives": [a.to_dict() for a in self.alternatives],
            "next_action": self.next_action,
            "founder_decisions": list(self.founder_decisions),
            "uncertainty": self.uncertainty,
            "execution_authorized": False,
        }


def match_capabilities(
    aliases: tuple[str, ...], facts: Mapping[str, CapabilityFact]
) -> tuple[CapabilityFact, ...]:
    prefixes: list[str] = []
    for alias in aliases:
        prefixes.extend(CAPABILITY_ALIASES.get(alias.strip().lower(), ()))
    if not prefixes:
        return ()
    matched = [
        fact
        for name, fact in facts.items()
        if fact.present and any(name == p or name.startswith(p + ".") or name.startswith(p) for p in prefixes)
    ]
    return tuple(sorted(matched, key=lambda f: f.component))


def _evidence(facts: tuple[CapabilityFact, ...]) -> tuple[str, ...]:
    refs: list[str] = []
    for fact in facts:
        state = "wired" if fact.wired else "present but not wired"
        test = "test-referenced" if fact.test_referenced else "no test reference"
        refs.append(f"{fact.component}: {state}, {test}")
        refs.extend(f"endpoint {e}" for e in fact.endpoints)
    return tuple(refs)


def recommend(
    request: EngineeringRequest, facts: Mapping[str, CapabilityFact]
) -> Recommendation:
    matched = match_capabilities(request.capability_aliases, facts)
    evidence = _evidence(matched)
    wired = tuple(f for f in matched if f.wired)
    tested = tuple(f for f in matched if f.test_referenced)

    if request.active_assignment:
        return Recommendation(
            outcome=RecommendationOutcome.wait,
            current_state="an active assignment already covers this work",
            evidence=(f"active assignment: {request.active_assignment}",) + evidence,
            root_problem="duplicate work would consume the same scope",
            alternatives=(
                Alternative(
                    option="wait",
                    description="wait for or join the active assignment",
                    advantages=("no duplicated effort", "keeps one owner for the scope"),
                    disadvantages=("later start for this request",),
                ),
            ),
            next_action="link this request to the active assignment; do not dispatch another agent",
            founder_decisions=("confirm whether the new request adds scope or duplicates it",),
            uncertainty="low",
        )

    if request.conflicting_sources or request.unresolved_evidence:
        missing = request.unresolved_evidence or ("sources disagree",)
        return Recommendation(
            outcome=RecommendationOutcome.ask,
            current_state="the evidence does not support a single conclusion",
            evidence=tuple(missing) + evidence,
            root_problem="conflicting or incomplete evidence",
            alternatives=(
                Alternative(
                    option="investigate",
                    description="compare both originals and their dates before choosing",
                    advantages=("preserves both originals", "avoids promoting one side to truth"),
                    disadvantages=("delays implementation",),
                ),
            ),
            next_action="present both statements side by side and ask which one is current",
            founder_decisions=("which source is authoritative for this project, if either",),
            uncertainty="high",
        )

    if request.other_project:
        return Recommendation(
            outcome=RecommendationOutcome.ask,
            current_state="the request appears to belong to another project",
            evidence=("project scope mismatch reported by the caller",) + evidence,
            root_problem="the request may be historical context, not current work",
            alternatives=(
                Alternative(
                    option="link",
                    description="link the request to that project instead of implementing it here",
                    advantages=("keeps project boundaries intact",),
                    disadvantages=("no implementation in this project",),
                ),
            ),
            next_action="ask which project the request belongs to",
            founder_decisions=("confirm the target project",),
            uncertainty="high",
        )

    if request.prior_decision == "rejected":
        return Recommendation(
            outcome=RecommendationOutcome.reject,
            current_state="a previous decision rejected this direction",
            evidence=("prior decision: rejected",) + evidence,
            root_problem="the proposal revives a rejected approach",
            alternatives=(
                Alternative(
                    option="investigate",
                    description="re-open only if the original rejection reason no longer applies",
                    advantages=("respects recorded decisions",),
                    disadvantages=("requires an explicit founder reversal",),
                ),
            ),
            next_action="cite the rejecting decision; do not implement unless the founder reverses it",
            founder_decisions=("whether the rejection still applies",),
            uncertainty="low" if not request.unresolved_evidence else "medium",
        )

    if request.prior_decision == "postponed":
        return Recommendation(
            outcome=RecommendationOutcome.wait,
            current_state="a previous decision postponed this direction",
            evidence=("prior decision: postponed",) + evidence,
            root_problem="the direction is intentionally not active",
            alternatives=(),
            next_action="wait until the postponing decision is revisited",
            founder_decisions=("whether the postponement should be lifted",),
            uncertainty="low",
        )

    if request.superseded_by:
        return Recommendation(
            outcome=RecommendationOutcome.investigate,
            current_state="a later record may supersede this request",
            evidence=(f"superseded by: {request.superseded_by}",) + evidence,
            root_problem="the request may describe an outdated state",
            alternatives=(
                Alternative(
                    option="reuse",
                    description=f"follow the superseding record: {request.superseded_by}",
                    advantages=("uses the current decision",),
                    disadvantages=("may not satisfy the original intent",),
                ),
            ),
            next_action="read the superseding record before any implementation",
            founder_decisions=("confirm that the newer record is the current intent",),
            uncertainty="medium",
        )

    if matched:
        components = ", ".join(f.component for f in matched)
        if request.implies_existing_failure and not request.root_cause_verified:
            return Recommendation(
                outcome=RecommendationOutcome.investigate,
                current_state=f"an existing implementation covers this area: {components}",
                evidence=evidence,
                root_problem=(
                    "the reported failure is unverified; it may come from retrieval quality, "
                    "indexing, authorization, context limits or stale data rather than the engine itself"
                ),
                alternatives=(
                    Alternative(
                        option="improve",
                        description="fix the failing stage (retrieval/indexing/authorization/data freshness) first",
                        advantages=("smallest change", "keeps existing provenance and tests"),
                        disadvantages=("requires a real reproduction of the failure",),
                        evidence=evidence,
                    ),
                    Alternative(
                        option="reuse",
                        description="keep the existing engine and add the missing capability around it",
                        advantages=("no migration", "no parallel memory store"),
                        disadvantages=("may not address a genuine architectural limit",),
                        evidence=evidence,
                    ),
                    Alternative(
                        option="replace",
                        description="replace the component only if a measured root cause proves it cannot work",
                        advantages=("removes a real limitation",),
                        disadvantages=("high risk", "duplicates provenance/RLS work", "long migration"),
                    ),
                ),
                next_action="reproduce the failure and identify the failing stage before proposing a new engine",
                founder_decisions=("approve an investigation budget before any replacement is designed",),
                uncertainty="medium",
            )

        if not wired:
            return Recommendation(
                outcome=RecommendationOutcome.improve,
                current_state=f"the capability exists but is not wired into an active path: {components}",
                evidence=evidence,
                root_problem="an unwired implementation does not serve users",
                alternatives=(
                    Alternative(
                        option="improve",
                        description="wire the existing component and add the missing test coverage",
                        advantages=("reuses implemented code", "smaller diff than a rewrite"),
                        disadvantages=("the component may not fit the intended call site",),
                        evidence=evidence,
                    ),
                    Alternative(
                        option="reuse",
                        description="call the component through its existing interface",
                        advantages=("no new code path",),
                        disadvantages=("wiring may expose missing authorization checks",),
                    ),
                ),
                next_action="inspect why the component is unwired before writing anything new",
                founder_decisions=("whether the unwired component is still intended to ship",),
                uncertainty="medium",
            )

        return Recommendation(
            outcome=RecommendationOutcome.reuse,
            current_state=f"an existing, wired capability covers this area: {components}",
            evidence=evidence,
            root_problem="the request may already be satisfied by existing code",
            alternatives=(
                Alternative(
                    option="reuse",
                    description="use the existing component and cite its evidence",
                    advantages=("no new system", "existing tests and provenance stay valid"),
                    disadvantages=("may need a small extension",),
                    evidence=evidence,
                ),
                Alternative(
                    option="improve",
                    description="extend the existing component only where evidence shows a gap",
                    advantages=("keeps one implementation",),
                    disadvantages=("requires a concrete gap, not a preference",),
                ),
            ),
            next_action="verify the existing capability against the actual requirement before building",
            founder_decisions=(
                "confirm the requirement is not already met"
                if not tested
                else "none; existing evidence is sufficient to start with reuse",
            ),
            uncertainty="low" if tested else "medium",
        )

    if request.prior_decision == "approved":
        return Recommendation(
            outcome=RecommendationOutcome.investigate,
            current_state="an approved direction exists, but no implementation was found",
            evidence=("prior decision: approved",) + evidence,
            root_problem="the approval has no matching code evidence in this checkout",
            alternatives=(
                Alternative(
                    option="investigate",
                    description="locate the implementation or confirm it was never built",
                    advantages=("avoids duplicating an approved design",),
                    disadvantages=("delays the request",),
                ),
            ),
            next_action="search other branches/projects for the approved implementation before building",
            founder_decisions=("whether the approved direction is still current",),
            uncertainty="medium",
        )

    return Recommendation(
        outcome=RecommendationOutcome.ask,
        current_state="no existing component was matched with sufficient evidence",
        evidence=("no capability alias matched a present component",) + evidence,
        root_problem="the request has not been tied to a verified problem or an existing capability",
        alternatives=(
            Alternative(
                option="investigate",
                description="identify the problem and the smallest component that could address it",
                advantages=("avoids speculative new systems",),
                disadvantages=("takes time before any code is written",),
            ),
        ),
        next_action="ask what problem the request solves and which evidence shows it is unsolved",
        founder_decisions=("confirm the problem, the scope, and whether a new component is permitted",),
        uncertainty="high",
    )
