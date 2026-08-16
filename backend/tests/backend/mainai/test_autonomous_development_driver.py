import hashlib
import uuid
from dataclasses import replace

import pytest
from sqlalchemy import select

from app.development_driver.service import (
    DevelopmentPlan,
    DriverPlanError,
    DriverStep,
    assemble_context,
    run_driver,
    select_scoped_task,
    validate_plan,
)
from app.development_operator.service import LOCAL_EXECUTION, LOCAL_WRITE, READ_ONLY
from app.models.active_context import ActiveContextMember
from app.models.mainai_execution import MainAICheckpoint, MainAITaskStatus
from app.models.mainai_job import MainAIJobStatus
from app.models.work_intelligence import WorkTraceEvent
from app.mainai_execution.approval import ApprovalRequiredError, grant_task_approval
from tests.backend.mainai.test_development_operator import (
    _foundation as _operator_foundation,
    _git,
)


def _driver_foundation(db, tmp_path, *, approved=True):
    values = _operator_foundation(db, tmp_path, approved=approved)
    values[2].status = MainAITaskStatus.running
    db.flush()
    return values


def _plan(context, key, *steps, max_failures=2):
    return DevelopmentPlan(
        plan_id=key,
        steps=tuple(steps),
        strategy_execution_id=context.strategy_execution_id,
        max_failures=max_failures,
    )


def _read_step():
    return DriverStep(
        capability="read_file",
        purpose="inspect canonical input",
        expected_result="bounded UTF-8 content",
        arguments={"path": "safe.txt"},
    )


def test_driver_reuses_mainai_authority_and_rejects_stale_or_cross_owner(
    superuser_db, tmp_path
):
    _, _, _, job, _, context = _driver_foundation(superuser_db, tmp_path)
    plan = _plan(context, "authority", _read_step())
    validate_plan(superuser_db, context, plan)
    job.lease_generation += 1
    superuser_db.flush()
    with pytest.raises(Exception, match="stale|lease"):
        run_driver(superuser_db, context=context, plan=plan)
    with pytest.raises(Exception, match="missing|cross-owner"):
        validate_plan(superuser_db, replace(context, owner_id=uuid.uuid4()), plan)


def test_plan_is_structured_bounded_and_capability_gaps_are_durable(
    superuser_db, tmp_path
):
    _, _, _, _, _, context = _driver_foundation(superuser_db, tmp_path)
    with pytest.raises(DriverPlanError, match="raw command"):
        validate_plan(
            superuser_db,
            context,
            _plan(
                context,
                "shell",
                DriverStep(
                    capability="read_file",
                    purpose="unsafe",
                    expected_result="none",
                    arguments={"command": "sh -c anything"},
                ),
            ),
        )
    for forbidden in ("deploy", "production_mutation", "shell", "merge"):
        with pytest.raises(DriverPlanError, match="forbidden"):
            validate_plan(
                superuser_db,
                context,
                _plan(
                    context,
                    forbidden,
                    DriverStep(
                        capability=forbidden,
                        purpose="forbidden",
                        expected_result="never",
                    ),
                ),
            )
    gap = run_driver(
        superuser_db,
        context=context,
        plan=_plan(
            context,
            "gap",
            DriverStep(
                capability="inspect_image",
                purpose="inspect a visual regression",
                expected_result="visual evidence",
            ),
        ),
    )
    assert gap.classification == "CAPABILITY_MISSING"
    checkpoint = superuser_db.get(MainAICheckpoint, gap.checkpoint_id)
    assert (
        checkpoint.executor_state["driver_state"]["capability_gap"][
            "requested_capability"
        ]
        == "inspect_image"
    )


def test_bounded_context_and_scoped_selection_are_deterministic(superuser_db, tmp_path):
    _, _, task, _, _, context = _driver_foundation(superuser_db, tmp_path)
    selected, reason = select_scoped_task(
        superuser_db,
        owner_id=context.owner_id,
        goal_id=task.goal_id,
    )
    assert selected.id == task.id and reason["classification"] == "SUCCESS"
    context_set, members = assemble_context(
        superuser_db,
        owner_id=context.owner_id,
        task_id=task.id,
        max_depth=2,
        max_members=6,
    )
    assert len(members) <= 6
    assert {member.object_type for member in members} >= {"mainai_task", "mainai_goal"}
    assert all(not hasattr(member, "content") for member in members)
    assert (
        superuser_db.execute(
            select(ActiveContextMember).where(
                ActiveContextMember.context_set_id == context_set.id
            )
        )
        .scalars()
        .all()
    )


def test_action_bound_resume_is_idempotent_and_semantic_conflict_fails(
    superuser_db, tmp_path
):
    _, _, task, _, _, context = _driver_foundation(superuser_db, tmp_path)
    task.verification_plan = []
    plan = _plan(
        context,
        "resume",
        _read_step(),
        DriverStep(
            capability="repo_status",
            purpose="inspect state",
            expected_result="status",
        ),
    )
    bounded = run_driver(superuser_db, context=context, plan=plan, max_actions=1)
    assert bounded.classification == "ACTION_BOUND_REACHED"
    conflicting = _plan(
        context,
        "resume",
        DriverStep(
            capability="repo_log",
            purpose="different semantics",
            expected_result="history",
        ),
    )
    with pytest.raises(DriverPlanError, match="conflicts"):
        run_driver(superuser_db, context=context, plan=conflicting)
    completed = run_driver(superuser_db, context=context, plan=plan, max_actions=2)
    assert completed.classification == "COMPLETE"
    assert superuser_db.execute(select(WorkTraceEvent)).scalars().all().__len__() == 2


def test_provider_wait_and_cancellation_preserve_local_truth(
    superuser_db, tmp_path, monkeypatch
):
    import app.providers.registry as provider_registry

    monkeypatch.setattr(
        provider_registry,
        "get_provider",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("provider called")),
    )
    _, _, _, job, _, context = _driver_foundation(superuser_db, tmp_path)
    waiting = run_driver(
        superuser_db,
        context=context,
        plan=_plan(
            context,
            "provider-wait",
            DriverStep(
                capability="provider_step",
                purpose="specialist review of ambiguity",
                expected_result="review",
                independent_of_provider=False,
            ),
            _read_step(),
        ),
    )
    assert waiting.classification == "WAITING_PROVIDER"
    assert waiting.detail["independent_work_remains"] is False
    assert waiting.completed_steps == 1
    traces = superuser_db.execute(select(WorkTraceEvent)).scalars().all()
    assert [trace.action_detail["operator_capability"] for trace in traces] == [
        "read_file"
    ]
    job.cancel_requested = True
    superuser_db.flush()
    cancelled = run_driver(
        superuser_db,
        context=context,
        plan=_plan(context, "cancelled", _read_step()),
    )
    assert cancelled.classification == "CANCELLED"


def test_failed_verification_never_completes_task(superuser_db, tmp_path):
    _, _, task, _, _, context = _driver_foundation(superuser_db, tmp_path)
    task.verification_plan = [{"kind": "targeted_tests", "target": "missing.py"}]
    result = run_driver(
        superuser_db,
        context=context,
        plan=_plan(context, "verify-missing", _read_step()),
    )
    assert result.classification == "VERIFICATION_REQUIRED"
    assert task.status == MainAITaskStatus.running


def test_approval_review_and_retry_bounds_stop_with_exact_reason(
    superuser_db, tmp_path
):
    _, _, task, _, _, context = _driver_foundation(superuser_db, tmp_path, approved=False)
    with pytest.raises(ApprovalRequiredError):
        run_driver(
            superuser_db,
            context=context,
            plan=_plan(context, "approval", _read_step()),
        )
    grant_task_approval(superuser_db, task=task, approved_by="test")
    review = run_driver(
        superuser_db,
        context=context,
        plan=_plan(
            context,
            "review",
            DriverStep(
                "external_review",
                "determine whether invariant X holds",
                "independent finding",
                {"question": "Does invariant X hold?", "risk": "high"},
            ),
        ),
    )
    assert review.classification == "EXTERNAL_REVIEW_REQUIRED"
    checkpoint = superuser_db.get(MainAICheckpoint, review.checkpoint_id)
    assert checkpoint.executor_state["driver_state"]["unresolved"].startswith(
        "determine"
    )


def test_retry_bound_preserves_failed_attempt_and_stops(superuser_db, tmp_path):
    _, _, task, _, _, context = _driver_foundation(superuser_db, tmp_path)
    task.verification_plan = []
    plan = _plan(
        context,
        "retry-bound",
        DriverStep(
            "run_focused_test",
            "reproduce a deterministic failing test",
            "failure evidence",
            {
                "profile_name": "focused_pytest",
                "arguments": ["test_operator_sample.py::test_missing"],
            },
            LOCAL_EXECUTION,
            on_failure="retry",
        ),
        max_failures=1,
    )
    first = run_driver(superuser_db, context=context, plan=plan)
    assert first.classification == "FAILED_RETRYABLE"
    second = run_driver(superuser_db, context=context, plan=plan)
    assert second.classification == "FAILED_NONRETRYABLE"
    traces = superuser_db.execute(
        select(WorkTraceEvent).order_by(WorkTraceEvent.sequence_number)
    ).scalars()
    assert [trace.result for trace in traces] == ["failed", "failed"]


def test_end_to_end_provider_independent_development_task(superuser_db, tmp_path):
    _, _, task, job, worktree, context = _driver_foundation(superuser_db, tmp_path)
    context = replace(
        context,
        allowed_paths=("calc.py", "test_calc.py"),
    )
    calc = "def add(left, right):\n    return left + right\n"
    test = "from calc import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    plan = _plan(
        context,
        "e2e-safe-change",
        DriverStep(
            "create_file",
            "add deterministic function",
            "new source hash",
            {"path": "calc.py", "content": calc, "expected_sha256": None},
            LOCAL_WRITE,
        ),
        DriverStep(
            "create_file",
            "add focused regression test",
            "new test hash",
            {"path": "test_calc.py", "content": test, "expected_sha256": None},
            LOCAL_WRITE,
        ),
        DriverStep(
            "run_focused_test",
            "prove deterministic behavior",
            "pytest exit zero",
            {"profile_name": "focused_pytest", "arguments": ["test_calc.py"]},
            LOCAL_EXECUTION,
            verification_required=True,
        ),
        DriverStep(
            "verification_evaluate",
            "evaluate required operator evidence",
            "verification checkpoint",
            required_risk=READ_ONLY,
        ),
        DriverStep(
            "stage_scoped_changes",
            "stage only authorized files",
            "scoped index",
            {"paths": ["calc.py", "test_calc.py"]},
            LOCAL_WRITE,
        ),
        DriverStep(
            "commit_scoped_changes",
            "commit verified change",
            "exact commit SHA",
            {"message": "Add deterministic addition function"},
            LOCAL_WRITE,
        ),
    )
    result = run_driver(superuser_db, context=context, plan=plan, max_actions=10)
    assert result.classification == "COMPLETE"
    assert task.status == MainAITaskStatus.completed
    assert job.status == MainAIJobStatus.completed
    assert worktree.current_commit == _git(context.repository_root, "rev-parse", "HEAD")
    assert hashlib.sha256(
        (context.repository_root / "calc.py").read_bytes()
    ).hexdigest()
    capabilities = [
        event.action_detail["operator_capability"]
        for event in superuser_db.execute(
            select(WorkTraceEvent).order_by(WorkTraceEvent.sequence_number)
        ).scalars()
    ]
    assert capabilities == [
        "create_file",
        "create_file",
        "run_focused_test",
        "stage_scoped_changes",
        "commit_scoped_changes",
    ]


# --- P1 approval-default regression coverage (founder review finding) -------------------
#
# The autonomous chain (development_supervisor / safe_planner) reaches this driver without a
# human reviewing each task, unlike the ordinary founder-facing executor. Before this fix, a
# goal that forgot to set approval_policy would silently inherit "standard_repo_work"'s AUTO
# default for repo_edit -- these tests prove that gap is closed at both of its two layers:
# (1) the driver refuses to run at all under any policy but "autonomous_development_work",
# and (2) even under that policy, an individual write/commit still requires a real
# approval_granted event, exactly like founder-facing work does.


def test_autonomous_write_denied_when_goal_does_not_use_autonomous_policy(
    superuser_db, tmp_path
):
    _, goal, task, _, _, context = _driver_foundation(superuser_db, tmp_path)
    assert goal.approval_policy == "autonomous_development_work"
    goal.approval_policy = "standard_repo_work"
    superuser_db.flush()
    with pytest.raises(DriverPlanError, match="autonomous_development_work"):
        validate_plan(
            superuser_db,
            context,
            _plan(
                context,
                "unscoped-write",
                DriverStep(
                    "create_file",
                    "add a file",
                    "new source hash",
                    {"path": "unauthorized.py", "content": "x = 1\n", "expected_sha256": None},
                    LOCAL_WRITE,
                ),
            ),
        )
    assert task.status == MainAITaskStatus.running


def test_autonomous_commit_denied_without_task_approval(superuser_db, tmp_path):
    _, goal, task, _, _, context = _driver_foundation(superuser_db, tmp_path, approved=False)
    assert goal.approval_policy == "autonomous_development_work"
    with pytest.raises(ApprovalRequiredError):
        run_driver(
            superuser_db,
            context=context,
            plan=_plan(
                context,
                "unapproved-commit",
                DriverStep(
                    "stage_scoped_changes",
                    "stage authorized files",
                    "scoped index",
                    {"paths": ["safe.txt"]},
                    LOCAL_WRITE,
                ),
                DriverStep(
                    "commit_scoped_changes",
                    "commit without approval",
                    "exact commit SHA",
                    {"message": "should be denied"},
                    LOCAL_WRITE,
                ),
            ),
        )


def test_approved_autonomous_write_and_commit_still_functions(superuser_db, tmp_path):
    _, goal, task, job, worktree, context = _driver_foundation(superuser_db, tmp_path)
    assert goal.approval_policy == "autonomous_development_work"
    task.verification_plan = []
    superuser_db.flush()
    context = replace(context, allowed_paths=("approved.py", "test_approved.py"))
    approved = "def add(left, right):\n    return left + right\n"
    approved_test = "from approved import add\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    plan = _plan(
        context,
        "approved-write-and-commit",
        DriverStep(
            "create_file",
            "add an authorized file",
            "new source hash",
            {"path": "approved.py", "content": approved, "expected_sha256": None},
            LOCAL_WRITE,
        ),
        DriverStep(
            "create_file",
            "add a focused regression test",
            "new test hash",
            {"path": "test_approved.py", "content": approved_test, "expected_sha256": None},
            LOCAL_WRITE,
        ),
        DriverStep(
            "run_focused_test",
            "prove the authorized change is correct",
            "pytest exit zero",
            {"profile_name": "focused_pytest", "arguments": ["test_approved.py"]},
            LOCAL_EXECUTION,
            verification_required=True,
        ),
        DriverStep(
            "verification_evaluate",
            "evaluate required operator evidence",
            "verification checkpoint",
            required_risk=READ_ONLY,
        ),
        DriverStep(
            "stage_scoped_changes",
            "stage authorized files",
            "scoped index",
            {"paths": ["approved.py", "test_approved.py"]},
            LOCAL_WRITE,
        ),
        DriverStep(
            "commit_scoped_changes",
            "commit approved change",
            "exact commit SHA",
            {"message": "Add approved file"},
            LOCAL_WRITE,
        ),
    )
    result = run_driver(superuser_db, context=context, plan=plan, max_actions=10)
    assert result.classification == "COMPLETE"
    assert task.status == MainAITaskStatus.completed
    assert job.status == MainAIJobStatus.completed
    assert worktree.current_commit == _git(context.repository_root, "rev-parse", "HEAD")
