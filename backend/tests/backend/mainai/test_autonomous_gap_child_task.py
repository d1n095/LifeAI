"""AUTONOMOUS GAP -> CHILD-TASK GENERATION (app/autonomous_gap/service.py) -- turning a REAL,
evidence-backed gap discovered while executing an authorized goal into a bounded child task,
handed off to the merged partial-plan-insertion primitive. See
docs/LIFE_AUTONOMOUS_GAP_TO_CHILD_TASK.md for the full design rationale.

Covers the founder's 16 required end-to-end scenarios, in order:
  1.  Verification Failure -> Child Task -- a real verification_failed event becomes a bounded
      repair child, inserted, ready to run.
  2.  CAPABILITY_MISSING -> Authorized Child -- self-work-authorized scope generates a bounded
      capability-development child.
  3.  CAPABILITY_MISSING -> NEEDS_AUTHORIZATION -- same gap, no self-work authority: rejected,
      evidence still preserved.
  4.  Missing Prerequisite -- a new task is inserted with an ADDITIVE dependency edge onto an
      existing task, without recreating it.
  5.  Duplicate Gap -- the same semantic gap discovered twice yields exactly one canonical task.
  6.  Provider Idea != Authority -- a non-authoritative evidence_kind is refused outright.
  7.  Out-of-Scope Gap -- a gap outside the authorized repository/path scope is rejected.
  8.  Founder Correction -- a stale authorization (goal instruction changed) is rejected.
  9.  Interruption/Resume -- evidence recorded but insertion interrupted; resume inserts exactly
      once, no duplicate.
  10. Inserted Task -> Supervisor -- the Scoped Development Supervisor discovers the generated
      child as ordinary work.
  11. Multi-Gap Continuation -- an unresolved gap is preserved as waiting while an independent,
      authorized gap continues.
  12. Depth Bound -- generation stops at the configured depth, evidence preserved, resumable.
  13. Self-Work -- an authorized self-improvement goal generates a child through the same full
      chain, no bypass.
  14. No Top-Level Goal Creation -- gap generation never creates a MainAIGoal.
  15. Approval Policy -- the generated child still obeys the goal's named approval policy.
  16. Provider Independence -- deterministic gap generation works with the provider registry
      unavailable.

Plus two focused authority-state-machine tests (EXTERNAL_REVIEW_REQUIRED, NEEDS_SELECTION) and a
SECURITY section covering: no provider output grants authority; no shell/merge/deploy path; no
direct mainai_tasks row construction (insertion only ever flows through insert_plan_tasks()); RLS
owner isolation; fail-closed malformed evidence; concurrency safety.

Real local Postgres (RLS included), same conventions as test_partial_plan_insertion.py."""

import dataclasses
import threading
import uuid

import pytest
from sqlalchemy import text as sa_text

from app.autonomous_gap.service import (
    DiscoveredGap,
    GapEvidenceError,
    GapGenerationBounds,
    generate_child_task_for_gap,
    record_gap,
    run_gap_generation,
)
from app.development_supervisor.service import SupervisorBounds, SupervisorScope, discover_candidates
from app.mainai_execution import approval, planner
from app.mainai_execution.approval import ApprovalRequiredError
from app.mainai_execution.plan_insertion import PlanInsertionValidationError
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import (
    MainAIGoal,
    MainAIGoalRiskLevel,
    MainAIPlan,
    MainAITask,
    MainAITaskDependency,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.problem_learning import LifeProblem
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as test_partial_plan_insertion.py's own identical fixture."""
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _goal_and_plan(db_session, owner_id, *, task_specs=None, instruction="Do the thing, carefully.", approval_policy=None):
    create_kwargs = {"approval_policy": approval_policy} if approval_policy is not None else {}
    goal = planner.create_goal(
        db_session, owner_id=owner_id, title="Test goal", original_instruction=instruction, created_by="test", **create_kwargs
    )
    if task_specs is None:
        task_specs = [
            PlannedTaskSpec(description="A", task_type="repo_edit"),
            PlannedTaskSpec(description="B", task_type="repo_edit"),
        ]
    plan = planner.create_plan(db_session, goal=goal, rationale="base plan", tasks=task_specs, created_by="test")
    db_session.commit()
    return goal, plan


def _tasks(db_session, plan_id):
    return db_session.query(MainAITask).filter(MainAITask.plan_id == plan_id).order_by(MainAITask.created_at).all()


def _auth_hash(goal) -> str:
    import hashlib

    return hashlib.sha256(goal.original_instruction.encode()).hexdigest()


def _scope(
    goal,
    *,
    self_work=False,
    authority_kind="founder_requirement",
    maximum_risk="medium",
    allowed_paths=("backend/app",),
    allowed_capabilities=(),
    repository_identity="lifeai",
):
    return SupervisorScope(
        owner_id=goal.owner_id,
        goal_id=goal.id,
        authority_kind=authority_kind,
        authority_ref="test",
        authorized_instruction_sha256=_auth_hash(goal),
        repository_identity=repository_identity,
        allowed_paths=allowed_paths,
        allowed_capabilities=allowed_capabilities,
        maximum_risk=maximum_risk,
        self_work=self_work,
    )


def _verification_failure_gap(goal, scope, task_id, *, reason="targeted test failed", required_outcome="Fix it"):
    return DiscoveredGap(
        gap_type="verification_failure",
        evidence_kind="verification_failure",
        parent_goal_id=goal.id,
        reason=reason,
        required_outcome=required_outcome,
        task_type="repo_edit",
        source_task_id=task_id,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        risk_level=MainAIGoalRiskLevel.low,
    )


def _capability_missing_gap(goal, scope, task_id):
    return DiscoveredGap(
        gap_type="capability_missing",
        evidence_kind="capability_missing",
        parent_goal_id=goal.id,
        reason="request class X always requires provider assistance because deterministic planner coverage is missing",
        required_outcome="Add deterministic planning support for request class X",
        task_type="repo_edit",
        source_task_id=task_id,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        risk_level=MainAIGoalRiskLevel.medium,
    )


# ---------------------------------------------------------------- 1. Verification Failure -> Child Task


def test_verification_failure_generates_and_inserts_a_ready_repair_child_task(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    a.status = MainAITaskStatus.retryable_failed
    db_session.add(
        MainAITaskEvent(
            task_id=a.id,
            owner_id=owner_id,
            event_type=MainAITaskEventType.verification_failed,
            detail={"passed": False, "steps": [{"kind": "targeted_tests", "passed": False, "detail": {"target": "tests/backend/test_x.py", "returncode": 1}}]},
        )
    )
    db_session.commit()

    scope = _scope(goal)
    gap = _verification_failure_gap(
        goal, scope, a.id, reason="targeted test tests/backend/test_x.py failed with returncode 1", required_outcome="Fix the failing targeted test"
    )
    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert outcome.classification == "ACCEPTED"
    assert outcome.problem is not None
    assert outcome.problem.mainai_task_id == a.id
    assert len(outcome.inserted_tasks) == 1
    child = outcome.inserted_tasks[0]
    assert child.plan_id == plan.id
    assert child.status == MainAITaskStatus.ready  # no dependencies -> immediately dispatchable


# ---------------------------------------------------------------- 2/3. CAPABILITY_MISSING


def test_capability_missing_with_self_work_authority_generates_an_authorized_child(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id, instruction="Make Life less dependent on external AI providers.")
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal, self_work=True)
    gap = _capability_missing_gap(goal, scope, a.id)

    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert outcome.classification == "ACCEPTED"
    assert len(outcome.inserted_tasks) == 1


def test_capability_missing_without_self_work_authority_needs_authorization(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal, self_work=False)
    gap = _capability_missing_gap(goal, scope, a.id)
    before = db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count()

    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert outcome.classification == "NEEDS_AUTHORIZATION"
    assert outcome.inserted_tasks is None
    assert outcome.problem is not None  # evidence still preserved
    assert db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count() == before


# ---------------------------------------------------------------- 4. Missing Prerequisite


def test_missing_prerequisite_inserts_an_additive_dependency_without_recreating_the_existing_task(db_session, owner_id):
    # B depends on A so it stays 'pending' (not 'ready') -- plan_insertion.py only allows a new
    # retroactive prerequisite edge on a 'pending'/'blocked' task, never one already 'ready'.
    goal, plan = _goal_and_plan(
        db_session,
        owner_id,
        task_specs=[
            PlannedTaskSpec(description="A", task_type="repo_edit"),
            PlannedTaskSpec(description="B", task_type="repo_edit", depends_on=[0]),
        ],
    )
    _a, b = _tasks(db_session, plan.id)
    b_id = b.id
    scope = _scope(goal)
    gap = DiscoveredGap(
        gap_type="missing_prerequisite",
        evidence_kind="repository_inspection",
        parent_goal_id=goal.id,
        reason="repository inspection proves prerequisite C is required before existing task B",
        required_outcome="Add prerequisite C",
        task_type="repo_edit",
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        existing_task_dependency_target=b.id,
    )

    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert outcome.classification == "ACCEPTED"
    c = outcome.inserted_tasks[0]
    db_session.refresh(b)
    assert b.id == b_id  # never recreated
    b_deps = {row.depends_on_task_id for row in db_session.query(MainAITaskDependency).filter(MainAITaskDependency.task_id == b.id).all()}
    assert c.id in b_deps  # additive: B's original dependency on A is untouched, C is added


# ---------------------------------------------------------------- 5. Duplicate Gap


def test_duplicate_gap_discovered_twice_yields_exactly_one_canonical_task(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal)
    gap = _verification_failure_gap(goal, scope, a.id, reason="same failure discovered twice", required_outcome="Fix the duplicate gap")

    first = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()
    second = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert first.problem.id == second.problem.id
    assert [t.id for t in first.inserted_tasks] == [t.id for t in second.inserted_tasks]
    count = db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id, MainAITask.description == "Fix the duplicate gap").count()
    assert count == 1


# ---------------------------------------------------------------- 6. Provider Idea != Authority


def test_provider_suggested_idea_is_not_authoritative_evidence(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    scope = _scope(goal)
    gap = DiscoveredGap(
        gap_type="unrelated_enhancement",
        evidence_kind="idea",  # not in AUTHORIZED_EVIDENCE_KINDS
        parent_goal_id=goal.id,
        reason="a provider suggested an unrelated enhancement",
        required_outcome="Do the unrelated enhancement",
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    before = db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count()

    with pytest.raises(GapEvidenceError):
        generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.rollback()

    assert db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count() == before
    assert db_session.query(LifeProblem).filter(LifeProblem.owner_id == owner_id).count() == 0


# ---------------------------------------------------------------- 7. Out-of-Scope Gap


def test_out_of_scope_gap_is_rejected(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id, instruction="Add multiplication support to the calculator module.")
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal, allowed_paths=("backend/app/calculator",))
    gap = DiscoveredGap(
        gap_type="unrelated_scope",
        evidence_kind="repository_inspection",
        parent_goal_id=goal.id,
        reason="calculator work led to an authentication refactor suggestion",
        required_outcome="Refactor authentication",
        source_task_id=a.id,
        repository_identity=scope.repository_identity,
        allowed_paths=("backend/app/auth",),
    )

    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert outcome.classification == "OUT_OF_SCOPE"
    assert outcome.inserted_tasks is None


# ---------------------------------------------------------------- 8. Founder Correction


def test_founder_correction_invalidates_a_stale_gap_generation(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal)  # computed from the ORIGINAL instruction

    goal.original_instruction = "Do something completely different now -- founder correction."
    db_session.commit()

    gap = _verification_failure_gap(goal, scope, a.id, reason="stale gap under superseded direction")
    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert outcome.classification == "NEEDS_AUTHORIZATION"
    assert outcome.inserted_tasks is None


# ---------------------------------------------------------------- 9. Interruption/Resume


def test_interruption_after_gap_evidence_recorded_resumes_without_a_duplicate_child(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal)
    gap = _verification_failure_gap(goal, scope, a.id, reason="interrupt test", required_outcome="Fix the interrupted gap")

    # Simulate a crash AFTER evidence is recorded but BEFORE insertion.
    problem = record_gap(db_session, owner_id=owner_id, gap=gap, requested_by="test")
    db_session.commit()
    problem_id = problem.id

    # Resume: the full pipeline for the SAME gap must reuse the SAME LifeProblem and insert exactly once.
    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert outcome.problem.id == problem_id
    assert outcome.classification == "ACCEPTED"
    assert len(outcome.inserted_tasks) == 1
    count = db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id, MainAITask.description == "Fix the interrupted gap").count()
    assert count == 1


# ---------------------------------------------------------------- 10. Inserted Task -> Supervisor


def test_supervisor_discovers_the_generated_child_as_ordinary_mainai_work(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal)
    gap = _verification_failure_gap(goal, scope, a.id)

    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()
    child = outcome.inserted_tasks[0]

    candidates = discover_candidates(db_session, scope=scope, bindings=[], bounds=SupervisorBounds())
    assert any(c.task_id == child.id for c in candidates)


# ---------------------------------------------------------------- 11. Multi-Gap Continuation


def test_multi_gap_continuation_preserves_the_waiting_gap_and_continues_the_authorized_one(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id, instruction="Make Life less dependent on external AI providers.")
    a, b = _tasks(db_session, plan.id)
    scope = _scope(goal, self_work=True)
    gap_a = DiscoveredGap(
        gap_type="capability_missing",
        evidence_kind="capability_missing",
        parent_goal_id=goal.id,
        reason="needs provider adapter selection",
        required_outcome="Add provider-dependent capability",
        source_task_id=a.id,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        unknowns=("which provider adapter to target",),
    )
    gap_b = _verification_failure_gap(goal, scope, b.id, reason="deterministic fix", required_outcome="Fix the deterministic thing")

    result = run_gap_generation(
        db_session, scope=scope, goal=goal, plan=plan, gaps=[gap_a, gap_b], bounds=GapGenerationBounds(), requested_by="test", run_id=uuid.uuid4()
    )
    db_session.commit()

    assert result.outcomes[0].classification == "NEEDS_CLARIFICATION"  # A preserved as waiting
    assert result.outcomes[1].classification == "ACCEPTED"  # B continues regardless
    assert result.stopped_reason is None


# ---------------------------------------------------------------- 12. Depth Bound


def test_depth_bound_stops_generation_and_preserves_evidence_resumably(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal)
    gap = _verification_failure_gap(goal, scope, a.id, reason="deep lineage")
    deep_gap = dataclasses.replace(gap, generation_depth=3)

    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=deep_gap, requested_by="test", max_generation_depth=3)
    db_session.commit()

    assert outcome.classification == "DEPTH_BOUND_REACHED"
    assert outcome.inserted_tasks is None
    assert outcome.problem is not None  # evidence preserved even though generation stopped


# ---------------------------------------------------------------- 13. Self-Work


def test_self_work_generates_a_child_through_the_full_chain_with_no_bypass(db_session, owner_id):
    goal, plan = _goal_and_plan(
        db_session, owner_id, instruction="Improve Life's deterministic development independence.", approval_policy="autonomous_development_work"
    )
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal, self_work=True)
    gap = DiscoveredGap(
        gap_type="self_improvement",
        evidence_kind="capability_missing",
        parent_goal_id=goal.id,
        reason="internal execution evidence proves a deterministic coverage gap",
        required_outcome="Add deterministic coverage for request class X",
        task_type="repo_edit",
        source_task_id=a.id,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )

    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert outcome.classification == "ACCEPTED"
    child = outcome.inserted_tasks[0]
    with pytest.raises(ApprovalRequiredError):
        approval.require_task_approval(db_session, task=child, goal_approval_policy=goal.approval_policy)


# ---------------------------------------------------------------- 14. No Top-Level Goal Creation


def test_gap_generation_never_creates_a_new_goal(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal)
    before_goals = db_session.query(MainAIGoal).filter(MainAIGoal.owner_id == owner_id).count()

    gap = _verification_failure_gap(goal, scope, a.id)
    generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()

    assert db_session.query(MainAIGoal).filter(MainAIGoal.owner_id == owner_id).count() == before_goals


# ---------------------------------------------------------------- 15. Approval Policy


def test_generated_child_obeys_the_goals_named_approval_policy_no_execution_bypass(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id, approval_policy="autonomous_development_work")
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal)
    gap = _verification_failure_gap(goal, scope, a.id)

    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()
    child = outcome.inserted_tasks[0]

    with pytest.raises(ApprovalRequiredError):
        approval.require_task_approval(db_session, task=child, goal_approval_policy=goal.approval_policy)
    approval.grant_task_approval(db_session, task=child, approved_by="founder")
    db_session.commit()
    approval.require_task_approval(db_session, task=child, goal_approval_policy=goal.approval_policy)  # no longer raises


# ---------------------------------------------------------------- 16. Provider Independence


@pytest.mark.asyncio
async def test_deterministic_gap_generation_works_with_the_provider_registry_unavailable(db_session, owner_id, monkeypatch):
    from app.providers.base import ProviderError
    from app.providers.openai_provider import OpenAIProvider

    async def _always_fails(self, *args, **kwargs):
        raise ProviderError(
            "simulated total provider outage",
            category="rate_limited",
            provider_request_may_have_left=False,
        )

    monkeypatch.setattr(OpenAIProvider, "chat", _always_fails)

    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal)
    gap = _verification_failure_gap(goal, scope, a.id)

    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()
    assert outcome.classification == "ACCEPTED"


# ---------------------------------------------------------------- Focused authority-state-machine coverage


def test_high_risk_gap_always_requires_external_review_regardless_of_envelope(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal, maximum_risk="high")
    gap = DiscoveredGap(
        gap_type="verification_failure",
        evidence_kind="verification_failure",
        parent_goal_id=goal.id,
        reason="high risk remediation",
        required_outcome="x",
        source_task_id=a.id,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        risk_level=MainAIGoalRiskLevel.high,
    )
    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()
    assert outcome.classification == "EXTERNAL_REVIEW_REQUIRED"


def test_multiple_candidate_options_requires_founder_selection(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    scope = _scope(goal)
    gap = DiscoveredGap(
        gap_type="verification_failure",
        evidence_kind="verification_failure",
        parent_goal_id=goal.id,
        reason="ambiguous remediation",
        required_outcome="x",
        source_task_id=a.id,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        candidate_options=("approach A", "approach B"),
    )
    outcome = generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.commit()
    assert outcome.classification == "NEEDS_SELECTION"


# ================================================================== SECURITY


def test_security_module_never_constructs_a_mainai_task_row_directly_and_has_no_shell_or_deploy_path():
    import inspect

    import app.autonomous_gap.service as gap_module

    source = inspect.getsource(gap_module)
    assert "MainAITask(" not in source, "must only ever create MainAITask rows via insert_plan_tasks(), never directly"
    forbidden = ["subprocess", "os.system", "os.popen", "shutil.", "merge_pull_request", "kubectl", "\ndeploy(", "docker ", "git push", "force_push"]
    for token in forbidden:
        assert token not in source, f"module must never reference {token!r}"


def test_security_owner_isolation_cross_owner_dependency_target_is_rejected(db_session, superuser_db, owner_id, make_verified_user):
    other_user, _password = make_verified_user()
    goal, plan = _goal_and_plan(db_session, owner_id)

    other_goal = planner.create_goal(superuser_db, owner_id=other_user.id, title="other owner goal", original_instruction="Other owner's work.", created_by="test")
    other_plan = planner.create_plan(
        superuser_db, goal=other_goal, rationale="other owner's plan", tasks=[PlannedTaskSpec(description="Other task", task_type="repo_edit")], created_by="test"
    )
    superuser_db.commit()
    other_task_id = superuser_db.query(MainAITask).filter(MainAITask.plan_id == other_plan.id).one().id

    scope = _scope(goal)
    gap = DiscoveredGap(
        gap_type="missing_prerequisite",
        evidence_kind="repository_inspection",
        parent_goal_id=goal.id,
        reason="cross owner dependency attempt",
        required_outcome="x",
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
        existing_task_dependency_target=other_task_id,
    )

    with pytest.raises(PlanInsertionValidationError):
        generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.rollback()


def test_security_fail_closed_on_malformed_evidence_kind(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    scope = _scope(goal)
    gap = DiscoveredGap(
        gap_type="verification_failure",
        evidence_kind="not_a_real_evidence_kind",
        parent_goal_id=goal.id,
        reason="r",
        required_outcome="x",
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    with pytest.raises(GapEvidenceError):
        generate_child_task_for_gap(db_session, scope=scope, goal=goal, plan=plan, gap=gap, requested_by="test")
    db_session.rollback()


def test_security_concurrent_gap_generation_for_the_same_gap_produces_exactly_one_canonical_child(db_session, owner_id, superuser_db):
    from app.db import SessionLocal

    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    goal_id, plan_id, task_id = goal.id, plan.id, a.id
    scope = _scope(goal)

    results: list[list[uuid.UUID]] = []
    errors: list[str] = []
    barrier = threading.Barrier(2, timeout=5)

    def _worker():
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            g = session.get(MainAIGoal, goal_id)
            p = session.get(MainAIPlan, plan_id)
            gap = _verification_failure_gap(g, scope, task_id, reason="race", required_outcome="Race fix")
            barrier.wait()
            outcome = generate_child_task_for_gap(session, scope=scope, goal=g, plan=p, gap=gap, requested_by="test")
            session.commit()
            results.append([t.id for t in outcome.inserted_tasks])
        except Exception as exc:  # noqa: BLE001 - captured for the assertion below, not swallowed
            errors.append(repr(exc))
        finally:
            session.close()

    threads = [threading.Thread(target=_worker), threading.Thread(target=_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert errors == [], f"both concurrent calls must succeed idempotently, got: {errors}"
    assert len(results) == 2
    assert results[0] == results[1], "both callers must get back the SAME canonical child task id(s)"

    row_count = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_tasks WHERE plan_id = :p AND description = 'Race fix'"), {"p": str(plan_id)}
    ).scalar()
    assert row_count == 1, "exactly one row, no duplicate created by the race"
