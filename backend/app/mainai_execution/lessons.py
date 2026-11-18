"""Engineering lesson / safety memory foundation (migration 0032's `engineering_lessons` —
see app/models/mainai_execution.py's EngineeringLesson for the full column rationale,
including why this is deliberately its OWN table, not RLS-protected, and never auto-promoted
to founder-approved truth). Two things live here:

  - record_lesson()/lookup_lessons(): plain CRUD-ish persistence and tag-based lookup, with
    provenance (source_type/source_ref) mandatory at the model level (NOT NULL columns) — there
    is no code path in this module that can create an unsourced lesson.
  - apply_lessons_to_verification_plan(): the one place a lesson is allowed to actually DO
    something, wired into app/mainai_execution/planner.py's create_plan(). A lesson whose
    `applies_to` tags include a task's `task_type` and which names a `regression_test` gets that
    regression test added to the task's `verification_plan` if it isn't already covered — real
    influence on planning, not a loose AI-generated summary with no source and no effect. Every
    task whose verification_plan was touched this way gets a `lessons_applied` list recorded on
    its `created` MainAITaskEvent (see planner.py) so the provenance survives in the durable
    event history, not just in this function's return value."""

import uuid

from sqlalchemy import String, cast, select
from sqlalchemy.dialects.postgresql import ARRAY
import logging

from sqlalchemy.orm import Session

from app.models.mainai_execution import EngineeringLesson, EngineeringLessonConfidence, EngineeringLessonStatus


logger = logging.getLogger("mainai.lessons")


def record_lesson(
    db: Session,
    *,
    problem: str,
    root_cause: str,
    affected_component: str,
    severity,
    evidence: str,
    fix: str,
    general_rule: str,
    applies_to: list[str],
    source_type: str,
    source_ref: str,
    created_by: str,
    first_seen_at,
    regression_test: str | None = None,
    confidence: EngineeringLessonConfidence = EngineeringLessonConfidence.likely,
) -> EngineeringLesson:
    lesson = EngineeringLesson(
        status=EngineeringLessonStatus.active,
        problem=problem,
        root_cause=root_cause,
        affected_component=affected_component,
        severity=severity,
        evidence=evidence,
        fix=fix,
        regression_test=regression_test,
        general_rule=general_rule,
        applies_to=applies_to,
        source_type=source_type,
        source_ref=source_ref,
        first_seen_at=first_seen_at,
        confidence=confidence,
        created_by=created_by,
    )
    db.add(lesson)
    db.flush()
    return lesson


def record_lesson_from_founder_correction(
    db: Session,
    *,
    note,
    root_cause: str,
    affected_component: str,
    general_rule: str,
    applies_to: list[str],
    created_by: str,
    fix: str,
    severity=None,
    regression_test: str | None = None,
    confidence: EngineeringLessonConfidence = EngineeringLessonConfidence.likely,
) -> EngineeringLesson:
    """docs/MAINAI_PERSONAL_INTENT_EXECUTIVE_REASONING.md §5 (missed-thing learning): "we forgot
    X" / "why didn't you think of Y" is a real, structured lesson source, not just a
    conversational remark to log and discard. Turns a confirmed `FounderMemoryNote`
    (`note_type="correction"`) into a real `EngineeringLesson`, reusing `record_lesson()`
    completely unchanged -- no new table, no schema change. `note` is a real, already-durable
    `app.founder_memory.FounderMemoryNote` row (owner-scoped) the CALLER already fetched and
    confirmed is a correction; this function never re-derives or re-classifies it, and never
    reads founder_memory_notes itself (kept as a plain, untyped parameter specifically to avoid
    a module-level app.mainai_execution -> app.founder_memory import, matching this codebase's
    established "local import, no hard cross-module dependency" convention elsewhere).

    `EngineeringLesson` is deliberately founder-wide, not owner-scoped (see this module's own
    docstring) -- a reasoning lesson generalizes the same way a code lesson already does; this
    crosses the RLS boundary the same, already-established way `record_lesson()` always has for
    every other caller, not a new exception carved out for this one.

    `problem`/`evidence`/`first_seen_at` are taken FROM the note (never re-typed by the caller,
    matching `founder_memory_notes.content`'s own immutability -- the lesson's own `problem`
    field is a durable quote of what the founder actually said, not a paraphrase). `root_cause`/
    `fix`/`general_rule`/`applies_to`/`affected_component` remain the caller's own required,
    explicit judgment -- reusing record_lesson()'s own "never auto-generalized" discipline
    verbatim; a MISS becomes a LESSON only via a deliberate act that names what actually went
    wrong and how narrowly it applies, never automatically from the raw correction text alone
    (this is what keeps `applies_to` narrow enough to avoid overgeneralizing a one-off, exactly
    as the founder's own spec requires)."""
    if getattr(note, "note_type", None) != "correction":
        raise ValueError(f"record_lesson_from_founder_correction requires a note_type='correction' note, got {getattr(note, 'note_type', None)!r}")

    from app.models.mainai_execution import EngineeringLessonSeverity

    return record_lesson(
        db,
        problem=note.content,
        root_cause=root_cause,
        affected_component=affected_component,
        severity=severity or EngineeringLessonSeverity.medium,
        evidence=f"founder_memory_notes:{note.id}",
        fix=fix,
        general_rule=general_rule,
        applies_to=applies_to,
        source_type="founder_correction",
        source_ref=str(note.id),
        created_by=created_by,
        first_seen_at=note.observed_at,
        regression_test=regression_test,
        confidence=confidence,
    )


def lookup_lessons(db: Session, *, applies_to_any: list[str]) -> list[EngineeringLesson]:
    """Active lessons tagged with ANY of `applies_to_any` — uses the GIN-indexed jsonb `?|`
    "any array element matches" operator (migration 0032's own ix_engineering_lessons_applies_to
    index exists specifically for this query shape), not a Python-side scan."""
    if not applies_to_any:
        return []
    # The `?|` jsonb operator requires a real Postgres text[] on its right-hand side -- without
    # this explicit ARRAY(String) cast, SQLAlchemy binds the Python list as a JSON-encoded
    # string parameter instead, which Postgres then rejects (DataError: operator does not
    # exist: jsonb ?| text).
    tags = cast(applies_to_any, ARRAY(String))
    return (
        db.execute(
            select(EngineeringLesson)
            .where(EngineeringLesson.status == EngineeringLessonStatus.active, EngineeringLesson.applies_to.op("?|")(tags))
            .order_by(EngineeringLesson.severity.desc(), EngineeringLesson.created_at.desc())
        )
        .scalars()
        .all()
    )


def apply_lessons_to_verification_plan(db: Session, *, task_type: str, verification_plan: list[dict]) -> tuple[list[dict], list[uuid.UUID]]:
    """Returns (possibly-augmented verification_plan, ids of lessons that actually changed
    it) -- called once per planned task from planner.py's create_plan(), never from the
    executor (a lesson influences what gets PLANNED, not what an already-dispatched task does
    on the fly, which would be an untracked runtime behavior change instead of a durable,
    reviewable planning decision).

    Hardening: `regression_test` is lesson-authored content (seed scripts, future
    auto-recording) that becomes a `targeted_tests` argv. create_plan() validates AI-
    proposed verification targets BEFORE this function runs, so an unsafe lesson target
    previously bypassed plan-time fail-closed and only failed at the subprocess boundary.
    Validate (or skip) here so plan persistence never records an absolute/`..` path.

    Conflict candidates (same affected_component + overlapping applies_to) are skipped
    deterministically until the async conflict tick (or founder review) resolves them.
    Applying BOTH sides of an unresolved candidate pair would inject contradictory
    regression targets; the AI judgment path must not be required for this safety gate.
    """
    from app.mainai_execution.lesson_conflicts import find_conflict_candidate_pairs
    from app.mainai_execution.verify import VerificationStepError, validate_targeted_tests_target

    lessons = lookup_lessons(db, applies_to_any=[task_type])
    conflicted_ids: set[uuid.UUID] = set()
    for a, b in find_conflict_candidate_pairs(db, lessons=lessons):
        conflicted_ids.add(a.id)
        conflicted_ids.add(b.id)

    existing_targets = {step.get("target") for step in verification_plan if step.get("kind") == "targeted_tests"}

    augmented = list(verification_plan)
    applied_lesson_ids: list[uuid.UUID] = []
    for lesson in lessons:
        if lesson.id in conflicted_ids:
            continue
        if not lesson.regression_test or lesson.regression_test in existing_targets:
            continue
        try:
            target = validate_targeted_tests_target(lesson.regression_test)
        except VerificationStepError:
            # Fail closed for this lesson only — do not abort the whole plan, and do not
            # inject the unsafe target. A bad seed/auto lesson must never widen argv scope.
            continue
        augmented.append({"kind": "targeted_tests", "target": target})
        existing_targets.add(target)
        applied_lesson_ids.append(lesson.id)

    return augmented, applied_lesson_ids


def record_lesson_plan_decisions(db: Session, *, task, applied_lesson_ids: list[uuid.UUID]) -> int:
    """The lesson -> changed-planning-decision PROVENANCE EDGE migration 0058 explicitly
    deferred: one durable `engineering_lesson_plan_decisions` row per (lesson, task) the
    lesson really shaped, written HERE — at the moment `apply_lessons_to_verification_plan()`
    appended the lesson's regression target to this task's verification_plan and
    planner.create_plan() persisted it — not re-derived later from event-detail strings.

    Together with 0058's guard observations this edge is what lets
    lesson_guard_observations.lesson_effectiveness() measure a rule's standing from
    APPLIED-count to guard OUTCOME (observations are counted only where a decision row
    proves the lesson actually shaped that task's plan) — the measured form of "recurring
    failures lead to improvements in decision rules" without ever manufacturing causality:
    a held guard is still "the guard held here", never "the lesson caused this".

    Idempotent on (task_id, lesson_id) via ON CONFLICT DO NOTHING — a replayed planner pass
    cannot inflate a lesson's applied count; a re-planned goal writes new rows because it
    creates a new task. Append-only; the caller owns the transaction (called from
    create_plan, commit=False, atomic with the plan itself)."""
    if not applied_lesson_ids:
        return 0

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.models.lesson_plan_decisions import (
        EngineeringLessonPlanDecision,
        LessonPlanDecisionType,
    )

    lessons = (
        db.query(EngineeringLesson).filter(EngineeringLesson.id.in_(list(applied_lesson_ids))).all()
    )
    written = 0
    for lesson in lessons:
        # The step appended is exactly what apply_lessons_to_verification_plan validated and
        # added — rebuilding it here keeps the decision row faithful to the actual plan step
        # (same target string, same kind).
        step = {"kind": "targeted_tests", "target": lesson.regression_test}
        db.execute(
            pg_insert(EngineeringLessonPlanDecision)
            .values(
                owner_id=task.owner_id,
                lesson_id=lesson.id,
                task_id=task.id,
                goal_id=task.goal_id,
                decision_type=LessonPlanDecisionType.verification_step_added.value,
                affected_step=step,
            )
            .on_conflict_do_nothing(constraint="uq_engineering_lesson_plan_decisions_source_ref")
        )
        written += 1
    return written


def record_lesson_guard_advisory(
    db,
    *,
    owner_id,
    goal_id,
    task_type: str,
    spec_verification_plan: list[dict],
    augmented_verification_plan: list[dict],
    applied_lesson_ids: list,
) -> None:
    """Anti-overcorrection advisory at PLAN time (app/mainai_execution/correction_intelligence.py):
    when the lesson layer's additions dominate a task's verification plan, the plan is
    mostly re-checking past corrections instead of verifying new work — the mechanism by
    which a correction would quietly disable legitimate behavior. ADVISORY ONLY: this never
    mutates the plan (apply_lessons_to_verification_plan's additions stand), it records the
    signal where the founder can see it — the audit trail — and logs it. Called from
    planner.create_plan right after apply_lessons_to_verification_plan, inside the same
    transaction (commit=False so the advisory is atomic with the plan itself)."""
    from app.mainai_execution.correction_intelligence import overcorrection_risk

    lesson_targets: set[str] = set()
    if applied_lesson_ids:
        from sqlalchemy.orm import Session  # noqa: F401 - typing only
        from app.models.mainai_execution import EngineeringLesson

        for lesson in db.query(EngineeringLesson).filter(EngineeringLesson.id.in_(list(applied_lesson_ids))).all():
            if lesson.regression_test:
                lesson_targets.add(lesson.regression_test)
    founder_targets = {
        step.get("target") for step in (spec_verification_plan or []) if step.get("kind") == "targeted_tests"
    }
    report = overcorrection_risk(
        augmented_verification_plan or [],
        lesson_targets=lesson_targets,
        founder_targets=founder_targets,
    )
    if report.acceptable:
        return
    detail = f"task_type={task_type}; {report.summary}"
    # The rules' own track record rides on the advisory (lesson_effectiveness, fed by the
    # 0074 plan-decision edge): a founder reading "guards dominate this plan" also reads
    # whether those same rules' guards have been HOLDING or FAILING where applied — the
    # measurable-effectiveness half of the self-correction loop, surfaced where the
    # overcorrection risk is already being reported. Advisory-only, like everything here.
    try:
        from app.mainai_execution.lesson_guard_observations import (
            format_lesson_standing_for_advisory,
            lesson_effectiveness,
        )

        standing = lesson_effectiveness(db, owner_id=owner_id, lesson_ids=list(applied_lesson_ids) if applied_lesson_ids else None)
        detail += format_lesson_standing_for_advisory(standing)
    except Exception:  # noqa: BLE001 - advisory enrichment must never break planning
        logger.warning("lesson standing enrichment failed; advisory records the risk alone", exc_info=True)
    logger.warning("Overcorrectionsrisk i verifieringsplan: %s", detail)
    from app.audit import record_audit

    record_audit(
        db,
        user_id=owner_id,
        action="lesson_guard_overcorrection_advisory",
        entity_type="mainai_goal",
        entity_id=str(goal_id) if goal_id else None,
        detail=detail,
        commit=False,
    )
