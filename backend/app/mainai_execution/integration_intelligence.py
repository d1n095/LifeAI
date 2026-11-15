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
(requirement 8). Symptom / root cause / proposed fix are reported as separate fields
(requirement 2). A stored verification string is never, on its own, presented as a regression-proven
fix — see `summarize_integration_lesson` and Founder decision #380 (2026-11-10).
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


def _has_executed_regression_run_evidence(db: Session | None, *, lesson_id, regression_test: str | None) -> bool:
    """FOUNDER DECISION #380 (2026-11-10), evidence-based resolution. A STORED `verification_status`
    string (even `"verified_by_regression_test"`) plus an allowlisted `regression_test` NAME is NOT
    proof a regression test executed and passed against this lesson's version (migration 0063:
    "naming a regression_test is not itself proof it caught the bug"). An earlier revision of this
    function concluded "no mechanism linking a lesson version to a concrete executed test-run
    outcome exists" and hard-coded `False` forever. That conclusion was wrong: this codebase already
    durably records exactly that proof, in two existing places this function reuses rather than
    inventing a third or a new migration:

      1. `app.mainai_execution.planner.create_plan()`'s `created` `MainAITaskEvent.detail
         ["lessons_applied"]` — the only existing binding from a lesson id to a real task (written
         by `app.mainai_execution.lessons.apply_lessons_to_verification_plan`). Without this, a
         passing test for a DIFFERENT lesson that merely shares a `regression_test` string could
         never be told apart from genuine evidence — this is what prevents that ("content identity",
         founder requirement 2/3).
      2. `app.mainai_execution.execution_job._finalize_task_outcome()`'s `verification_passed`/
         `verification_failed` `MainAITaskEvent.detail` — the real, subprocess-executed `pytest`
         outcome for that exact task, per step (`kind`, `passed`, `detail.target`)
         (`app.mainai_execution.verify`).

    Evidence counts ONLY when ALL of:
      - a task exists whose `lessons_applied` names this EXACT lesson id (founder requirement 2:
        content/identity binding — never a target-string coincidence with a different lesson);
      - that SAME task's LATEST verification outcome (by `created_at` — a later retry supersedes
        an earlier one on the same task) is `verification_passed` (founder requirement 3: causally
        linked to the same authorized run; requirement 6: not stale/superseded — a since-regressed
        pass can never outrank the task's current, final outcome);
      - that outcome's evidence contains a `targeted_tests` step with `passed is True` and
        `detail["target"] == regression_test` — the lesson's CURRENT, live value, re-read fresh on
        every call, never a cached copy (founder requirement 5: target corresponds to the claimed
        fix; the live-value re-read is also what catches a hypothetical future content edit, since
        `EngineeringLesson` has no edit path today — see this module's own test coverage).

    Deliberately conservative, never speculative (founder requirement: "never synthesize missing
    evidence"): `False` (never raises) for no `db`, no `regression_test`, no matching task, or any
    ambiguous/malformed event detail — "no proof" is always the default, never "assume pass". No new
    table or migration; reads only `mainai_task_events`, which already exists and is already
    append-only (DB-trigger enforced, migration 0032). A caller-supplied claim can never reach this
    function at all — see `context_handoff._clamp_verification_claim` (founder requirement 7)."""
    if db is None or not regression_test:
        return False

    from sqlalchemy import select

    from app.models.mainai_execution import MainAITaskEvent, MainAITaskEventType

    lesson_id_str = str(lesson_id)
    created_rows = db.execute(
        select(MainAITaskEvent.task_id, MainAITaskEvent.detail).where(MainAITaskEvent.event_type == MainAITaskEventType.created)
    ).all()
    candidate_task_ids = [
        task_id
        for task_id, detail in created_rows
        if isinstance(detail, dict) and lesson_id_str in (detail.get("lessons_applied") or [])
    ]
    if not candidate_task_ids:
        return False

    verification_rows = (
        db.execute(
            select(MainAITaskEvent.task_id, MainAITaskEvent.event_type, MainAITaskEvent.detail)
            .where(
                MainAITaskEvent.task_id.in_(candidate_task_ids),
                MainAITaskEvent.event_type.in_(
                    [MainAITaskEventType.verification_passed, MainAITaskEventType.verification_failed]
                ),
            )
            .order_by(MainAITaskEvent.task_id, MainAITaskEvent.created_at.asc())
        )
        .all()
    )
    # Overwriting in ascending created_at order leaves each task's LATEST verification outcome.
    latest_outcome_by_task: dict = {}
    for task_id, event_type, detail in verification_rows:
        latest_outcome_by_task[task_id] = (event_type, detail)

    for event_type, detail in latest_outcome_by_task.values():
        if event_type != MainAITaskEventType.verification_passed or not isinstance(detail, dict):
            continue
        for step in detail.get("steps") or []:
            if not isinstance(step, dict):
                continue
            if (
                step.get("kind") == "targeted_tests"
                and step.get("passed") is True
                and isinstance(step.get("detail"), dict)
                and step["detail"].get("target") == regression_test
            ):
                return True
    return False


def summarize_integration_lesson(lesson, *, db: Session | None = None) -> dict:
    """Compact, honestly-labelled summary that keeps CONFIDENCE, the SELF-ASSERTED VERIFICATION
    CLAIM, EXECUTABLE-EVIDENCE state and EVIDENCE PROVENANCE as SEPARATE meanings (requirement 1):

    - `confidence` (certain/likely/uncertain) is the AUTHOR's own confidence — never, by itself,
      proof that anything was verified.
    - `verification_status` is carried VERBATIM as the lesson's stored, SELF-ASSERTED claim — it is
      surfaced honestly as a claim, not as proof. A stored `"verified_by_regression_test"` string is
      NOT, on its own, evidence a regression test executed and passed (migration 0063).
    - `verification_evidence` names whether CANONICAL EXECUTABLE evidence backs the claim:
      `"executable_regression_run"` only when `_has_executed_regression_run_evidence` finds a real,
      durably-recorded, actually-executed-and-passing regression-test run tied to THIS exact lesson
      id (via `MainAITaskEvent` — see that function), otherwise `"self_asserted"`. Without `db` (no
      session to query that evidence), always `"self_asserted"` — "cannot confirm" is never reported
      as "confirmed".
    - `verified` is the agent-facing "is this a regression-PROVEN, current fix?" — true ONLY with
      executed-run evidence AND an active status. Confidence alone never suffices; a stored
      verification string alone never suffices (requirements 2/3/5).
    - `fix_status` distinguishes a `"proposed"` fix, a `"claimed_validated_unproven"` fix (asserts
      regression validation but no executed evidence backs it), and a `"validated_by_regression_test"`
      fix (reserved for executed evidence) (requirement 4).
    - Historical/superseded lessons are never current validated guidance: `verified` additionally
      requires `status == "active"` (requirement 5).
    - `source_ref` (+ evidence) is retained so a recommendation can cite its provenance
      (requirement 6)."""
    confidence = getattr(lesson.confidence, "value", str(lesson.confidence))
    status = getattr(lesson.status, "value", str(lesson.status))
    verification_status = getattr(lesson, "verification_status", None) or "unverified"
    # The STORED claim (self-asserted) — honest input, never by itself promoted to proof.
    claims_regression_validated = verification_status == "verified_by_regression_test"
    has_executable_evidence = _has_executed_regression_run_evidence(
        db, lesson_id=lesson.id, regression_test=lesson.regression_test
    )
    verified = has_executable_evidence and status == "active"
    if verified:
        fix_status = "validated_by_regression_test"
    elif claims_regression_validated:
        fix_status = "claimed_validated_unproven"  # asserted, but no executed-run evidence backs it
    else:
        fix_status = "proposed"
    return {
        "lesson_id": str(lesson.id),
        "affected_component": lesson.affected_component,
        "symptom": _trim(lesson.problem),
        "root_cause": _trim(lesson.root_cause),
        "fix": _trim(lesson.fix),
        "fix_status": fix_status,
        "general_rule": _trim(lesson.general_rule),
        "has_regression_test": bool(lesson.regression_test),
        "regression_test": lesson.regression_test,
        "severity": getattr(lesson.severity, "value", str(lesson.severity)),
        # SEPARATE axes — never conflated:
        "confidence": confidence,  # author confidence
        "verification_status": verification_status,  # stored, self-asserted claim (verbatim)
        "verification_evidence": "executable_regression_run" if has_executable_evidence else "self_asserted",
        # Verified = backed by EXECUTED regression evidence AND still current. A stored verification
        # string or high confidence never suffices (Founder decision #380, 2026-11-10).
        "verified": verified,
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
    return [summarize_integration_lesson(lesson, db=db) for lesson in lessons]
