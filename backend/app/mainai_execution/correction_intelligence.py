"""Correction intelligence over the EXISTING self-correction records — the P3 increment
(Founder Alpha round, 2026-11-18). Extends app/mainai_execution/lessons.py,
lesson_guard_observations.py and app/models/problem_learning.py; adds no table, no
migration, no model leg, and — deliberately — no new persistent record type.

WHY NO NEW "CORRECTION RECORD" TABLE: the repo already has the durable correction records
the mission asks for, and inventing a parallel one would fork the truth:

- `EngineeringLesson` (founder-wide, append-only, mandatory provenance: problem /
  root_cause / evidence / fix / regression_test) IS the persistent correction record;
- `EngineeringLessonGuardObservation` (append-only, idempotent on source_ref, RLS-
  owner-scoped) IS the persistent record of what a correction's guard did afterwards;
- `LifeProblem` (owner-scoped, idempotency_key, supersedes chain) IS the persistent
  record of a problem the founder or the agent surfaced.

Nothing in this module may promote itself into those tables' contents either — the
non-promotion rule (a lesson is never auto-created, auto-applied beyond plan-time
regression-test append, or auto-rewritten) is what keeps a correction from silently
becoming policy. What was genuinely missing — and what this module adds — is the
INTELLIGENCE AROUND those records:

1. `detect_recurring_problems()` — recurrence detection: the same normalized problem
   surfacing again and again within a window, with first/last seen and how many times.
   A recurrence is the trigger a founder (or a future gated model leg, FR-14) needs
   before writing a LESSON — one-off noise never escalates.
2. `classify_correction_root_cause()` — deterministic root-cause classification into the
   CLOSED MainAIJobErrorCategory vocabulary (no new enum, no guessed buckets): the
   recorded job error category wins when present; free text maps through an explicit,
   reviewed keyword table; no match → unexpected. Same discipline as ClaimType's
   "uncategorized, never a guess dressed up as a real value".
3. `correction_effectiveness()` — the measurement the mission asks for ("whether
   corrective changes actually improve subsequent decisions"), built ONLY on
   LessonGuardObservation aggregates, with the docstring's honesty contract preserved:
   counts of held/failed/not-exercised guards, first/last observation, and a FIXED
   interpretation string that never claims the lesson caused anything. The
   before/after-recurrence comparison for the SAME normalized signature is included as
   `recurrence_delta` where the lesson's problem text matches a recurring LifeProblem
   signature — reported as an observation, explicitly not a causal claim.
4. `overcorrection_risk()` — the anti-overcorrection gate: an ADVISORY (never mutating)
   check on a verification plan, flagging when lesson-derived guards would dominate the
   plan (count and share caps), duplicate a target the founder already authored, or
   duplicate each other. `apply_lessons_to_verification_plan()` already refuses
   conflicted lesson pairs; this catches the subtler failure mode — a plan that is
   mostly re-checking past corrections instead of verifying new work, i.e. a correction
   that has grown into a de-facto ban on legitimate behavior.

Owner scoping: recurrence queries filter `owner_id` explicitly and RLS composes on top.
Lessons are founder-wide by design; their guard observations carry owner-scoped facts
and are queried owner-scoped here.
"""

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.lesson_guard_observations import EngineeringLessonGuardObservation, LessonGuardOutcome
from app.models.mainai_execution import EngineeringLesson
from app.models.mainai_job import MainAIJobErrorCategory
from app.models.problem_learning import LifeProblem

_WS_RUN = re.compile(r"\s+")

# Outcomes that mean the lesson's own guard RAN and held (the two held values of
# LessonGuardOutcome — kept in one place so a future enum value can't silently
# masquerade as "held" here).
_GUARD_HELD_OUTCOMES = {LessonGuardOutcome.guard_held.value, LessonGuardOutcome.guard_held_task_failed_elsewhere.value}
_GUARD_FAILED_OUTCOMES = {LessonGuardOutcome.guard_failed.value, LessonGuardOutcome.guard_unusable.value}
_GUARD_NOT_EXERCISED_OUTCOMES = {LessonGuardOutcome.guard_not_exercised.value, LessonGuardOutcome.lesson_superseded.value}

# Advisory caps for overcorrection_risk: a verification plan that is mostly lesson
# re-checks stops verifying NEW work. These are review thresholds, not hard bans — the
# report is advisory, the planner keeps its own judgment.
MAX_GUARD_STEPS_DEFAULT = 5
MAX_GUARD_SHARE_DEFAULT = 0.5


def normalize_problem_signature(text: str) -> str:
    """The recurrence signature: lower-cased, whitespace-collapsed, trimmed. Deliberately
    EXACT-TEXT structural identity (same discipline as epistemics' cross-domain claim
    overlaps) — never a semantic "these two problems are about the same thing" claim."""
    return _WS_RUN.sub(" ", (text or "").strip().lower())


@dataclass
class RecurringProblemGroup:
    """One recurring problem signature within the window. `problem_ids` preserves
    first-seen order — the audit trail behind the count."""

    signature: str
    occurrences: int
    first_seen: datetime
    last_seen: datetime
    problem_ids: list[uuid.UUID] = field(default_factory=list)
    unresolved_last: bool = False  # the most recent occurrence is still open (not resolved_at)


def detect_recurring_problems(
    db: Session,
    owner_id: uuid.UUID,
    *,
    window_days: int = 90,
    min_occurrences: int = 2,
) -> list[RecurringProblemGroup]:
    """Problems whose normalized title recurs >= min_occurrences times within the last
    `window_days`, oldest first inside each group, groups ordered by occurrences desc then
    last_seen desc (the loudest recurrence first). Owner-scoped end to end."""
    if min_occurrences < 2:
        min_occurrences = 2  # one occurrence is not a recurrence, by definition
    since = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
    problems = (
        db.query(LifeProblem)
        .filter(LifeProblem.owner_id == owner_id, LifeProblem.created_at >= since)
        .order_by(LifeProblem.created_at.asc(), LifeProblem.id.asc())
        .all()
    )

    groups: dict[str, RecurringProblemGroup] = {}
    for p in problems:
        signature = normalize_problem_signature(p.title)
        if not signature:
            continue
        group = groups.get(signature)
        if group is None:
            group = RecurringProblemGroup(
                signature=signature, occurrences=0, first_seen=p.created_at, last_seen=p.created_at
            )
            groups[signature] = group
        group.occurrences += 1
        group.problem_ids.append(p.id)
        if p.created_at and p.created_at > group.last_seen:
            group.last_seen = p.created_at
        group.unresolved_last = p.resolved_at is None  # last write wins → the latest occurrence's state

    result = [g for g in groups.values() if g.occurrences >= min_occurrences]
    result.sort(key=lambda g: (-g.occurrences, -g.last_seen.timestamp()))
    return result


def classify_correction_root_cause(
    *,
    error_category: str | None = None,
    text: str = "",
) -> MainAIJobErrorCategory:
    """Deterministic root-cause classification in the CLOSED MainAIJobErrorCategory
    vocabulary. A recorded category is authoritative when valid; otherwise free text maps
    through an explicit keyword table; nothing matches → unexpected. Never invents a
    category outside the enum."""
    if error_category:
        try:
            return MainAIJobErrorCategory(error_category)
        except ValueError:
            pass  # a stale/free-form category string must not crash classification
    haystack = (text or "").lower()
    keyword_map: list[tuple[tuple[str, ...], MainAIJobErrorCategory]] = [
        (("timeout", "timed out", "deadline"), MainAIJobErrorCategory.timeout),
        (("connection", "unreachable", "temporarily", "rate limit", "503", "retry"), MainAIJobErrorCategory.transient_io),
        (("permission denied", "not configured", "capability", "unsupported"), MainAIJobErrorCategory.capability_unavailable),
        (("cancelled", "canceled", "revoked"), MainAIJobErrorCategory.cancelled_by_owner),
        (("assertionerror", "assertion", "syntaxerror", "typeerror", "integrityerror"), MainAIJobErrorCategory.permanent),
    ]
    for needles, category in keyword_map:
        if any(n in haystack for n in needles):
            return category
    return MainAIJobErrorCategory.unexpected


@dataclass
class CorrectionEffectivenessReport:
    """Guard-observation aggregates for one lesson. NOT a causal verdict — see the fixed
    `interpretation` contract (and app/models/lesson_guard_observations.py's module
    docstring, which this deliberately does not upgrade)."""

    lesson_id: uuid.UUID
    observations_by_outcome: dict[str, int] = field(default_factory=dict)
    exercised_total: int = 0
    held_total: int = 0
    failed_total: int = 0
    not_exercised_total: int = 0
    first_observed_at: datetime | None = None
    last_observed_at: datetime | None = None
    recurrence_delta: int | None = None  # occurrences-after minus occurrences-before (signature match), None when no signature match
    interpretation: str = (
        "Observation aggregates only: a held guard proves the guard ran and held in that "
        "context — never that the lesson caused the outcome. Attribution needs durable "
        "provenance of HOW the lesson altered later execution, which does not exist yet."
    )


def correction_effectiveness(
    db: Session,
    owner_id: uuid.UUID,
    lesson: EngineeringLesson,
    *,
    window_days: int = 90,
) -> CorrectionEffectivenessReport:
    """Aggregate THIS owner's guard observations for the lesson, and — when the lesson's
    problem text matches a recurring LifeProblem signature — the recurrence delta around
    the lesson's creation. Every number is an observation, never an attribution."""
    observations = (
        db.query(EngineeringLessonGuardObservation)
        .filter(
            EngineeringLessonGuardObservation.owner_id == owner_id,
            EngineeringLessonGuardObservation.lesson_id == lesson.id,
        )
        .order_by(EngineeringLessonGuardObservation.created_at.asc())
        .all()
    )

    report = CorrectionEffectivenessReport(lesson_id=lesson.id)
    timestamps: list[datetime] = []
    for obs in observations:
        report.observations_by_outcome[obs.outcome] = report.observations_by_outcome.get(obs.outcome, 0) + 1
        if obs.outcome in _GUARD_HELD_OUTCOMES:
            report.held_total += 1
            report.exercised_total += 1
        elif obs.outcome in _GUARD_FAILED_OUTCOMES:
            report.failed_total += 1
            report.exercised_total += 1
        elif obs.outcome in _GUARD_NOT_EXERCISED_OUTCOMES:
            report.not_exercised_total += 1
        created = getattr(obs, "created_at", None)
        if created is not None:
            timestamps.append(created)
    if timestamps:
        report.first_observed_at = min(timestamps)
        report.last_observed_at = max(timestamps)

    signature = normalize_problem_signature(lesson.problem)
    if signature:
        since = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
        matched = (
            db.query(LifeProblem)
            .filter(LifeProblem.owner_id == owner_id, LifeProblem.created_at >= since)
            .all()
        )
        before = [p for p in matched if normalize_problem_signature(p.title) == signature and p.created_at < lesson.created_at]
        after = [p for p in matched if normalize_problem_signature(p.title) == signature and p.created_at >= lesson.created_at]
        if before or after:
            report.recurrence_delta = len(after) - len(before)

    return report


@dataclass
class OvercorrectionReport:
    """Advisory anti-overcorrection check on one (augmented) verification plan."""

    guard_steps: int = 0
    total_steps: int = 0
    guard_share: float = 0.0
    risks: list[str] = field(default_factory=list)
    acceptable: bool = True

    @property
    def summary(self) -> str:
        if self.acceptable:
            return "no overcorrection signal: lesson guards stay a minority of the plan"
        return "; ".join(self.risks)


def overcorrection_risk(
    verification_plan: list[dict],
    *,
    lesson_targets: set[str] | None = None,
    founder_targets: set[str] | None = None,
    max_guard_steps: int = MAX_GUARD_STEPS_DEFAULT,
    max_guard_share: float = MAX_GUARD_SHARE_DEFAULT,
) -> OvercorrectionReport:
    """Flag when a plan is dominated by past-lesson re-checks — the mechanism by which a
    correction would quietly disable legitimate behavior (a task that can never pass
    because the plan re-litigates every old mistake). Advisory: never mutates the plan;
    the caller decides. `lesson_targets` = the targets the lesson layer contributed
    (apply_lessons_to_verification_plan's applied ids' regression tests);
    `founder_targets` = the targeted_tests targets the plan already had BEFORE lesson
    augmentation (needed to detect a lesson shadowing an identical founder target —
    authorship is not recoverable from the augmented plan alone)."""
    plan = verification_plan or []
    lesson_targets = lesson_targets or set()
    founder_targets = founder_targets or set()
    total = len(plan)
    guard_steps = sum(1 for step in plan if step.get("kind") == "targeted_tests" and step.get("target") in lesson_targets)
    share = (guard_steps / total) if total else 0.0

    report = OvercorrectionReport(guard_steps=guard_steps, total_steps=total, guard_share=round(share, 4))
    if guard_steps > max_guard_steps:
        report.risks.append(
            f"{guard_steps} lesson-guard steps exceed the review cap of {max_guard_steps} — "
            "the plan is mostly re-checking past corrections"
        )
    if total and share > max_guard_share:
        report.risks.append(
            f"lesson guards are {report.guard_share:.0%} of the plan (cap {max_guard_share:.0%}) — "
            "new work is being verified less than old corrections are being re-checked"
        )

    targets = [step.get("target") for step in plan if step.get("kind") == "targeted_tests"]
    duplicates = {t for t in targets if targets.count(t) > 1}
    if duplicates:
        report.risks.append(
            "duplicate verification target(s) in one plan: " + ", ".join(sorted(str(d) for d in duplicates)) +
            " — redundant re-checking, not additional assurance"
        )
    shadowed = founder_targets & lesson_targets
    if shadowed:
        report.risks.append(
            "lesson guard target(s) identical to founder-authored target(s): " + ", ".join(sorted(str(s) for s in shadowed)) +
            " — the lesson adds nothing here and should not double-run"
        )
    report.acceptable = not report.risks
    return report


def count_prior_occurrences(
    db: Session,
    owner_id: uuid.UUID,
    title: str,
    *,
    window_days: int = 90,
) -> int:
    """How many of the owner's problems within the window share this (normalized) title —
    the write-time recurrence signal for problem recording, BEFORE the new row exists.
    Unlike detect_recurring_problems (which only returns groups already at
    min_occurrences), this answers the "is this the second time?" question directly."""
    signature = normalize_problem_signature(title)
    if not signature:
        return 0
    since = datetime.utcnow() - timedelta(days=max(1, int(window_days)))
    candidates = (
        db.query(LifeProblem)
        .filter(LifeProblem.owner_id == owner_id, LifeProblem.created_at >= since)
        .all()
    )
    return sum(1 for p in candidates if normalize_problem_signature(p.title) == signature)
