"""Requirement traceability and evidence reconciliation (Phase 3 / Workstream F).

The chain is VISION -> REQUIREMENT -> DECISION -> ARCHITECTURE -> IMPLEMENTATION -> TEST ->
REVIEW -> INTEGRATION -> RUNTIME EVIDENCE. Every stage is either backed by a named evidence
reference or explicitly `missing`; a gap is never filled with a plausible guess.

Two rules are structural, not advisory:

1. `RequirementTrace.canonical_truth` is always None. A trace collects evidence; it never
   promotes one historical statement into current truth. Promotion requires an explicit
   founder decision recorded outside this module.
2. `reconcile_disagreement()` keeps both statements verbatim. It may name a *possible*
   supersession from timestamps or an explicit reference, but never selects a winner and
   never hides the uncertainty.

This module performs no I/O. Callers supply evidence they have actually read (a doc path, a
test node id, a PR number, a runtime probe id).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence


class Stage(str, Enum):
    vision = "vision"
    requirement = "requirement"
    decision = "decision"
    architecture = "architecture"
    implementation = "implementation"
    test = "test"
    review = "review"
    integration = "integration"
    runtime_evidence = "runtime_evidence"


STAGE_ORDER: tuple[Stage, ...] = tuple(Stage)


class ContextClass(str, Enum):
    historical_suggestion = "historical_suggestion"
    open_question = "open_question"
    proposed_design = "proposed_design"
    founder_approved_decision = "founder_approved_decision"
    superseded_decision = "superseded_decision"
    implemented_capability = "implemented_capability"
    verified_capability = "verified_capability"
    active_work = "active_work"
    rejected_approach = "rejected_approach"
    unknown = "unknown"


class LinkStatus(str, Enum):
    evidenced = "evidenced"
    missing = "missing"
    unknown = "unknown"


@dataclass(frozen=True)
class EvidenceRef:
    kind: str  # doc | code | test | pull_request | runtime | founder_decision
    ref: str
    verified: bool = False
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "verified": self.verified,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TraceLink:
    stage: Stage
    status: LinkStatus
    evidence: tuple[EvidenceRef, ...] = ()
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "stage": self.stage.value,
            "status": self.status.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "note": self.note,
        }


@dataclass(frozen=True)
class RequirementTrace:
    requirement_id: str
    title: str
    project_ref: str
    classification: ContextClass
    links: tuple[TraceLink, ...]
    uncertainty: str  # low | medium | high
    missing_stages: tuple[Stage, ...]
    superseded_by: str | None = None
    canonical_truth: str | None = None
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "title": self.title,
            "project_ref": self.project_ref,
            "classification": self.classification.value,
            "uncertainty": self.uncertainty,
            "missing_stages": [s.value for s in self.missing_stages],
            "superseded_by": self.superseded_by,
            "canonical_truth": self.canonical_truth,
            "links": [link.to_dict() for link in self.links],
            "execution_authorized": False,
        }


def _has(links: Mapping[Stage, Sequence[EvidenceRef]], stage: Stage) -> bool:
    return bool(links.get(stage))


def _verified(links: Mapping[Stage, Sequence[EvidenceRef]], stage: Stage) -> bool:
    return any(ref.verified for ref in links.get(stage, ()))


def _classify(
    links: Mapping[Stage, Sequence[EvidenceRef]],
    *,
    rejected: bool,
    superseded_by: str | None,
    open_question: bool,
) -> ContextClass:
    if rejected:
        return ContextClass.rejected_approach
    if superseded_by:
        return ContextClass.superseded_decision
    if _verified(links, Stage.runtime_evidence):
        return ContextClass.verified_capability
    if _has(links, Stage.implementation) and _has(links, Stage.test):
        return ContextClass.implemented_capability
    if _has(links, Stage.implementation):
        return ContextClass.active_work
    if _has(links, Stage.decision):
        approved = any(
            ref.kind == "founder_decision" and ref.verified
            for ref in links[Stage.decision]
        )
        return (
            ContextClass.founder_approved_decision
            if approved
            else ContextClass.proposed_design
        )
    if open_question:
        return ContextClass.open_question
    if _has(links, Stage.requirement) or _has(links, Stage.vision):
        return ContextClass.historical_suggestion
    return ContextClass.unknown


def _uncertainty(
    links: Mapping[Stage, Sequence[EvidenceRef]], classification: ContextClass
) -> str:
    if classification in (
        ContextClass.unknown,
        ContextClass.historical_suggestion,
        ContextClass.open_question,
    ):
        return "high"
    if _verified(links, Stage.runtime_evidence) and _has(links, Stage.review):
        return "low"
    if _has(links, Stage.implementation) and _has(links, Stage.test):
        return "medium"
    return "high"


def build_trace(
    requirement_id: str,
    title: str,
    project_ref: str,
    *,
    evidence: Mapping[Stage, Sequence[EvidenceRef]],
    rejected: bool = False,
    superseded_by: str | None = None,
    open_question: bool = False,
) -> RequirementTrace:
    """Build one trace, marking every stage without evidence as missing."""
    if not requirement_id.strip():
        raise ValueError("requirement_id must not be empty")

    links: list[TraceLink] = []
    missing: list[Stage] = []
    for stage in STAGE_ORDER:
        refs = tuple(evidence.get(stage, ()))
        if refs:
            links.append(
                TraceLink(stage=stage, status=LinkStatus.evidenced, evidence=refs)
            )
        else:
            missing.append(stage)
            links.append(
                TraceLink(
                    stage=stage,
                    status=LinkStatus.missing,
                    note="no evidence supplied for this stage",
                )
            )

    classification = _classify(
        evidence, rejected=rejected, superseded_by=superseded_by, open_question=open_question
    )
    return RequirementTrace(
        requirement_id=requirement_id,
        title=title,
        project_ref=project_ref,
        classification=classification,
        links=tuple(links),
        uncertainty=_uncertainty(evidence, classification),
        missing_stages=tuple(missing),
        superseded_by=superseded_by,
    )


@dataclass(frozen=True)
class SourceStatement:
    source_id: str
    statement: str
    observed_at: str | None = None
    project_ref: str = "unknown"
    status: str = "unknown"


@dataclass(frozen=True)
class Disagreement:
    a: SourceStatement
    b: SourceStatement
    project_scope: str  # same_project | different_projects | unknown
    uncertainty: str
    possible_supersession: str | None
    notes: tuple[str, ...]
    canonical_truth: str | None = None
    execution_authorized: bool = False

    def to_dict(self) -> dict:
        def _statement(s: SourceStatement) -> dict:
            return {
                "source_id": s.source_id,
                "statement": s.statement,
                "observed_at": s.observed_at,
                "project_ref": s.project_ref,
                "status": s.status,
            }

        return {
            "a": _statement(self.a),
            "b": _statement(self.b),
            "project_scope": self.project_scope,
            "uncertainty": self.uncertainty,
            "possible_supersession": self.possible_supersession,
            "notes": list(self.notes),
            "canonical_truth": None,
            "execution_authorized": False,
        }


def reconcile_disagreement(
    a: SourceStatement,
    b: SourceStatement,
    *,
    supersession_evidence: str | None = None,
) -> Disagreement:
    """Keep both statements; report scope, uncertainty and a *possible* supersession."""
    if a.project_ref == b.project_ref and a.project_ref != "unknown":
        scope = "same_project"
    elif a.project_ref != "unknown" and b.project_ref != "unknown":
        scope = "different_projects"
    else:
        scope = "unknown"

    notes = ["both originals are preserved verbatim", "no statement is promoted to truth"]
    possible: str | None = None

    if scope == "different_projects":
        uncertainty = "high"
        notes.append("statements describe different projects and are not comparable as truth")
    elif supersession_evidence:
        uncertainty = "medium"
        possible = supersession_evidence
        notes.append("an explicit reference may supersede one statement; review required")
    elif a.observed_at and b.observed_at and a.observed_at != b.observed_at:
        later = a if a.observed_at > b.observed_at else b
        possible = f"{later.source_id} (newer timestamp; candidate only)"
        uncertainty = "medium"
        notes.append("timestamps differ; recency is a candidate, not proof of correctness")
    else:
        uncertainty = "high"
        notes.append("no reliable date or supersession evidence distinguishes the statements")

    return Disagreement(
        a=a,
        b=b,
        project_scope=scope,
        uncertainty=uncertainty,
        possible_supersession=possible,
        notes=tuple(notes),
    )
