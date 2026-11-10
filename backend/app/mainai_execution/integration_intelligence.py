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

Owner/project scope (independent-review handoff, PR #380): retrieval here is intentionally
tag-only, with no owner/project predicate, because `EngineeringLesson` itself carries no
owner_id/project column and is documented in at least three other places
(`app.models.mainai_execution.EngineeringLesson`'s own docstring,
`app.mainai_execution.lessons`'s module docstring, and
docs/LIFE_VAULT_V4_V5_V7_V8_DESIGN_MEMOS.md / docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md /
docs/LIFE_VAULT_EGRESS_CONTROL.md's V4 entry) as deliberately founder-wide, cross-project
knowledge in this single-founder system ("MainAI 0.1 is single-founder-only today" —
LIFE_VAULT_EGRESS_CONTROL.md). Two different integration contexts matching the same tag are
the SAME founder's own lesson store queried twice, not a cross-tenant disclosure — see
`tests/backend/mainai/test_integration_intelligence_hardening.py`'s
`test_engineering_lesson_has_no_owner_or_project_column_by_documented_design` /
`test_identical_tags_from_two_synthetic_contexts_share_the_same_lesson_by_design`, which pin
this so a future PR cannot silently reintroduce per-owner lesson data without updating both the
schema and that test. What IS enforced here, per lesson field, trust zone, and verification
claim, is `app.workforce.context_handoff._sanitize_integration_lessons` et al. — this module
only retrieves and summarizes; it never decides disclosure.

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
    """Compact, honestly-labelled summary that keeps CONFIDENCE, VERIFICATION STATUS and EVIDENCE
    PROVENANCE as SEPARATE meanings (requirement 1):

    - `confidence` (certain/likely/uncertain) is the AUTHOR's own confidence — never, by itself,
      proof that anything was verified.
    - `verification_status` is the lesson's ACTUAL verification state. A fix is `verified` ONLY when
      it was validated by an executable regression test — the exact signal
      `verification_status == "verified_by_regression_test"` that `app.inspectable_memory.service`
      already treats as `MemoryTruthState.VERIFIED` (reused, not reinvented). So a `certain` but
      unverified lesson is NOT verified, and a low-confidence lesson WITH a passing regression test
      IS (requirements 2/3).
    - `fix_status` distinguishes a PROPOSED fix from one VALIDATED by a regression test
      (requirement 4).
    - Historical/superseded lessons are never current validated guidance: `verified` additionally
      requires `status == "active"` (requirement 5).
    - `source_ref` (+ evidence) is retained so a recommendation can cite its provenance
      (requirement 6)."""
    confidence = getattr(lesson.confidence, "value", str(lesson.confidence))
    status = getattr(lesson.status, "value", str(lesson.status))
    verification_status = getattr(lesson, "verification_status", None) or "unverified"
    fix_validated_by_regression_test = verification_status == "verified_by_regression_test"
    return {
        "lesson_id": str(lesson.id),
        "affected_component": lesson.affected_component,
        "symptom": _trim(lesson.problem),
        "root_cause": _trim(lesson.root_cause),
        "fix": _trim(lesson.fix),
        "fix_status": "validated_by_regression_test" if fix_validated_by_regression_test else "proposed",
        "general_rule": _trim(lesson.general_rule),
        "has_regression_test": bool(lesson.regression_test),
        "regression_test": lesson.regression_test,
        "severity": getattr(lesson.severity, "value", str(lesson.severity)),
        # Three SEPARATE axes — never conflated:
        "confidence": confidence,  # author confidence
        "verification_status": verification_status,  # actual verification state
        # Verified = validated by a regression test AND still current. Confidence alone never suffices.
        "verified": fix_validated_by_regression_test and status == "active",
        "status": status,
        "source_ref": lesson.source_ref,  # evidence provenance
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
