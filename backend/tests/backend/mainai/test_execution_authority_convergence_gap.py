"""Execution-architecture convergence evidence (founder directive, Priority 1, 2026-11-15).

Proves, with real executable regression tests against real Postgres (not a design claim),
the exact asymmetry docs/LIFE_THREE_EXECUTION_SYSTEMS_BOUNDARY_MAP.md and
docs/BRANCH_REGISTRY.md's convergence-recommendation section describe between the two
systems that both hold a `task_id`-shaped link to a real `MainAITask`:

- `app.agent_coordination.dispatch.evaluate_dispatch_readiness()` delegates ENTIRELY to
  `app.mainai_execution.approval.require_task_approval()` for any assignment with a linked
  `task_id` -- the SAME MainAITask's approval state changes its outcome
  (OUTCOME_APPROVAL_REQUIRED -> OUTCOME_ASSIGNABLE once approved).
- `app.workforce.provider_worker.execute_workforce_assignment()` NEVER calls
  `require_task_approval()` anywhere on its `activate_provider=True` path -- the SAME
  MainAITask's approval state has ZERO observable effect on the outcome (both before and
  after approval, the call raises the identical `ProviderActivationBlocked`, for the
  identical reason, because of an unconditional `raise` that already blocks real invocation
  regardless of approval).

This file makes NO production-code change, activates NOTHING, and does not touch the
unconditional `ProviderActivationBlocked` staged-raise -- it only proves, executably, the
gap the convergence recommendation describes, so the recommendation is evidence-based rather
than asserted. `first_cycle_go` and the global activation gates are exercised only through
existing test-only reset/record helpers already used elsewhere in this suite
(`reset_activation_gates_for_tests`, `record_gate_verification`) -- the same pattern
test_workforce_activation_prep.py already relies on.

ON THIS BRANCH ONLY (`claude/workforce-task-approval-enforcement`, stacked on this PR): the
workforce side of the gap described above has since been closed by this same branch's own
`require_live_task_authorization_for_workforce_dispatch()` fix. The workforce test below was
updated here (not on the frozen PR this file originated from) to assert the CLOSED gap
instead of leaving a now-false assertion that approval has zero effect.
"""

from __future__ import annotations

import uuid

import pytest

from app.agent_coordination.dispatch import (
    OUTCOME_APPROVAL_REQUIRED,
    evaluate_dispatch_readiness,
)
from app.agent_coordination.service import (
    OUTCOME_ASSIGNABLE,
    acquire_lease,
    create_work_assignment,
    register_agent,
    transition_status,
)
from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask
from app.workforce import (
    REQUIRED_ACTIVATION_GATES,
    GateStatus,
    ProviderActivationBlocked,
    execute_workforce_assignment,
    record_gate_verification,
    register_workforce_agent,
    reset_activation_gates_for_tests,
    resolve_delegation,
    submit_delegation_request,
)


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as app.agent_coordination's own dispatch tests."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, approval_policy="autonomous_development_work"):
    """autonomous_development_work requires approval for repo_edit -- same choice
    test_agent_dispatch_foundation.py makes, for the same reason: proves real delegation to
    require_task_approval(), not a reimplementation of its policy logic."""
    goal = create_goal(db, owner_id=owner_id, title="Convergence-gap test", original_instruction="x", created_by="founder", approval_policy=approval_policy)
    plan = create_plan(db, goal=goal, rationale="convergence-gap test", tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")], created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).first()
    return goal, task


def test_agent_coordination_dispatch_outcome_tracks_the_same_tasks_approval_state(superuser_db, owner_id):
    """Control case: the system that already delegates to the real gate."""
    goal, task = _goal_plan_task(superuser_db, owner_id)
    cursor = register_agent(
        superuser_db, agent_key=f"cursor-{uuid.uuid4().hex[:8]}", display_name="cursor", adapter_kind="cli",
        execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3,
    )
    assignment = create_work_assignment(
        superuser_db, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=cursor.id, role="builder",
        read_write_mode="read_write", repository_identity="lifeai", allowed_paths=["backend/app/**"], requested_by="test",
    )
    acquire_lease(superuser_db, assignment=assignment, agent_id=cursor.id, branch="cursor/convergence-gap", worktree_path="/tmp/wt-convergence-gap", allowed_paths=["backend/app/**"], mode="read_write")
    transition_status(superuser_db, assignment=assignment, new_status="running")

    before = evaluate_dispatch_readiness(superuser_db, assignment=assignment, agent=cursor)
    assert before.outcome == OUTCOME_APPROVAL_REQUIRED

    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()

    after = evaluate_dispatch_readiness(superuser_db, assignment=assignment, agent=cursor)
    assert after.outcome == OUTCOME_ASSIGNABLE


def test_workforce_dispatch_outcome_now_tracks_the_same_tasks_approval_state(superuser_db, owner_id):
    """This test originally proved the gap this same PR's own
    require_live_task_authorization_for_workforce_dispatch() fix (app/workforce/
    provider_worker.py) closes: before the fix, the SAME MainAITask (approval-required,
    then approved) produced the SAME outcome either way on workforce's activate_provider
    path. With the fix applied, it no longer does -- updated here to assert the CLOSED gap
    rather than leave a now-false assertion in place. The fuller regression suite for this
    fix lives in tests/backend/mainai/test_workforce_task_approval_enforcement.py; this test
    stays as a direct, minimal regression guard against the exact gap it originally
    documented ever reopening."""
    goal, task = _goal_plan_task(superuser_db, owner_id)

    register_workforce_agent(
        superuser_db, owner_id=owner_id, agent_key=f"b-{uuid.uuid4().hex[:8]}", name="B", role="builder",
        agent_type="CODING", capability_tags=["low_risk_classification"], status="active",
        trust_zone="LOCAL_INTERNAL", allowed_tool_classes=["read_excerpt"],
    )
    verifier = register_workforce_agent(
        superuser_db, owner_id=owner_id, agent_key=f"v-{uuid.uuid4().hex[:8]}", name="V", role="verifier",
        agent_type="VERIFIER", capability_tags=["verification"], status="active", trust_zone="LOCAL_INTERNAL",
    )
    request = submit_delegation_request(
        superuser_db, owner_id=owner_id, goal_text="x", required_capability="low_risk_classification",
        goal_id=goal.id, task_id=task.id, verification_requirement="independent_verifier",
    )
    assignment = resolve_delegation(superuser_db, owner_id=owner_id, request=request, verifier_profile_id=verifier.id)

    reset_activation_gates_for_tests()
    for key in REQUIRED_ACTIVATION_GATES:
        record_gate_verification(key, status=GateStatus.verified, evidence_ref=f"test:{key}")

    def _invoke():
        with pytest.raises(ProviderActivationBlocked) as exc_info:
            execute_workforce_assignment(
                superuser_db, owner_id=owner_id, assignment=assignment, goal_text="x",
                capability="low_risk_classification", activate_provider=True,
            )
        return str(exc_info.value)

    before_message = _invoke()
    # Pre-fix, this asserted "first real provider invoke remains staged" -- the gap. Now the
    # new per-task gate fires FIRST, with a distinguishable reason, before the task is
    # approved.
    assert "requires founder approval" in before_message

    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()

    after_message = _invoke()
    # Approving the task now changes the outcome: the per-task gate passes, and the call
    # reaches the SAME pre-existing, untouched staged-raise every other legitimate
    # activate_provider=True call already hits -- dormant execution paths stay disabled.
    assert "first real provider invoke remains staged" in after_message
    assert after_message != before_message

    reset_activation_gates_for_tests()
