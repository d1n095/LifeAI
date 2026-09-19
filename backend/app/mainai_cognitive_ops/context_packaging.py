"""Context Packaging. See docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

Task-specific context packages -- each builder validates its own required fields are present
(raises rather than silently returning a package missing critical state)."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.mainai_cognitive_ops.types import CognitiveOpsError


@dataclass(frozen=True)
class CodeDebugPackage:
    objective: str
    failing_behavior: str
    exact_sha: str
    changed_files: tuple[str, ...]
    callers_callees: tuple[str, ...]
    interfaces: tuple[str, ...]
    failing_tests: tuple[str, ...]
    relevant_schema: tuple[str, ...]
    authority_constraints: tuple[str, ...]
    recent_attempts: tuple[str, ...] = field(default_factory=tuple)
    do_not_repeat: tuple[str, ...] = field(default_factory=tuple)


def build_code_debug_package(**kwargs) -> CodeDebugPackage:
    if not kwargs.get("objective") or not kwargs.get("failing_behavior") or not kwargs.get("exact_sha"):
        raise CognitiveOpsError("code debug package requires objective, failing_behavior, and exact_sha")
    return CodeDebugPackage(**kwargs)


@dataclass(frozen=True)
class ResearchPackage:
    question: str
    hypotheses: tuple[str, ...]
    source_lineage: tuple[str, ...]
    evidence: tuple[str, ...]
    contradictions: tuple[str, ...] = field(default_factory=tuple)
    actor_graph_ref: str | None = None
    timeline_ref: str | None = None
    uncertainty: str | None = None
    what_changes_our_mind: tuple[str, ...] = field(default_factory=tuple)


def build_research_package(**kwargs) -> ResearchPackage:
    if not kwargs.get("question"):
        raise CognitiveOpsError("research package requires a question")
    return ResearchPackage(**kwargs)


@dataclass(frozen=True)
class LegalPackage:
    documents: tuple[str, ...]
    clauses: tuple[str, ...]
    jurisdiction: str
    dates: tuple[str, ...] = field(default_factory=tuple)
    claims: tuple[str, ...] = field(default_factory=tuple)
    risks: tuple[str, ...] = field(default_factory=tuple)
    precedent_source_provenance: tuple[str, ...] = field(default_factory=tuple)


def build_legal_package(**kwargs) -> LegalPackage:
    if not kwargs.get("documents") or not kwargs.get("jurisdiction"):
        raise CognitiveOpsError("legal package requires documents and jurisdiction")
    return LegalPackage(**kwargs)


@dataclass(frozen=True)
class FounderDecisionPackage:
    decision_required: str
    why_now: str
    options: tuple[str, ...]
    cost: str | None = None
    risk: str | None = None
    reversibility: str | None = None
    recommendation: str | None = None
    disagreement: str | None = None
    unknowns: tuple[str, ...] = field(default_factory=tuple)


def build_founder_decision_package(**kwargs) -> FounderDecisionPackage:
    if not kwargs.get("decision_required") or not kwargs.get("why_now") or not kwargs.get("options"):
        raise CognitiveOpsError("founder decision package requires decision_required, why_now, and options")
    return FounderDecisionPackage(**kwargs)
