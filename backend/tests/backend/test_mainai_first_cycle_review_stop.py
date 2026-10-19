"""Independent review receipts, STOP-before-merge, and no-paid-replan on external CI."""

from __future__ import annotations

import inspect
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.mainai_execution.first_cycle_stop import (
    STATE_EXTERNAL_CI_FAILED,
    STATE_INDEPENDENT_REVIEW_BLOCKED,
    STATE_STOP_BEFORE_MERGE,
    LOCAL_EDITABLE_TASK_TYPES,
    fail_closed_external_ci,
    should_suppress_paid_replan,
    stop_first_cycle,
)
from app.mainai_execution.replan import ReplanSuppressedError, find_replan_trigger, trigger_replan
from app.mainai_execution.review_receipt import (
    FIRST_CYCLE_REVIEW_COMPONENT,
    PATH_A_BUILDER_IDENTITY,
    ReviewReceiptError,
    envelope_authorization_hash,
    evaluate_first_cycle_review_receipt,
    instruction_hash,
    output_hash,
    record_first_cycle_review_receipt,
)
from app.mainai_execution import planner
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_verification_registry.service import record_verification_attestation
from app.models.mainai_execution import MainAIGoalStatus, MainAITask, MainAITaskStatus
from app.request_context import current_user_id as current_user_id_var

HEAD = "a" * 40
BASE = "b" * 40
EVIDENCE = "c" * 40
OTHER = "d" * 40
REPO = "d1n095/LifeAI"


@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


def _goal_with_task(db, owner_id, *, task_type="open_pr"):
    goal = planner.create_goal(db, owner_id=owner_id, title="fc", original_instruction="document greet", created_by="founder")
    planner.create_plan(
        db,
        goal=goal,
        rationale="fc",
        tasks=[PlannedTaskSpec(description="t", task_type=task_type, target_files=["backend/app/greeting.py"])],
        created_by="test",
    )
    db.commit()
    task = db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    return goal, task


def _envelope_ctx():
    return {
        "envelope_id": "11111111-1111-4111-8111-111111111111",
        "authorized_paths": ["backend/app/greeting.py"],
        "authorized_capabilities": ["path_a", "open_pr"],
        "authorized_by": "founder",
        "repository_identity": REPO,
    }


def _record(superuser_db, owner_id, **overrides):
    payload = dict(
        repository=REPO,
        pr_number=7,
        head_sha=HEAD,
        approved_base=BASE,
        builder_identity=PATH_A_BUILDER_IDENTITY,
        reviewer_identity="founder-reviewer",
        review_result="PASS",
        evidence_sha=EVIDENCE,
        instruction_hash_value=instruction_hash("document greet"),
        output_hash_value=output_hash(repository=REPO, pr_number=7, head_sha=HEAD),
        envelope_context=_envelope_ctx(),
        evidence_summary="Founder independent review of exact SHA",
        owner_id=owner_id,
        examiner_class="founder",
    )
    payload.update(overrides)
    record = record_first_cycle_review_receipt(superuser_db, **payload)
    superuser_db.commit()
    return record


def _eval(db, owner_id, **overrides):
    payload = dict(
        repository=REPO,
        pr_number=7,
        head_sha=HEAD,
        approved_base=BASE,
        builder_identity=PATH_A_BUILDER_IDENTITY,
        instruction_hash_value=instruction_hash("document greet"),
        output_hash_value=output_hash(repository=REPO, pr_number=7, head_sha=HEAD),
        envelope_context=_envelope_ctx(),
        owner_id=owner_id,
    )
    payload.update(overrides)
    return evaluate_first_cycle_review_receipt(db, **payload)


def test_missing_review_blocks_completion(db_session, owner_id):
    review = _eval(db_session, owner_id)
    assert review.ok is False
    assert review.merged is False
    assert review.first_cycle_state == STATE_INDEPENDENT_REVIEW_BLOCKED
    assert "no review receipt" in review.reason


def test_negative_review_blocks_completion(db_session, superuser_db, owner_id):
    _record(superuser_db, owner_id, review_result="FAIL", evidence_summary="changes requested")
    review = _eval(db_session, owner_id)
    assert review.ok is False
    assert review.merged is False
    assert "FAIL" in review.reason


def test_inconclusive_blocked_verdict_blocks_completion(db_session, superuser_db, owner_id):
    _record(superuser_db, owner_id, review_result="BLOCKED", evidence_summary="inconclusive")
    review = _eval(db_session, owner_id)
    assert review.ok is False
    assert "BLOCKED" in review.reason


def test_stale_and_changed_pr_head_invalidates_receipt(db_session, superuser_db, owner_id):
    _record(superuser_db, owner_id)
    stale = _eval(db_session, owner_id, head_sha=OTHER, output_hash_value=output_hash(repository=REPO, pr_number=7, head_sha=OTHER))
    assert stale.ok is False
    assert stale.first_cycle_state == STATE_INDEPENDENT_REVIEW_BLOCKED


def test_reviewer_builder_identity_collision_cannot_be_recorded(superuser_db, owner_id):
    with pytest.raises(ReviewReceiptError):
        _record(superuser_db, owner_id, reviewer_identity=PATH_A_BUILDER_IDENTITY)


def test_forged_receipt_hashes_are_rejected(db_session, superuser_db, owner_id):
    record_verification_attestation(
        superuser_db,
        owner_id=owner_id,
        component_id=FIRST_CYCLE_REVIEW_COMPONENT,
        candidate_id=f"github:{REPO}#7",
        candidate_sha=HEAD,
        candidate_tree_identity=BASE,
        builder_identity=PATH_A_BUILDER_IDENTITY,
        examiner_identity="forger",
        review_result="PASS",
        evidence_summary="forged hashes",
        verification_scope={
            "repository": REPO,
            "pr_number": 7,
            "approved_base": BASE,
            "evidence_sha": EVIDENCE,
            "instruction_hash": "0" * 64,
            "output_hash": "1" * 64,
            "envelope": _envelope_ctx(),
            "reviewer_authority": [],
        },
    )
    superuser_db.commit()
    review = _eval(db_session, owner_id)
    assert review.ok is False
    assert "instruction_hash" in review.reason or "forged" in review.reason or "output_hash" in review.reason


def test_positive_review_is_stop_before_merge_not_merged(db_session, superuser_db, owner_id):
    _record(superuser_db, owner_id)
    review = _eval(db_session, owner_id)
    assert review.ok is True
    assert review.merged is False
    assert review.first_cycle_state == STATE_STOP_BEFORE_MERGE
    assert review.truth["merged"] is False
    assert review.truth["awaiting_founder_merge"] is True
    assert review.truth["subsequent_autonomous_job"] is False
    assert review.truth["draft_pr_is_not_merged"] is True


def test_runtime_cannot_insert_review_receipt(db_session, owner_id):
    with pytest.raises(Exception):
        db_session.execute(
            sa_text(
                """
                INSERT INTO mainai_verification_records(
                    component_id, candidate_id, candidate_sha, builder_identity, examiner_identity,
                    review_result, evidence_summary
                ) VALUES (
                    :c, :cid, :sha, 'path_a.builder', 'founder-reviewer', 'PASS', 'runtime forged pass'
                )
                """
            ),
            {"c": FIRST_CYCLE_REVIEW_COMPONENT, "cid": f"github:{REPO}#7", "sha": HEAD},
        )
        db_session.commit()
    db_session.rollback()


def test_stop_before_merge_blocks_goal_and_never_says_merged(db_session, owner_id):
    goal, task = _goal_with_task(db_session, owner_id)
    payload = stop_first_cycle(
        db_session,
        task,
        reason="independent review recorded; STOP before merge",
        first_cycle_state=STATE_STOP_BEFORE_MERGE,
        evidence={"ci_passed": True, "review_ok": True, "draft_pr": True},
        goal=goal,
    )
    db_session.commit()
    assert task.status == MainAITaskStatus.blocked
    assert goal.status == MainAIGoalStatus.blocked
    assert payload["merged"] is False
    assert '"merged": false' in (goal.final_outcome or "").lower() or "stop_before_merge" in (goal.final_outcome or "")
    assert find_replan_trigger(db_session, goal=goal) is None


@pytest.mark.asyncio
async def test_external_ci_failure_does_not_trigger_paid_replan(db_session, owner_id):
    goal, task = _goal_with_task(db_session, owner_id, task_type="open_pr")
    task.status = MainAITaskStatus.failed
    task.completed_at = datetime.utcnow()
    db_session.commit()
    assert should_suppress_paid_replan(task) is True
    assert find_replan_trigger(db_session, goal=goal) is None
    with pytest.raises(ReplanSuppressedError):
        await trigger_replan(db_session, goal=goal, failed_task=task, dispatched_by="test")


def test_local_editable_failure_still_eligible_for_replan(db_session, owner_id):
    goal, task = _goal_with_task(db_session, owner_id, task_type="repo_edit")
    task.status = MainAITaskStatus.failed
    task.completed_at = datetime.utcnow()
    db_session.commit()
    assert task.task_type in LOCAL_EDITABLE_TASK_TYPES
    assert should_suppress_paid_replan(task) is False
    assert find_replan_trigger(db_session, goal=goal) == task


def test_fail_closed_external_ci_is_not_retryable(db_session, owner_id):
    goal, task = _goal_with_task(db_session, owner_id)
    fail_closed_external_ci(
        db_session,
        task,
        reason="external CI failed; STOP without paid replan",
        first_cycle_state=STATE_EXTERNAL_CI_FAILED,
        evidence={"ci_passed": False},
        goal=goal,
    )
    db_session.commit()
    assert task.status == MainAITaskStatus.failed
    assert task.next_retry_at is None
    assert find_replan_trigger(db_session, goal=goal) is None


def test_review_module_has_no_push_or_merge_authority():
    from app.mainai_execution import review_receipt
    from app.integrations import github_client as github_client_mod
    import ast
    from pathlib import Path

    src = inspect.getsource(review_receipt)
    assert "merge_pull_request" not in src
    assert "push_worktree_branch" not in src
    assert "Track B" in src or "track_b" in src
    tree = ast.parse(Path(github_client_mod.__file__).read_text(encoding="utf-8"))
    methods = {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "GitHubClient"
        for node in node.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    assert "merge" not in methods
    assert "merge_pull_request" not in methods


def test_hashes_bind_repo_pr_and_envelope():
    assert output_hash(repository=REPO, pr_number=7, head_sha=HEAD) != output_hash(
        repository=REPO, pr_number=8, head_sha=HEAD
    )
    assert envelope_authorization_hash(_envelope_ctx()) != envelope_authorization_hash({})
    assert len(instruction_hash("document greet")) == 64
