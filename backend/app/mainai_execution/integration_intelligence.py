"""Integration intelligence — surface relevant PAST integration failures so a new Blueprint,
architectural decision or agent assignment does not repeat a known problem.

REUSE, NO PARALLEL STORE. This is a thin READ + SUMMARY layer over the existing engineering-lesson
memory (`app.mainai_execution.lessons` / `EngineeringLesson`, and the owner-scoped
`engineering_lesson_guard_observations`). It creates no new table, no second issue tracker, and
writes nothing:
  * recording a new verified finding stays `app.mainai_execution.lessons.record_lesson`
    (requirement 9);
  * requiring regression tests for a confirmed failure mode stays
    `app.mainai_execution.lessons.apply_lessons_to_verification_plan` (requirement 7).
This module only RETRIEVES relevant active lessons for an integration context (requirement 1) and
renders them as compact, honestly-labelled summaries to attach to an agent task-context package
(requirement 6), so a recommendation can explain how history influenced it (requirement 10).

Never promotes an unverified claim into an established fact: each lesson's own `confidence`
(certain/likely/uncertain) and `status` are carried verbatim into every summary, so a hypothesis
stays labelled a hypothesis. Only `status="active"` lessons are surfaced (`lookup_lessons` filters
out superseded/historical/disputed), so an OBSOLETE fix is not presented as current guidance
(requirement 8). Symptom / root cause / validated fix are reported as separate fields
(requirement 2).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.mainai_execution.lessons import lookup_lessons

# Keep each summary field compact so attaching lessons stays token-efficient in an agent packet.
_MAX_TEXT = 400


def _trim(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def integration_tags(
    *,
    components: list[str] | None = None,
    providers: list[str] | None = None,
    interfaces: list[str] | None = None,
    dependencies: list[str] | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    """Normalize an integration context (modules/providers/interfaces/dependencies) into the
    de-duplicated `applies_to` tag space that lessons are GIN-indexed on."""
    tags: list[str] = []
    for group in (components, providers, interfaces, dependencies, extra):
        for raw in group or []:
            tag = str(raw).strip()
            if tag and tag not in tags:
                tags.append(tag)
    return tags


def summarize_integration_lesson(lesson) -> dict:
    """Compact, honestly-labelled summary that DISTINGUISHES symptom / root cause / validated fix
    and carries the lesson's own confidence + status verbatim (never upgraded to 'fact')."""
    confidence = getattr(lesson.confidence, "value", str(lesson.confidence))
    return {
        "lesson_id": str(lesson.id),
        "affected_component": lesson.affected_component,
        "symptom": _trim(lesson.problem),
        "root_cause": _trim(lesson.root_cause),
        "validated_fix": _trim(lesson.fix),
        "general_rule": _trim(lesson.general_rule),
        "regression_test": lesson.regression_test,
        "severity": getattr(lesson.severity, "value", str(lesson.severity)),
        "confidence": confidence,
        "status": getattr(lesson.status, "value", str(lesson.status)),
        # Only an explicitly 'certain' lesson is a verified fact; likely/uncertain stay hypotheses.
        "verified": confidence == "certain",
        "source_ref": lesson.source_ref,
        "applies_to": list(lesson.applies_to or []),
    }


def retrieve_integration_lessons(
    db: Session,
    *,
    components: list[str] | None = None,
    providers: list[str] | None = None,
    interfaces: list[str] | None = None,
    dependencies: list[str] | None = None,
    extra_tags: list[str] | None = None,
    limit: int = 10,
):
    """Active engineering lessons relevant to an integration context, by tag (module / provider /
    interface / dependency). Reuses `lookup_lessons` (GIN-indexed, active-only, severity-ordered)
    — no Python-side scan, no new query surface."""
    tags = integration_tags(
        components=components,
        providers=providers,
        interfaces=interfaces,
        dependencies=dependencies,
        extra=extra_tags,
    )
    if not tags:
        return []
    return lookup_lessons(db, applies_to_any=tags)[:limit]


def integration_lessons_for_task_context(
    db: Session,
    *,
    components: list[str] | None = None,
    providers: list[str] | None = None,
    interfaces: list[str] | None = None,
    dependencies: list[str] | None = None,
    extra_tags: list[str] | None = None,
    max_lessons: int = 5,
) -> list[dict]:
    """The attach point: compact summaries of the most relevant known failures, ready to drop into
    an `AgentTaskContext` via
    `app.workforce.context_handoff.build_agent_task_context(integration_lessons=...)`. Bounded for
    token efficiency, honestly labelled, verified-vs-hypothesis preserved."""
    lessons = retrieve_integration_lessons(
        db,
        components=components,
        providers=providers,
        interfaces=interfaces,
        dependencies=dependencies,
        extra_tags=extra_tags,
        limit=max_lessons,
    )
    return [summarize_integration_lesson(lesson) for lesson in lessons]
