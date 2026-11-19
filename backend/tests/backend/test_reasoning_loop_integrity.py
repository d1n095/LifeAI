"""M1 tests (Round 9): LOOP INTEGRITY of the self-correction system.

Round 8 closed the loop measured-outcomes -> planning order. These tests prove the loop
cannot corrupt itself, that its claims stay observational, and that the planner feedback
surface treats unverified lessons as hypotheses — never established rules:

  1. READ-ONLY selection: a selection pass (and the controlled comparison over both
     paths) creates ZERO rows — standing changes only through real guard observations,
     so the ranking cannot reinforce itself.
  2. Controlled behavioral comparison: the ranked path applies a SUBSET of the legacy
     path on the same inputs, and the delta is EXACTLY the evidence-backed exclusions —
     rank-based expansion is forbidden.
  3. The standing-effectiveness aggregate buckets guard verdicts by ranked class —
     an observational review signal, documented as never causal.
  4. Clusters now carry root causes, distinct guard targets (the measurable corrective
     instruments), and a deterministic corrective action with real counts.
  5. The chat prompt carries the LÄRANDE block: aggregate standings + clusters, with the
     rule that untested/flagged rules are HYPOTHESES — and the block CHANGES when a
     cluster forms (executable invocation proof, not an import proof).
"""

import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.db import migration_engine
from app.mainai_execution import planner
from app.mainai_execution.lesson_guard_observations import recurring_failure_clusters
from app.mainai_execution.lesson_selection import (
    evaluate_selection_behavior,
    format_lesson_feedback_instructions,
    lesson_standing_effectiveness,
    select_lessons_for_planning,
)
from app.models.mainai_execution import EngineeringLessonSeverity
from app.request_context import current_user_id as current_user_id_var
from app.rls import apply_mainai_execution_privileges
from tests.backend.test_claim_graph_integration import _login

SAFE_TARGET_A = "tests/backend/test_mainai_execution_planner.py::test_get_goal_raises_for_unknown_id"
SAFE_TARGET_B = "tests/backend/test_mainai_execution_planner.py::test_create_goal_starts_pending_with_no_plan"


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _lesson(db, *, tags=("run_tests",), target=SAFE_TARGET_A, root_cause="seeded root cause"):
    from app.mainai_execution.lessons import record_lesson

    return record_lesson(
        db,
        problem=f"R9 loop {uuid.uuid4()}",
        root_cause=root_cause,
        affected_component=f"loop_component_{uuid.uuid4().hex[:8]}",
        severity=EngineeringLessonSeverity.low,
        evidence="loop integrity test",
        fix="guard",
        general_rule="rule",
        applies_to=list(tags),
        source_type="loop_tests",
        source_ref=f"loop-{uuid.uuid4()}",
        created_by="test",
        first_seen_at=datetime.utcnow(),
        regression_test=target,
    )


def _observe(db, owner_id, task, lesson, outcome):
    from app.models.lesson_guard_observations import EngineeringLessonGuardObservation

    db.add(
        EngineeringLessonGuardObservation(
            owner_id=owner_id, lesson_id=lesson.id, task_id=task.id, goal_id=task.goal_id,
            outcome=outcome, evidence_strength="direct", relevance_reason="loop integrity test",
            guard_target=lesson.regression_test, evidence={}, source_ref=f"loop:{uuid.uuid4()}",
        )
    )
    db.commit()


def _plan_one_task(db, owner_id):
    goal = planner.create_goal(
        db, owner_id=owner_id, title=f"Loop goal {uuid.uuid4()}", original_instruction="Do it.", created_by="test"
    )
    plan = planner.create_plan(
        db, goal=goal, rationale="loop test",
        tasks=[planner.PlannedTaskSpec(description="Run", task_type="run_tests")],
        created_by="test",
    )
    db.commit()
    return db.query(planner.MainAITask).filter(planner.MainAITask.plan_id == plan.id).one()


def _table_counts(db):
    obs = db.execute(sa_text("SELECT count(*) FROM engineering_lesson_guard_observations")).scalar()
    dec = db.execute(sa_text("SELECT count(*) FROM engineering_lesson_plan_decisions")).scalar()
    return int(obs), int(dec)


# --- 1. selection writes NOTHING ----------------------------------------------------------------


def test_selection_and_comparison_are_read_only(db_session, owner_id):
    lesson = _lesson(db_session)
    task = _plan_one_task(db_session, owner_id)  # lesson applied -> decision row written by the planner
    _observe(db_session, owner_id, task, lesson, "guard_failed")

    before = _table_counts(db_session)
    select_lessons_for_planning(db_session, owner_id=owner_id, task_type="run_tests")
    evaluate_selection_behavior(
        db_session, owner_id=owner_id, task_type="run_tests", verification_plan=[]
    )
    lesson_standing_effectiveness(db_session, owner_id=owner_id)
    after = _table_counts(db_session)
    assert before == after, (
        f"selection/comparison/evaluation created rows ({before} -> {after}) — a ranking that "
        "writes its own evidence can reinforce itself; this one structurally cannot"
    )


# --- 2. the controlled comparison ---------------------------------------------------------------


def test_ranked_path_is_a_subset_and_the_delta_is_exactly_the_exclusions(db_session, owner_id):
    # DISTINCT targets per lesson: with shared targets the apply-time dedup makes whichever
    # lesson runs FIRST claim the target — a dedup-order artifact, not ranking semantics.
    # The subset invariant is about exclusions, so every candidate gets its own target.
    target_c = "tests/backend/test_mainai_execution_planner.py::test_create_plan_persists_tasks_and_marks_dependency_free_tasks_ready"
    target_d = "tests/backend/test_mainai_execution_planner.py::test_create_plan_rejects_empty_task_list"
    unproven = _lesson(db_session, target=SAFE_TARGET_A)
    proven = _lesson(db_session, target=SAFE_TARGET_B, root_cause="proven rule")
    task = _plan_one_task(db_session, owner_id)  # both applied -> decision rows
    _observe(db_session, owner_id, task, proven, "guard_held")

    # One failed-majority (2 fails, 2 distinct tasks) and one unusable guard.
    failing = _lesson(db_session, target=target_c, root_cause="failing rule")
    # Both failure observations must land on tasks the lesson actually SHAPED (decision
    # rows exist) — an observation on task `task` would not count: that plan predates the
    # lesson, which is exactly the provenance contract the rollup enforces.
    task2 = _plan_one_task(db_session, owner_id)
    task3 = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task2, failing, "guard_failed")
    _observe(db_session, owner_id, task3, failing, "guard_failed")
    unusable = _lesson(db_session, target=target_d, root_cause="unusable rule")
    task4 = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task4, unusable, "guard_unusable")

    comparison = evaluate_selection_behavior(
        db_session, owner_id=owner_id, task_type="run_tests", verification_plan=[]
    )
    assert comparison.subset_holds, "the ranked path never applies MORE than the legacy path"
    assert comparison.exclusions_explain_delta, "every delta lesson is an evidence-backed exclusion"
    assert set(comparison.excluded_by_ranking) == {failing.id, unusable.id}
    assert {unproven.id, proven.id} <= set(comparison.ranked_applied), (
        "unproven and proven lessons still apply — the ranking only removes evidence-backed failures"
    )


# --- 3. the observational effectiveness aggregate -----------------------------------------------


def test_standing_effectiveness_buckets_verdicts_by_rank_class(db_session, owner_id):
    proven = _lesson(db_session, target=SAFE_TARGET_A)
    flagged = _lesson(db_session, target=SAFE_TARGET_B, root_cause="flagged rule")
    task = _plan_one_task(db_session, owner_id)
    task2 = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task, proven, "guard_held")
    _observe(db_session, owner_id, task, flagged, "guard_failed")

    buckets = lesson_standing_effectiveness(db_session, owner_id=owner_id)
    assert buckets.get("proven_ranked", {}).get("held", 0) >= 1
    assert buckets.get("flagged_ranked", {}).get("failed", 0) >= 1
    assert all(set(v) == {"held", "failed", "lessons"} for v in buckets.values()), (
        "the aggregate is counts only — no causal attribution is manufactured"
    )


# --- 4. cluster -> root cause -> measurable corrective action -----------------------------------


def test_cluster_carries_root_causes_guard_targets_and_action(db_session, owner_id):
    shared = f"shared_{uuid.uuid4().hex[:8]}"
    a = _lesson(db_session, target=SAFE_TARGET_A, root_cause="missing transaction boundary")
    b = _lesson(db_session, target=SAFE_TARGET_B, root_cause="missing transaction boundary")
    a.affected_component = shared
    b.affected_component = shared
    db_session.commit()
    task1 = _plan_one_task(db_session, owner_id)
    task2 = _plan_one_task(db_session, owner_id)
    _observe(db_session, owner_id, task1, a, "guard_failed")
    _observe(db_session, owner_id, task2, b, "guard_failed")

    clusters = recurring_failure_clusters(db_session, owner_id=owner_id)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster["root_causes"] == ["missing transaction boundary"], "the shared root cause is named"
    assert set(cluster["guard_targets"]) == {SAFE_TARGET_A, SAFE_TARGET_B}, (
        "the cluster's measurable corrective instruments are its distinct running guards"
    )
    assert "review component" in cluster["corrective_action"]
    assert "2 failed tasks" in cluster["corrective_action"]
    assert "2 distinct" in cluster["corrective_action"]

    feedback = format_lesson_feedback_instructions(db_session, owner_id=owner_id)
    assert shared in feedback and "rotorsaker" in feedback, "the cluster travels to the feedback surface"


# --- 5. the prompt-level feedback surface (executable invocation proof) --------------------------


def test_chat_prompt_carries_lesson_feedback_and_changes_with_a_cluster(client, monkeypatch, db_session, make_verified_user):
    from app.models.user import User
    from app.providers.openai_provider import OpenAIProvider
    from app.providers.base import ChatResult

    captured: dict = {}

    async def _embed(self, texts, model, **kwargs):
        return [[0.1] * get_dim() for _ in texts]

    async def _chat(self, messages, model, **kwargs):
        captured["system"] = messages[0].content
        return ChatResult(content="Svar.", provider="openai", model=model, raw_usage={"prompt_tokens": 5, "completion_tokens": 3})

    def get_dim():
        from app.config import get_settings

        return get_settings().embedding_dim

    monkeypatch.setattr(OpenAIProvider, "embed", _embed)
    monkeypatch.setattr(OpenAIProvider, "chat", _chat)

    founder = db_session.query(User).filter(User.email == "founder@lifeos.local").first()
    _set_rls_user(db_session, founder.id)

    csrf = _login(client)
    client.post("/api/chat", json={"conversation_id": None, "message": "Hur står det till?"}, headers={"X-CSRF-Token": csrf})
    assert "LÄRANDE-status" in captured["system"], "the feedback block is IN the prompt, not merely importable"
    assert "HYPOTES" in captured["system"], "unverified lessons are named hypotheses, never established rules"
    assert "inga återkommande felkluster" in captured["system"]

    # A cluster forms -> the SAME surface changes: the component, root cause, and action appear.
    shared = f"prompt_cluster_{uuid.uuid4().hex[:6]}"
    from app.mainai_execution.lessons import record_lesson

    a = record_lesson(
        db_session, problem="p", root_cause="bad seed data", affected_component=shared,
        severity=EngineeringLessonSeverity.low, evidence="e", fix="f", general_rule="r",
        applies_to=["run_tests"], source_type="loop_tests", source_ref=f"loop-{uuid.uuid4()}",
        created_by="test", first_seen_at=datetime.utcnow(), regression_test=SAFE_TARGET_A,
    )
    b = record_lesson(
        db_session, problem="p", root_cause="bad seed data", affected_component=shared,
        severity=EngineeringLessonSeverity.low, evidence="e", fix="f", general_rule="r",
        applies_to=["run_tests"], source_type="loop_tests", source_ref=f"loop-{uuid.uuid4()}",
        created_by="test", first_seen_at=datetime.utcnow(), regression_test=SAFE_TARGET_B,
    )
    db_session.commit()
    task1 = _plan_one_task(db_session, founder.id)
    task2 = _plan_one_task(db_session, founder.id)
    _observe(db_session, founder.id, task1, a, "guard_failed")
    _observe(db_session, founder.id, task2, b, "guard_failed")

    client.post("/api/chat", json={"conversation_id": None, "message": "Hur står det till nu?"}, headers={"X-CSRF-Token": csrf})
    assert shared in captured["system"]
    assert "bad seed data" in captured["system"]
    assert "åtgärd:" in captured["system"], "the corrective action is IN the prompt the model actually sees"
