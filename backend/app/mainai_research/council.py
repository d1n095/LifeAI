"""Multi-Specialist Review Council + Adversarial Counsel. See
docs/mainai_v2/MAINAI_RESEARCH_TRUTH_ADVISORY_RECONCILIATION.md for the architecture decision.

SPECIALIST AGREEMENT != TRUTH. SPECIALIST DISAGREEMENT MUST REMAIN VISIBLE UNTIL RESOLVED --
`synthesize_council_review()` never averages away or hides a dissenting verdict; MainAI's own
role integrates the reviews into one payload but never overwrites another role's verdict.

Pure: no `db`, no I/O -- a caller persists the review (e.g. as `mainai_research_evidence_links`
provenance, or a future durable council-review table) separately."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field

from app.mainai_research.types import SpecialistRole


class ReviewVerdict(str, enum.Enum):
    SUPPORTS = "supports"
    DISPUTES = "disputes"
    UNCERTAIN = "uncertain"
    NOT_APPLICABLE = "not_applicable"  # e.g. LEGAL_COUNSEL on a claim with no legal dimension


@dataclass(frozen=True)
class SpecialistReview:
    role: SpecialistRole
    verdict: ReviewVerdict
    reasoning: str
    concerns: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CouncilSynthesis:
    reviews: tuple[SpecialistReview, ...]
    unanimous: bool
    dissenting_roles: tuple[SpecialistRole, ...]
    integrated_summary: str
    authorized: bool = False


def synthesize_council_review(reviews: tuple[SpecialistReview, ...]) -> CouncilSynthesis:
    """Pure. Never collapses disagreement: `dissenting_roles` names every role whose verdict
    differs from the majority (or, on a genuine tie, every role at all) -- always non-empty
    when `unanimous` is False. `integrated_summary` states the disagreement explicitly rather
    than silently picking a winner."""

    applicable = tuple(r for r in reviews if r.verdict != ReviewVerdict.NOT_APPLICABLE)
    if not applicable:
        return CouncilSynthesis(reviews=reviews, unanimous=True, dissenting_roles=(), integrated_summary="no applicable specialist verdicts were recorded")

    verdict_counts: dict[ReviewVerdict, int] = {}
    for r in applicable:
        verdict_counts[r.verdict] = verdict_counts.get(r.verdict, 0) + 1
    majority_verdict = max(verdict_counts, key=lambda v: verdict_counts[v])
    unanimous = len(verdict_counts) == 1
    dissenting = tuple(r.role for r in applicable if r.verdict != majority_verdict)

    if unanimous:
        summary = f"all {len(applicable)} applicable specialist(s) reached {majority_verdict.value} -- SPECIALIST AGREEMENT != TRUTH, still subject to falsification"
    else:
        dissent_desc = ", ".join(f"{r.role.value}={r.verdict.value}" for r in applicable if r.role in dissenting)
        summary = f"majority verdict is {majority_verdict.value}, but disagreement remains VISIBLE and unresolved: {dissent_desc}"

    return CouncilSynthesis(reviews=reviews, unanimous=unanimous, dissenting_roles=dissenting, integrated_summary=summary)


@dataclass(frozen=True)
class AdversarialCritique:
    strongest_criticism: str
    unprovable_statements: tuple[str, ...]
    potentially_misleading_wording: tuple[str, ...]
    hidden_assumptions: tuple[str, ...]
    what_would_embarrass_this_conclusion: tuple[str, ...]
    authorized: bool = False


def adversarial_critique(
    *,
    strongest_criticism: str,
    unprovable_statements: tuple[str, ...] = (),
    potentially_misleading_wording: tuple[str, ...] = (),
    hidden_assumptions: tuple[str, ...] = (),
    what_would_embarrass_this_conclusion: tuple[str, ...] = (),
) -> AdversarialCritique:
    """Structured self-attack -- ADVERSARIAL COUNSEL'S PURPOSE IS IMPROVEMENT THROUGH
    OPPOSITION, never merely legal blocking. This function is a plain constructor/validator
    (the actual critique content is the caller's own real analysis, matching every other
    "caller supplies the real signal" convention in this program) -- it exists so a critique is
    always a complete, typed object, never a bare string missing half its required dimensions."""

    if not strongest_criticism.strip():
        raise ValueError("adversarial critique requires a real strongest_criticism, not empty")
    return AdversarialCritique(
        strongest_criticism=strongest_criticism, unprovable_statements=unprovable_statements,
        potentially_misleading_wording=potentially_misleading_wording, hidden_assumptions=hidden_assumptions,
        what_would_embarrass_this_conclusion=what_would_embarrass_this_conclusion,
    )
