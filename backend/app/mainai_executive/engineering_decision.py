"""Engineering decision comparison over the six real options.

Reuse, repair, extend, replace, defer, or make no change — compared on the criteria that
actually decide whether a change is safe. Scores are caller-supplied and may be ``None``
(unknown); this module never imputes a missing score, never averages an unknown into a
number, and never treats its own ranking as execution authorization.

Deterministic weighted sum only. No model call, no confidence score invented here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

OPTION_KINDS = (
    "reuse_existing",
    "repair_existing",
    "extend_existing",
    "replace_component",
    "defer",
    "no_change",
)

CRITERIA = (
    "security",
    "maintainability",
    "dependencies",
    "resource_consumption",
    "operational_cost",
    "reliability",
    "testing",
    "rollback",
    "architectural_compatibility",
)

# Equal weights by default: a caller may emphasize security or rollback, but no criterion
# is silently worth more than another.
DEFAULT_WEIGHTS = {criterion: 1.0 for criterion in CRITERIA}

# Criteria whose score is meaningless for a "do nothing" option in the same way as for a
# code change; the option is still scored where the caller supplied a score.
_MIN_SCORE = 0
_MAX_SCORE = 5


@dataclass(frozen=True)
class SolutionOption:
    kind: str
    description: str
    criterion_scores: dict[str, int | None] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in OPTION_KINDS:
            raise ValueError(f"unknown option kind: {self.kind}")
        unknown = set(self.criterion_scores) - set(CRITERIA)
        if unknown:
            raise ValueError(f"unknown criteria: {sorted(unknown)}")
        for criterion, score in self.criterion_scores.items():
            if score is None:
                continue
            if not isinstance(score, int) or not _MIN_SCORE <= score <= _MAX_SCORE:
                raise ValueError(f"{criterion} score must be an int 0..5 or None")


@dataclass(frozen=True)
class OptionResult:
    kind: str
    description: str
    weighted_score: float | None
    scored_criteria: tuple[str, ...]
    unknown_criteria: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    risks: tuple[str, ...]
    dependencies: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "description": self.description,
            "weighted_score": self.weighted_score,
            "scored_criteria": list(self.scored_criteria),
            "unknown_criteria": list(self.unknown_criteria),
            "evidence_refs": list(self.evidence_refs),
            "risks": list(self.risks),
            "dependencies": list(self.dependencies),
        }


def _score(option: SolutionOption, weights: dict[str, float]) -> OptionResult:
    scored: list[str] = []
    unknown: list[str] = []
    total = 0.0
    weight_total = 0.0
    for criterion in CRITERIA:
        value = option.criterion_scores.get(criterion)
        if value is None:
            unknown.append(criterion)
            continue
        weight = weights.get(criterion, 1.0)
        total += value * weight
        weight_total += weight
        scored.append(criterion)
    return OptionResult(
        kind=option.kind,
        description=option.description,
        # None when nothing was scored — never 0.0, which would look like a measured worst.
        weighted_score=(round(total / weight_total, 4) if weight_total else None),
        scored_criteria=tuple(scored),
        unknown_criteria=tuple(unknown),
        evidence_refs=tuple(option.evidence_refs),
        risks=tuple(option.risks),
        dependencies=tuple(option.dependencies),
    )


def compare_solutions(
    options: list[SolutionOption] | tuple[SolutionOption, ...],
    *,
    weights: dict[str, float] | None = None,
    require_all_criteria: bool = False,
) -> dict:
    """Compare supplied options; never auto-select a winner on insufficient evidence.

    - Options with no scored criterion are not ranked and are reported as unscored.
    - ``require_all_criteria=True`` refuses to recommend anything that has an unknown
      criterion — the strict mode for security-sensitive decisions.
    - Ties at the top return ``unresolved_tie`` rather than picking arbitrarily.
    """
    if not options:
        raise ValueError("at least one option is required")
    kinds = [option.kind for option in options]
    if len(set(kinds)) != len(kinds):
        raise ValueError("option kinds must be unique")
    merged_weights = dict(DEFAULT_WEIGHTS)
    if weights:
        unknown_weights = set(weights) - set(CRITERIA)
        if unknown_weights:
            raise ValueError(f"unknown criteria weights: {sorted(unknown_weights)}")
        merged_weights.update(weights)

    results = [_score(option, merged_weights) for option in options]
    ranked = sorted(
        (result for result in results if result.weighted_score is not None),
        key=lambda result: (-result.weighted_score, result.kind),
    )
    unscored = [result.kind for result in results if result.weighted_score is None]

    disqualified: list[dict] = []
    eligible: list[OptionResult] = []
    for result in ranked:
        if require_all_criteria and result.unknown_criteria:
            disqualified.append(
                {"kind": result.kind, "reason": "unknown_criteria", "criteria": list(result.unknown_criteria)}
            )
            continue
        eligible.append(result)

    if not eligible:
        return {
            "recommended_kind": None,
            "outcome": "insufficient_evidence",
            "ranking": [result.to_dict() for result in results],
            "unscored_options": unscored,
            "disqualified": disqualified,
            "unknown_criteria_by_option": {result.kind: list(result.unknown_criteria) for result in results},
            "preserved_alternatives": [result.to_dict() for result in results],
            "recommendation_is_not_authorization": True,
            "execution_authorized": False,
            "note": "no option has enough supplied evidence to rank",
        }

    top = eligible[0]
    tied = [result for result in eligible if result.weighted_score == top.weighted_score]
    outcome = "unresolved_tie" if len(tied) > 1 else "recommended"
    return {
        "recommended_kind": None if outcome == "unresolved_tie" else top.kind,
        "outcome": outcome,
        "tied_kinds": [result.kind for result in tied] if outcome == "unresolved_tie" else [],
        "ranking": [result.to_dict() for result in results],
        "unscored_options": unscored,
        "disqualified": disqualified,
        "unknown_criteria_by_option": {result.kind: list(result.unknown_criteria) for result in results},
        "preserved_alternatives": [result.to_dict() for result in results],
        "recommendation_is_not_authorization": True,
        "execution_authorized": False,
        "note": "a ranking is evidence for a human decision, never permission to execute",
    }


def default_option_set(*, reuse_evidence: tuple[str, ...] = ()) -> list[SolutionOption]:
    """The six options, unscored — a checklist, not a recommendation.

    Unscored on purpose: filling in scores without inspecting the system would be exactly
    the manufactured confidence this program exists to remove.
    """
    return [
        SolutionOption(
            kind="reuse_existing",
            description="Reuse an existing component that already provides the behaviour.",
            evidence_refs=reuse_evidence,
            risks=("may not fit the new context without adaptation",),
        ),
        SolutionOption(
            kind="repair_existing",
            description="Fix a defect in the existing implementation rather than adding a second one.",
        ),
        SolutionOption(
            kind="extend_existing",
            description="Add the missing capability to the existing component behind its current contract.",
        ),
        SolutionOption(
            kind="replace_component",
            description="Replace a component only when it is demonstrably unsuitable.",
            risks=("migration cost", "temporary dual-run risk"),
        ),
        SolutionOption(
            kind="defer",
            description="Defer until a blocking measurement or authorization exists.",
            risks=("the underlying problem remains",),
        ),
        SolutionOption(
            kind="no_change",
            description="Make no change; the observed problem does not justify the risk.",
        ),
    ]
