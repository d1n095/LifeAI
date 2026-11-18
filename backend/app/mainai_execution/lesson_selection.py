"""Effectiveness-driven lesson SELECTION — the conservative, explainable ranking layer that
turns measured outcomes into planning influence (Round 8, M1).

Round 7 built the two halves of the measurement: the lesson -> plan-decision provenance
edge (migration 0074) and the guard-observation rollup (`lesson_effectiveness()`). What
they did NOT do is change any planning decision — deliberately, pending review of the
standing report's shape. This module is that review's outcome: the rules are conservative,
explainable from the raw numbers, and re-computed from the evidence tables on EVERY
planning pass — nothing is cached, nothing mutates the lesson, and no exclusion is
permanent by construction.

The four standing classes the mission names, and the rule each maps to:

  PROVEN      held > 0, failed == 0              -> selected FIRST (a track record, not
                                                    a guarantee: `guard_held` still means
                                                    "held here", never "caused success")
  UNPROVEN    no verdicts yet                    -> selected after proven (absence of
                                                    evidence is not negative evidence)
  MIXED       held > 0, failed > 0, ratio>=0.5   -> selected, FLAGGED with the numbers
  SINGLE_FAIL failed == 1, held == 0             -> selected, FLAGGED — one isolated failure
                                                    must never permanently suppress an
                                                    otherwise useful rule; it still gets
                                                    to prove itself, with the flag on the
                                                    audit trail
  FAILED_MAJORITY  verdicts >= 2, ratio < 0.5    -> EXCLUDED from auto-application this pass,
                                                    flagged for founder reassessment — an
                                                    ineffective rule is reassessed rather
                                                    than blindly reinforced. NOT deleted,
                                                    NOT demoted forever: the class is
                                                    recomputed each pass, so a later held
                                                    verdict restores selection on its own.
  UNUSABLE_GUARD   any guard_unusable observation -> EXCLUDED this pass — the lesson's own
                                                    prescription is invalid as written
                                                    (pytest usage error / nothing
                                                    collected); re-adding a broken step is
                                                    blind reinforcement of a broken rule.

Anti-overcorrection invariants (enforced here and by the tests):
  - No lesson row is ever mutated by selection (SIGNAL != TRUTH WRITER, as everywhere).
  - Exclusions are per-pass and evidence-derived; they disappear the moment the evidence
    changes (a held verdict flips FAILED_MAJORITY back to MIXED automatically).
  - Every selection/exclusion carries its raw counts (applied/held/failed/unusable), so a
    reviewer can disagree with the ranking while keeping the evidence trail — the same
    per-axis discipline evidence_quality.py applies to study metadata.
  - Recurring-failure clustering (recurring_failure_clusters) DETECTS shared root causes
    across distinct tasks and surfaces them in the audit trail; it never auto-resolves,
    auto-deletes, or auto-disputes anything.

Owner scoping: standing comes from lesson_effectiveness(), which is owner-scoped end to
end (RLS + explicit filters) — owner B's planning never sees owner A's guard evidence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.lesson_guard_observations import (
    LessonEffectiveness,
    lesson_effectiveness,
)
from app.mainai_execution.lessons import lookup_lessons
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonStatus


@dataclass
class LessonStandingClass:
    """The measured-outcome classification for ONE lesson, with the numbers that produced
    it — the explainability contract: every class is derivable from these counts alone."""

    lesson: EngineeringLesson
    standing: str  # proven | unproven | mixed | single_fail | failed_majority | unusable_guard
    selected: bool
    reason: str
    applied_count: int
    guard_held: int
    guard_failed: int
    guard_unusable: int
    guard_not_exercised: int
    keep_ratio: float | None


@dataclass
class LessonSelection:
    """The full, reviewable decision record for one planning pass. `ordered` is the
    selected lessons in application order; `decisions` is EVERY considered lesson with its
    class and raw counts — selected or not, nothing is silently dropped."""

    task_type: str
    ordered: list[EngineeringLesson] = field(default_factory=list)
    decisions: list[LessonStandingClass] = field(default_factory=list)
    recurring_clusters: list[dict] = field(default_factory=list)

    def summary_for_event(self) -> list[dict]:
        """The audit block that rides on the task's created MainAITaskEvent: per-lesson
        class + counts + reason + whether the lesson sits inside a recurring-failure
        cluster (>= 2 distinct failed tasks sharing its affected_component). Historical
        evidence stays in the observation tables; this is the point-in-time explanation of
        why the plan looks the way it does."""
        cluster_ids = {
            lid for cluster in self.recurring_clusters for lid in cluster["lesson_ids"]
        }
        return [
            {
                "lesson_id": str(d.lesson.id),
                "standing": d.standing,
                "selected": d.selected,
                "reason": d.reason,
                "applied_count": d.applied_count,
                "guard_held": d.guard_held,
                "guard_failed": d.guard_failed,
                "guard_unusable": d.guard_unusable,
                "in_recurring_cluster": str(d.lesson.id) in cluster_ids,
            }
            for d in self.decisions
        ]


def _classify(lesson: EngineeringLesson, eff: LessonEffectiveness | None) -> LessonStandingClass:
    """Pure classifier: one lesson + its measured standing -> one standing class."""
    if eff is None:
        return LessonStandingClass(
            lesson=lesson, standing="unproven", selected=True,
            reason="no plan-decision provenance for this owner yet — selected without negative evidence",
            applied_count=0, guard_held=0, guard_failed=0, guard_unusable=0, guard_not_exercised=0,
            keep_ratio=None,
        )
    verdicts = eff.guard_held + eff.guard_failed
    if eff.guard_unusable > 0:
        return LessonStandingClass(
            lesson=lesson, standing="unusable_guard", selected=False,
            reason=(
                f"its named regression_test was unusable as written {eff.guard_unusable}x where applied "
                "(pytest usage error / nothing collected) — excluded from this pass, not deleted; "
                "reassessment, not blind reinforcement"
            ),
            applied_count=eff.applied_count, guard_held=eff.guard_held, guard_failed=eff.guard_failed,
            guard_unusable=eff.guard_unusable, guard_not_exercised=eff.guard_not_exercised,
            keep_ratio=eff.keep_ratio,
        )
    if verdicts >= 2 and eff.keep_ratio is not None and eff.keep_ratio < 0.5:
        return LessonStandingClass(
            lesson=lesson, standing="failed_majority", selected=False,
            reason=(
                f"its own guard failed {eff.guard_failed}/{verdicts} verdicts where it shaped the plan "
                "(keep_ratio < 0.5) — excluded from THIS pass and flagged for reassessment; the class is "
                "recomputed every pass, so a later held verdict restores selection automatically"
            ),
            applied_count=eff.applied_count, guard_held=eff.guard_held, guard_failed=eff.guard_failed,
            guard_unusable=eff.guard_unusable, guard_not_exercised=eff.guard_not_exercised,
            keep_ratio=eff.keep_ratio,
        )
    if eff.guard_failed == 1 and eff.guard_held == 0:
        return LessonStandingClass(
            lesson=lesson, standing="single_fail", selected=True,
            reason=(
                "one isolated guard failure — selected anyway: a single failure must not permanently "
                "suppress a possibly useful rule; flagged for the audit trail"
            ),
            applied_count=eff.applied_count, guard_held=eff.guard_held, guard_failed=eff.guard_failed,
            guard_unusable=eff.guard_unusable, guard_not_exercised=eff.guard_not_exercised,
            keep_ratio=eff.keep_ratio,
        )
    if eff.guard_held > 0 and eff.guard_failed == 0:
        return LessonStandingClass(
            lesson=lesson, standing="proven", selected=True,
            reason=f"guard held {eff.guard_held}/{eff.guard_held} verdicts where applied (held here, not proven causal)",
            applied_count=eff.applied_count, guard_held=eff.guard_held, guard_failed=eff.guard_failed,
            guard_unusable=eff.guard_unusable, guard_not_exercised=eff.guard_not_exercised,
            keep_ratio=eff.keep_ratio,
        )
    verdicts = eff.guard_held + eff.guard_failed
    if verdicts == 0:
        # Applied but its guard never reached a verdict (not exercised / not recorded):
        # no negative evidence, no positive evidence — selection behaves exactly as if the
        # lesson had never been applied, which is the conservative default.
        return LessonStandingClass(
            lesson=lesson, standing="unproven", selected=True,
            reason=(
                f"applied {eff.applied_count}x but its guard never reached a verdict "
                f"({eff.guard_not_exercised} not-exercised) — selected without negative evidence"
            ),
            applied_count=eff.applied_count, guard_held=eff.guard_held, guard_failed=eff.guard_failed,
            guard_unusable=eff.guard_unusable, guard_not_exercised=eff.guard_not_exercised,
            keep_ratio=None,
        )
    return LessonStandingClass(
        lesson=lesson, standing="mixed", selected=True,
        reason=(
            f"mixed record: guard held {eff.guard_held}, failed {eff.guard_failed}, "
            f"no verdict ratio {'' if eff.keep_ratio is None else f'(keep_ratio {eff.keep_ratio:.2f}) '}"
            "— selected with the record on the audit trail"
        ),
        applied_count=eff.applied_count, guard_held=eff.guard_held, guard_failed=eff.guard_failed,
        guard_unusable=eff.guard_unusable, guard_not_exercised=eff.guard_not_exercised,
        keep_ratio=eff.keep_ratio,
    )


_RANK_ORDER = {"proven": 0, "unproven": 1, "mixed": 2, "single_fail": 2}


def select_lessons_for_planning(db: Session, *, owner_id: uuid.UUID, task_type: str) -> LessonSelection:
    """Rank this owner's active, task-tagged lessons by their MEASURED standing.

    Same candidate set lookup_lessons() always returned (severity desc, created desc) —
    the ranking layer only ORDERS them and, for two evidence-backed classes (failed_majority,
    unusable_guard), excludes them from THIS pass with the reason on the record. Every other
    class applies exactly as before, so a lesson with no evidence behaves precisely as it
    did before this layer existed (conservative by construction)."""
    candidates = lookup_lessons(db, applies_to_any=[task_type])
    selection = LessonSelection(task_type=task_type)

    standing_by_id: dict[uuid.UUID, LessonEffectiveness] = {}
    if candidates:
        standing_by_id = {
            s.lesson_id: s
            for s in lesson_effectiveness(db, owner_id=owner_id, lesson_ids=[l.id for l in candidates])
        }

    for lesson in candidates:
        selection.decisions.append(_classify(lesson, standing_by_id.get(lesson.id)))

    # Application order is explicit and deterministic: standing rank (proven > unproven >
    # flagged), then severity (critical/high first), then creation recency, then id as the
    # tiebreaker — never hash or DB row order.
    def _sev(lesson: EngineeringLesson) -> int:
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
        return order.get(getattr(lesson.severity, "value", "low"), 3)

    selection.ordered = [
        d.lesson
        for d in sorted(
            (d for d in selection.decisions if d.selected),
            key=lambda d: (_RANK_ORDER.get(d.standing, 3), _sev(d.lesson), d.lesson.created_at, d.lesson.id),
        )
    ]

    from app.mainai_execution.lesson_guard_observations import recurring_failure_clusters

    selection.recurring_clusters = recurring_failure_clusters(db, owner_id=owner_id)
    return selection


def recurring_cluster_component_ids(selection: LessonSelection) -> set[uuid.UUID]:
    """Lesson ids that sit inside a recurring-failure cluster (>= 2 distinct failed tasks,
    shared affected_component) — the audit layer cross-references these so an excluded or
    flagged lesson's reason can name the cluster evidence instead of its own counts alone."""
    out: set[uuid.UUID] = set()
    for cluster in selection.recurring_clusters:
        for lid in cluster["lesson_ids"]:
            out.add(uuid.UUID(lid))
    return out
