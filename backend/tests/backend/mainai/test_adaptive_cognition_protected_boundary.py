"""Adaptive Cognition / Protected-vs-Adaptive Boundary -- proves, against a real, concrete
cross-system scenario, that Life's own adaptive strategy-evolution machinery (migrations
0043-0045: `work_strategies`, strategy comparisons/experiments/promotion candidates) can
NEVER reach into or weaken the protected, governed layers this codebase already established:
founder task approval (`app.mainai_execution.approval`) and the fail-closed agent dispatch
gate (`app.agent_coordination.dispatch.evaluate_dispatch_readiness`).

This answers item 4 of the mission "LIFE SELF-MODEL, ADAPTIVE COGNITION & CORPUS READINESS":
"even governed components may evolve later, but only through explicit controlled change...
not ordinary strategy evolution." No new schema, no new service module -- migrations 0043-0045
already implement the adaptive-cognition layer this mission's item 3 asked for (versioned,
supersedable work strategies; evidence-gated comparison/experimentation/promotion, never a
permanent winner). What was missing, and what this file adds, is an explicit, cross-system
TEST proving the boundary those existing docs already claim in prose
(`docs/LIFE_SELF_OPTIMIZING_WORK_INTELLIGENCE.md`'s "No silent core self-modification is
permitted", `docs/LIFE_STRATEGY_EVALUATION_AND_PROMOTION.md`'s "Approval is only evidence that
the proposal passed this review state; it has no code path that activates a strategy or
rewrites production policy") but never tested against another real governed subsystem outside
their own module.

Structural precondition, verified directly: neither `app.strategy_evaluation` nor
`app.work_intelligence` nor `app.strategy_synthesis` imports anything from
`app.agent_coordination` or `app.mainai_execution.approval` at all -- there is no code path
capable of reaching into the dispatch/approval gate in the first place, the same "cheap,
concrete, testable" module-dependency-check pattern
`docs/LIFE_AI_INDEPENDENCE_CONSTITUTION.md` §3 already establishes for the AI-independence
boundary, applied here to a different governed boundary."""

import ast
import uuid
from pathlib import Path

import pytest

from app.agent_coordination.dispatch import OUTCOME_APPROVAL_REQUIRED, OUTCOME_ASSIGNABLE, evaluate_dispatch_readiness
from app.agent_coordination.service import acquire_lease, create_work_assignment, register_agent
from app.intelligence_governance import record_evidence, record_execution
from app.mainai_execution.approval import ApprovalRequiredError, grant_task_approval, require_task_approval
from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan
from app.models.mainai_execution import MainAITask
from app.strategy_evaluation import (
    assess_comparability,
    assess_quality,
    create_comparison,
    create_promotion_candidate,
    link_comparison,
    transition_candidate,
)
from app.work_intelligence import bind_strategy_execution, create_strategy, record_verification_obligation, record_verification_observation


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _owner(db, make_verified_user):
    user, _password = make_verified_user()
    return user


def _goal_plan_task(db, owner_id, *, approval_policy="autonomous_development_work"):
    goal = create_goal(db, owner_id=owner_id, title="Boundary test", original_instruction="test", created_by="founder", approval_policy=approval_policy)
    plan = create_plan(db, goal=goal, rationale="boundary test", tasks=[PlannedTaskSpec(description="Task 0", task_type="repo_edit")], created_by="founder")
    db.commit()
    task = db.query(MainAITask).filter_by(plan_id=plan.id).order_by(MainAITask.created_at).first()
    return goal, plan, task


def _agent(db, key, **kwargs):
    defaults = dict(display_name=key, adapter_kind="cli", execution_mode="cli_interactive", supports_read=True, supports_write=True, concurrency_limit=3)
    defaults.update(kwargs)
    return register_agent(db, agent_key=f"{key}-{uuid.uuid4().hex[:8]}", **defaults)


def _assign(db, *, owner_id, goal, task, agent, paths):
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=task.id, agent_id=agent.id, role="builder", read_write_mode="read_write",
        repository_identity="lifeai", allowed_paths=list(paths), requested_by="test",
    )


def _verify(db, owner_id, binding, key):
    evidence = record_evidence(
        db, owner_id=owner_id, execution_id=binding.execution_id, evidence_kind="test_result", review_kind="deterministic_tool",
        deterministic=True, payload={"passed": True}, source_type="pytest", source_ref=key, idempotency_key=f"ev-{key}",
    )
    obligation = record_verification_obligation(
        db, owner_id=owner_id, strategy_execution_id=binding.id, requirement_kind="focused_tests", description="required", idempotency_key=f"obl-{key}"
    )
    record_verification_observation(
        db, owner_id=owner_id, obligation_id=obligation.id, status="performed_passed", reason="observed", evidence_id=evidence.id, idempotency_key=f"vo-{key}"
    )


def _fully_approved_strategy(db, owner_id, task):
    """The strongest, most 'adaptive-cognition-approved' state an owner's own strategy layer
    can reach: a challenger strategy that beat a real, quality-gated, comparability-assessed
    baseline comparison and was formally APPROVED through the real 0043-0045 governed
    promotion pipeline -- never a mock, never a shortcut. Mirrors
    test_strategy_evaluation_promotion.py's own proven `_pair`/`_verify` pipeline exactly."""

    strategy_key = f"boundary-workflow-{uuid.uuid4()}"
    baseline = create_strategy(db, owner_id=owner_id, strategy_key=strategy_key, version=1, idempotency_key=f"base-{uuid.uuid4()}")
    challenger = create_strategy(db, owner_id=owner_id, strategy_key=strategy_key, version=2, predecessor_id=baseline.id, idempotency_key=f"chal-{uuid.uuid4()}")

    execution_a = record_execution(db, owner_id=owner_id, task_id=task.id, idempotency_key=f"exec-a-{uuid.uuid4()}", provider="internal")
    execution_b = record_execution(db, owner_id=owner_id, task_id=task.id, idempotency_key=f"exec-b-{uuid.uuid4()}", provider="internal")
    binding_a = bind_strategy_execution(db, owner_id=owner_id, strategy_id=baseline.id, execution_id=execution_a.id, idempotency_key=f"bind-a-{uuid.uuid4()}")
    binding_b = bind_strategy_execution(db, owner_id=owner_id, strategy_id=challenger.id, execution_id=execution_b.id, idempotency_key=f"bind-b-{uuid.uuid4()}")

    comparison = create_comparison(
        db, owner_id=owner_id, baseline_binding_id=binding_a.id, challenger_binding_id=binding_b.id, task_id=task.id,
        task_type=task.task_type, domain="engineering", comparison_basis="deterministic", idempotency_key=f"compare-{uuid.uuid4()}",
    )
    _verify(db, owner_id, binding_a, f"a-{comparison.id}")
    _verify(db, owner_id, binding_b, f"b-{comparison.id}")
    assess_quality(db, owner_id=owner_id, comparison_id=comparison.id, subject="challenger", reason="all passed", idempotency_key=f"quality-{comparison.id}")
    assess_comparability(
        db, owner_id=owner_id, comparison_id=comparison.id, status="comparable", dimensions={}, reasons=[], idempotency_key=f"fair-{comparison.id}"
    )

    candidate = create_promotion_candidate(
        db, owner_id=owner_id, strategy_id=challenger.id, baseline_strategy_id=baseline.id, minimum_valid_comparisons=1, idempotency_key=f"cand-{uuid.uuid4()}"
    )
    link_comparison(db, owner_id=owner_id, candidate_id=candidate.id, comparison_id=comparison.id, idempotency_key=f"promo-link-{uuid.uuid4()}")
    transition_candidate(db, owner_id=owner_id, candidate_id=candidate.id, to_state="under_review", idempotency_key=f"review-{uuid.uuid4()}")
    approved, _ = transition_candidate(db, owner_id=owner_id, candidate_id=candidate.id, to_state="approved", idempotency_key=f"approve-{uuid.uuid4()}")
    return approved


# ============================================================================ Structural
# precondition: no code path in the adaptive layer can even reach the protected layer.

def test_strategy_evaluation_module_never_imports_the_protected_dispatch_or_approval_gates():
    """Mirrors docs/LIFE_AI_INDEPENDENCE_CONSTITUTION.md §3's own module-dependency-check
    pattern -- a cheap, concrete, testable proof that no import path exists, not merely that
    behavior happens to look correct today."""

    backend_root = Path(__file__).resolve().parents[3]
    protected_modules = ("app.agent_coordination", "app.mainai_execution.approval")
    for adaptive_package in ("strategy_evaluation", "work_intelligence", "strategy_synthesis"):
        package_dir = backend_root / "app" / adaptive_package
        assert package_dir.is_dir(), f"expected {package_dir} to exist"
        for py_file in package_dir.glob("*.py"):
            tree = ast.parse(py_file.read_text(), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for protected in protected_modules:
                        assert not node.module.startswith(protected), (
                            f"{py_file} imports from {node.module!r} -- the adaptive strategy layer "
                            f"must never import the protected dispatch/approval layer"
                        )
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        for protected in protected_modules:
                            assert not alias.name.startswith(protected), (
                                f"{py_file} imports {alias.name!r} -- the adaptive strategy layer "
                                f"must never import the protected dispatch/approval layer"
                            )


# ============================================================================ Behavioral
# proof: even a FULLY APPROVED strategy has zero effect on the protected gates.

def test_approved_strategy_never_satisfies_founder_task_approval(superuser_db, make_verified_user):
    owner = _owner(superuser_db, make_verified_user)
    _goal, _plan, task = _goal_plan_task(superuser_db, owner.id)

    with pytest.raises(ApprovalRequiredError):
        require_task_approval(superuser_db, task=task, goal_approval_policy="autonomous_development_work")

    approved_candidate = _fully_approved_strategy(superuser_db, owner.id, task)
    superuser_db.commit()
    assert approved_candidate.state == "approved"

    # The task is STILL unapproved -- a fully approved, promoted work strategy has no code
    # path that satisfies the founder's own approval gate.
    with pytest.raises(ApprovalRequiredError):
        require_task_approval(superuser_db, task=task, goal_approval_policy="autonomous_development_work")

    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()
    require_task_approval(superuser_db, task=task, goal_approval_policy="autonomous_development_work")  # only NOW passes


@pytest.mark.asyncio
async def test_approved_strategy_never_bypasses_the_agent_dispatch_gate(superuser_db, make_verified_user):
    """The concrete, real-world scenario: Life has a fully evaluated, verified, PROMOTED work
    strategy available -- the strongest evidence her own adaptive-cognition layer can produce.
    The dispatch gate must behave IDENTICALLY whether or not that strategy exists: still
    APPROVAL_REQUIRED before approval, still ASSIGNABLE only after the REAL founder approval,
    never influenced by the strategy's own approved state."""

    owner = _owner(superuser_db, make_verified_user)
    goal, _plan, task = _goal_plan_task(superuser_db, owner.id)
    agent = _agent(superuser_db, "claude-code")
    assignment = _assign(superuser_db, owner_id=owner.id, goal=goal, task=task, agent=agent, paths=["backend/app/adaptive_cognition_boundary_test/**"])
    acquire_lease(
        superuser_db, assignment=assignment, agent_id=agent.id, branch="claude/boundary-test", worktree_path="/tmp/wt-adaptive-boundary",
        allowed_paths=["backend/app/adaptive_cognition_boundary_test/**"], mode="read_write",
    )
    superuser_db.commit()

    approved_candidate = _fully_approved_strategy(superuser_db, owner.id, task)
    superuser_db.commit()
    assert approved_candidate.state == "approved"

    still_blocked = evaluate_dispatch_readiness(superuser_db, assignment=assignment, agent=agent)
    assert still_blocked.outcome == OUTCOME_APPROVAL_REQUIRED  # the approved strategy changed nothing here

    grant_task_approval(superuser_db, task=task, approved_by="founder")
    superuser_db.commit()
    now_ready = evaluate_dispatch_readiness(superuser_db, assignment=assignment, agent=agent)
    assert now_ready.outcome == OUTCOME_ASSIGNABLE  # only the REAL founder approval unblocks it
