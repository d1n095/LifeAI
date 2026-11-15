"""Workforce task-approval enforcement (founder directive, 2026-11-15, "Execution Authority,
Integration and Complete Project Knowledge", Priority 1) -- the narrowly-scoped, isolated
remediation for the gap `test_execution_authority_convergence_gap.py` proved executably.

`app.workforce.provider_worker.require_live_task_authorization_for_workforce_dispatch()`
reuses `app.mainai_execution.approval.require_task_approval()` -- the SAME real gate
`app.agent_coordination.dispatch` already delegates to -- never a second approval engine.
`app.workforce.broker.resolve_delegation()` now surfaces `request.task_id` onto the created
assignment's own `provenance` JSON field (no migration: three open PRs already independently
claim revision 0069, see docs/BRANCH_REGISTRY.md -- a new column would be a fourth claimant).

Every test here calls `execute_workforce_assignment(..., activate_provider=True)` for real --
the pre-existing, UNTOUCHED unconditional `ProviderActivationBlocked` staged-raise still
fires on every genuinely legitimate case (dormant execution paths stay disabled, per the
directive) -- the new gate's job is only to fire FIRST, and with a distinguishable reason,
whenever the linked task is missing, unapproved, cross-tenant, or no longer a live dispatch
target."""

from __future__ import annotations

import uuid

import pytest

from app.mainai_execution.approval import grant_task_approval
from app.mainai_execution.executor import cancel_task
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

_STAGED_MESSAGE_FRAGMENT = "first real provider invoke remains staged"


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as the other mainai_execution-touching test modules."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(superuser_db, make_verified_user):
    user, _password = make_verified_user()
    return user.id


def _goal_plan_task(db, owner_id, *, approval_policy="autonomous_development_work"):
    goal = create_goal(db, owner_id=owner_id, title="Workforce approval-gap remediation test", original_instruction="x", created_by="founder", approval_policy=approval_policy)
    plan = create_plan(db, goal=goal, rationale="test", tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")], created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).first()
    return goal, task


def _delegate(db, owner_id, *, task_id=None):
    register_workforce_agent(
        db, owner_id=owner_id, agent_key=f"b-{uuid.uuid4().hex[:8]}", name="B", role="builder",
        agent_type="CODING", capability_tags=["low_risk_classification"], status="active",
        trust_zone="LOCAL_INTERNAL", allowed_tool_classes=["read_excerpt"],
    )
    verifier = register_workforce_agent(
        db, owner_id=owner_id, agent_key=f"v-{uuid.uuid4().hex[:8]}", name="V", role="verifier",
        agent_type="VERIFIER", capability_tags=["verification"], status="active", trust_zone="LOCAL_INTERNAL",
    )
    request = submit_delegation_request(
        db, owner_id=owner_id, goal_text="x", required_capability="low_risk_classification",
        task_id=task_id, verification_requirement="independent_verifier",
    )
    return resolve_delegation(db, owner_id=owner_id, request=request, verifier_profile_id=verifier.id)


def _gates_satisfied():
    reset_activation_gates_for_tests()
    for key in REQUIRED_ACTIVATION_GATES:
        record_gate_verification(key, status=GateStatus.verified, evidence_ref=f"test:{key}")


def _activate(db, owner_id, assignment):
    with pytest.raises(ProviderActivationBlocked) as exc_info:
        execute_workforce_assignment(
            db, owner_id=owner_id, assignment=assignment, goal_text="x",
            capability="low_risk_classification", activate_provider=True,
        )
    return str(exc_info.value)


def test_resolve_delegation_surfaces_task_id_into_provenance(superuser_db, owner_id):
    _goal, task = _goal_plan_task(superuser_db, owner_id)
    assignment = _delegate(superuser_db, owner_id, task_id=task.id)
    assert assignment.provenance["mainai_task_id"] == str(task.id)


def test_resolve_delegation_with_no_task_stores_none(superuser_db, owner_id):
    assignment = _delegate(superuser_db, owner_id, task_id=None)
    assert assignment.provenance["mainai_task_id"] is None


def test_missing_task_link_fails_closed(superuser_db, owner_id):
    assignment = _delegate(superuser_db, owner_id, task_id=None)
    _gates_satisfied()
    message = _activate(superuser_db, owner_id, assignment)
    assert "no linked MainAI task" in message
    assert _STAGED_MESSAGE_FRAGMENT not in message
    reset_activation_gates_for_tests()


def test_unapproved_task_fails_closed_before_the_staged_raise(superuser_db, owner_id):
    _goal, task = _goal_plan_task(superuser_db, owner_id)
    assignment = _delegate(superuser_db, owner_id, task_id=task.id)
    _gates_satisfied()
    message = _activate(superuser_db, owner_id, assignment)
    assert "requires founder approval" in message
    assert _STAGED_MESSAGE_FRAGMENT not in message
    reset_activation_gates_for_tests()


def test_approved_task_passes_the_new_gate_and_still_hits_the_untouched_staged_raise(superuser_db, owner_id):
    """Proves dormant execution paths stay disabled: approving the task clears the NEW gate
    but reaches the SAME pre-existing, unmodified staged-raise every other legitimate
    activate_provider=True call already hits."""
    _goal, task = _goal_plan_task(superuser_db, owner_id)
    assignment = _delegate(superuser_db, owner_id, task_id=task.id)
    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()
    _gates_satisfied()

    message = _activate(superuser_db, owner_id, assignment)
    assert _STAGED_MESSAGE_FRAGMENT in message
    reset_activation_gates_for_tests()


def test_cross_tenant_task_fails_closed(superuser_db, owner_id, make_verified_user):
    """Defense-in-depth tenant boundary at the application layer, independent of RLS: even if
    an assignment's provenance somehow named another tenant's task, the lookup is filtered by
    owner_id in the query itself and resolves to nothing."""
    other_owner, _ = make_verified_user()
    _other_goal, other_task = _goal_plan_task(superuser_db, other_owner.id)

    assignment = _delegate(superuser_db, owner_id, task_id=None)
    assignment.provenance = {**assignment.provenance, "mainai_task_id": str(other_task.id)}
    superuser_db.commit()
    _gates_satisfied()

    message = _activate(superuser_db, owner_id, assignment)
    assert "does not resolve to a real task owned by this tenant" in message
    assert _STAGED_MESSAGE_FRAGMENT not in message
    reset_activation_gates_for_tests()


def test_cancelled_task_fails_closed_even_after_approval(superuser_db, owner_id):
    """The closest existing equivalent to "revoked": a task that was approved and then
    cancelled must not pass the gate just because an approval_granted event still exists --
    MainAITaskEventType is append-only and has no separate revocation event, so the task's own
    current status is what "the founder no longer wants this dispatched" looks like here."""
    _goal, task = _goal_plan_task(superuser_db, owner_id)
    assignment = _delegate(superuser_db, owner_id, task_id=task.id)
    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()

    cancel_task(superuser_db, task=task, cancelled_by="founder")
    superuser_db.commit()
    _gates_satisfied()

    message = _activate(superuser_db, owner_id, assignment)
    assert "not a live dispatch target" in message
    assert _STAGED_MESSAGE_FRAGMENT not in message
    reset_activation_gates_for_tests()


def test_gate_always_rechecks_fresh_state_not_an_earlier_decision(superuser_db, owner_id):
    """TOCTOU-safety proof for "concurrent approval revocation and dispatch races": the SAME
    assignment passes the per-task gate once (approved task), and then -- simulating a
    cancellation that happens after that check but before any real invoke -- fails on the very
    next call, because the gate re-queries the task fresh every time rather than trusting a
    decision computed earlier. This mirrors the exact doctrine
    app.agent_coordination.dispatch.dispatch_assignment()'s own docstring states for itself:
    "ALWAYS re-runs ... immediately before touching the adapter, never trusts a decision
    computed earlier.\""""
    _goal, task = _goal_plan_task(superuser_db, owner_id)
    assignment = _delegate(superuser_db, owner_id, task_id=task.id)
    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()
    _gates_satisfied()

    first = _activate(superuser_db, owner_id, assignment)
    assert _STAGED_MESSAGE_FRAGMENT in first  # passed the per-task gate the first time

    cancel_task(superuser_db, task=task, cancelled_by="founder")
    superuser_db.commit()

    second = _activate(superuser_db, owner_id, assignment)
    assert "not a live dispatch target" in second  # the SAME call now fails at the per-task gate
    assert _STAGED_MESSAGE_FRAGMENT not in second
    reset_activation_gates_for_tests()
