"""WIRE AUTONOMOUS GAP -> CHILD-TASK GENERATION INTO THE LIVE MAINAI EXECUTION FLOW
(app/development_supervisor/service.py's call into app/autonomous_gap/service.py's
handle_live_gap_signal()) -- see docs/LIFE_AUTONOMOUS_GAP_TO_CHILD_TASK_LIVE_INTEGRATION.md for
the full design rationale.

This module proves the WIRING, not the primitives it wires together -- the gap-generation
primitive itself (record -> assess authority -> insert) is already fully covered by
test_autonomous_gap_child_task.py, and partial-plan-insertion by test_partial_plan_insertion.py.
Every test here drives the REAL run_supervisor() loop (same Scoped Development Supervisor
test_scoped_development_supervisor.py exercises) end to end, through a real local git repository,
a real Development Driver/Operator execution, and real Postgres -- never a mock of the live call
site.

Covers the founder's 12 required live end-to-end scenarios, in order:
  1.  Verification Failure Live Loop -- a real driver VERIFICATION_REQUIRED becomes a recorded
      gap, an inserted repair child, discovered and completed by a second supervisor invocation.
  2.  CAPABILITY_MISSING Live Loop -- a real driver CAPABILITY_MISSING, under an authorized
      self-work scope, becomes an inserted capability-development child.
  3.  Capability Gap Not Authorized -- the identical live signal outside self-work authority is
      rejected (NEEDS_AUTHORIZATION), no task inserted, unrelated work continues.
  4.  Interruption After Gap Record -- evidence already durably recorded (simulating a crash
      right after record_gap(), before insertion) never becomes a duplicate LifeProblem when the
      live path "resumes" (is invoked again for the same failure).
  5.  Interruption After Insert -- a child already inserted (simulating a crash right after
      insertion) is discovered, not recreated, on the next live invocation, and executes normally.
  6.  Stale Worker -- a lease lost before the driver even runs fails closed (OperatorAuthorizationError)
      before gap generation is ever reached; no gap, no child.
  7.  Provider Wait + Independent Gap -- a provider-dependent task waits while an unrelated task's
      real live gap is recorded and its child inserted in the SAME run.
  8.  Founder Correction -- a correction landing between task dispatch and gap handling makes the
      live authority check reject the generation as stale (NEEDS_AUTHORIZATION).
  9.  Depth/Count Bound -- a REAL chain of live-generated repair children (each one itself failing
      verification again) stops at DEFAULT_MAX_GENERATION_DEPTH, checkpointed resumably.
  10. Self-Work -- an authorized self-improvement goal runs a live verification-failure gap
      through the exact same chain, no bypass.
  11. No Human Job Translation -- the repair child's own provenance proves it was produced by the
      live signal, never by a founder/test directly constructing a MainAITask row.
  12. No Top-Level Goal Creation -- no live gap-generation path ever creates a MainAIGoal.

Plus a SECURITY section: non-gap classifications (WAITING_PROVIDER, WAITING_APPROVAL, ...) can
never become gaps; a generated child spec never carries a capability/shell-shaped field."""

from dataclasses import replace

import pytest
from sqlalchemy import select

from app.autonomous_gap.service import handle_live_gap_signal
from app.development_operator.service import OperatorAuthorizationError
from app.mainai_execution.approval import grant_task_approval
from app.development_supervisor.service import (
    SupervisorBounds,
    WorkBinding,
    instruction_sha256,
    record_founder_correction,
    run_supervisor,
)
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoal,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.problem_learning import LifeProblem
from app.safe_planner.service import CandidateStep, PlanCandidate
from tests.backend.mainai.test_scoped_development_supervisor import (
    _foundation,
    _independent_candidate,
)


def _unverified_candidate():
    """A candidate whose only step is a bare verification gate with nothing preceding it that
    could ever supply passing evidence -- deterministically reproduces VERIFICATION_REQUIRED,
    same fixture test_verification_failure_never_completes_parent already relies on."""
    return PlanCandidate(
        "attempt",
        "change",
        "missing evidence",
        (CandidateStep("gate", "verify", "pass", "verification_evaluate"),),
    )


def _missing_capability_candidate(capability="inspect_git_history"):
    """"inspect_git_history" is registered in app.development_operator.service's own
    DEVELOPMENT_CAPABILITIES dict (so Safe Planner's _validate_step() ACCEPTS the plan --
    unlike a truly unrecognized capability, which Safe Planner itself rejects BEFORE a plan is
    ever accepted, never reaching run_driver() at all), but app.development_driver.service's
    _invoke_operator() has no dispatch branch for it -- it falls through to
    operator.capability_missing(), making this a genuine EXECUTION-time (run_driver())
    CAPABILITY_MISSING, the one this module's live wiring actually recognizes."""
    return PlanCandidate(
        "inspect prior repository history",
        "history summary",
        "requires a capability not yet implemented by the operator dispatch table",
        (CandidateStep("inspect", "inspect history", "history data", capability),),
    )


def _repair_binding(task_id, prepare, scope):
    """A working, verification-passing candidate bound to a NEWLY discovered repair child --
    proves the child is real, executable work, not a broken stub. Writes to
    "test_calculator.py" -- already inside the fixed OperatorContext.allowed_paths
    _foundation()'s prepare_context() closure grants, so no scope/path plumbing is needed
    beyond what the original goal's own scope already authorizes. Mirrors
    test_scoped_development_supervisor.py's own _test_candidate() shape exactly (create ->
    run_focused_test[verification_required] -> verification_evaluate gate -> stage -> commit)
    -- commit_scoped_changes() unconditionally requires a passing "verification" checkpoint to
    exist, regardless of the child task's own (empty) verification_plan."""
    content = "def test_repair_applied():\n    assert True\n"
    return WorkBinding(
        task_id,
        prepare,
        PlanCandidate(
            "repair",
            "issue fixed",
            "patch, verify, and commit the fix",
            (
                CandidateStep(
                    "create",
                    "add repair test",
                    "file written",
                    "create_file",
                    {"path": "test_calculator.py", "content": content, "expected_sha256": None},
                    required_risk="LOCAL_WRITE",
                ),
                CandidateStep(
                    "test",
                    "run repair test",
                    "pytest pass",
                    "run_focused_test",
                    {"profile_name": "focused_pytest", "arguments": ["test_calculator.py"]},
                    ("create",),
                    "LOCAL_EXECUTION",
                    verification_required=True,
                ),
                CandidateStep(
                    "gate",
                    "verify evidence",
                    "verification pass",
                    "verification_evaluate",
                    {},
                    ("test",),
                ),
                CandidateStep(
                    "stage",
                    "stage repair",
                    "staged diff",
                    "stage_scoped_changes",
                    {"paths": ["test_calculator.py"]},
                    ("gate",),
                    "LOCAL_WRITE",
                ),
                CandidateStep(
                    "commit",
                    "commit repair",
                    "commit sha",
                    "commit_scoped_changes",
                    {"message": "Apply repair"},
                    ("stage",),
                    "LOCAL_WRITE",
                ),
            ),
        ),
        required_capabilities=("create_file", "run_focused_test", "stage_scoped_changes", "commit_scoped_changes"),
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )


def _repair_child(db, goal, source_task):
    return (
        db.execute(
            select(MainAITask).where(
                MainAITask.goal_id == goal.id,
                MainAITask.description
                == f"Repair the verification failure blocking: {source_task.description}",
            )
        )
        .scalars()
        .one()
    )


def _created_event(db, task):
    return db.execute(
        select(MainAITaskEvent)
        .where(
            MainAITaskEvent.task_id == task.id,
            MainAITaskEvent.event_type == MainAITaskEventType.created,
        )
        .order_by(MainAITaskEvent.created_at.asc())
        .limit(1)
    ).scalar_one()


def _gap_checkpoints(db, goal_id):
    """Only the Supervisor's OWN checkpoints (step="development_supervisor") -- a goal's
    checkpoint rows also include Safe Planner's/the Development Driver's own
    record_checkpoint() calls, whose executor_state has no "supervisor_state" key at all."""
    return [
        row
        for row in db.execute(select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal_id))
        .scalars()
        .all()
        if row.executor_state.get("step") == "development_supervisor"
    ]


# ---------------------------------------------------------------- 1. Verification Failure Live Loop


@pytest.mark.asyncio
async def test_verification_failure_live_loop_repairs_and_continues(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked

    first_run = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            WorkBinding(
                first.id,
                prepare,
                _unverified_candidate(),
                repository_identity=scope.repository_identity,
                allowed_paths=scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert first_run.classification == "VERIFICATION_REQUIRED"

    problem = (
        superuser_db.execute(
            select(LifeProblem).where(LifeProblem.mainai_task_id == first.id)
        )
        .scalars()
        .one()
    )
    assert problem.classification_basis == "deterministic"
    child = _repair_child(superuser_db, goal, first)
    assert child.status == MainAITaskStatus.ready
    grant_task_approval(superuser_db, task=child, approved_by="founder")

    second_run = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(_repair_binding(child.id, prepare, scope),),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert second_run.classification == "RUN_BOUND_REACHED"
    superuser_db.refresh(child)
    assert child.status == MainAITaskStatus.completed
    assert (tmp_path / "repo" / "test_calculator.py").exists()


# ---------------------------------------------------------------- 2. CAPABILITY_MISSING Live Loop


@pytest.mark.asyncio
async def test_capability_missing_live_loop_under_self_work_authority(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked
    instruction = "Improve deterministic development independence within this disposable repository."
    goal.original_instruction = instruction
    self_scope = replace(
        scope,
        self_work=True,
        authorized_instruction_sha256=instruction_sha256(instruction),
        # gap_from_capability_missing() hardcodes risk_level=medium for every capability gap
        # (adding a capability is inherently more than a read-only/local-write change) --
        # _foundation()'s own scope defaults to maximum_risk="low", which would otherwise reject
        # every capability-missing gap with NEEDS_AUTHORIZATION regardless of self_work.
        maximum_risk="medium",
    )

    result = await run_supervisor(
        superuser_db,
        scope=self_scope,
        bindings=(
            WorkBinding(
                first.id,
                prepare,
                _missing_capability_candidate(),
                repository_identity=self_scope.repository_identity,
                allowed_paths=self_scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "CAPABILITY_MISSING"

    child = (
        superuser_db.execute(
            select(MainAITask).where(
                MainAITask.goal_id == goal.id,
                MainAITask.description
                == "Add deterministic support for the missing capability: inspect_git_history",
            )
        )
        .scalars()
        .one()
    )
    assert child.status == MainAITaskStatus.ready
    checkpoints = _gap_checkpoints(superuser_db, goal.id)
    assert any(
        row.executor_state.get("phase") == "CAPABILITY_MISSING"
        and row.executor_state["supervisor_state"].get("gap_generation", {}).get("classification")
        == "ACCEPTED"
        for row in checkpoints
    )


# ---------------------------------------------------------------- 3. Capability Gap Not Authorized


@pytest.mark.asyncio
async def test_capability_gap_not_authorized_blocks_only_the_gap(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    first.priority = 20
    second.priority = 10
    assert scope.self_work is False

    gap_binding = WorkBinding(
        first.id,
        prepare,
        _missing_capability_candidate(),
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    independent = WorkBinding(
        second.id,
        prepare,
        _independent_candidate(),
        required_capabilities=("create_file", "run_focused_test"),
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    before = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).count()

    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(gap_binding, independent),
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert result.classification == "CAPABILITY_MISSING"
    assert second.status == MainAITaskStatus.completed  # unrelated work continued
    after = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).count()
    assert after == before  # no child inserted -- capability_missing gap_type needs self_work

    problem = (
        superuser_db.execute(select(LifeProblem).where(LifeProblem.mainai_task_id == first.id))
        .scalars()
        .one()
    )
    assert problem is not None  # evidence still preserved


# ---------------------------------------------------------------- 4. Interruption After Gap Record


@pytest.mark.asyncio
async def test_interruption_after_gap_record_resumes_without_duplicate(superuser_db, tmp_path):
    from app.autonomous_gap.service import gap_from_verification_required, record_gap

    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked
    superuser_db.flush()
    superuser_db.refresh(first)  # forces real enum deserialization (risk_level, status) via a DB round-trip

    # Simulate a crash that recorded gap evidence but never reached insertion: call the exact
    # gap-construction + record_gap() the live path itself would use, directly, with the SAME
    # requested_by the live run_supervisor() call below will also use ("development-supervisor",
    # its own default worker_id) -- requested_by is part of record_gap()'s provenance dict, which
    # create_problem()'s idempotency replay compares for EXACT equality, so a differing
    # requested_by across the "before crash" and "after resume" calls would raise
    # ProblemLearningError as a semantic conflict rather than converging, even though this is
    # genuinely the same worker identity resuming, not a takeover by a differently-named one.
    pre_gap = gap_from_verification_required(db=superuser_db, scope=scope, goal=goal, task=first)
    record_gap(superuser_db, owner_id=goal.owner_id, gap=pre_gap, requested_by="development-supervisor")
    superuser_db.commit()
    problems_before = (
        superuser_db.query(LifeProblem).filter(LifeProblem.mainai_task_id == first.id).count()
    )
    assert problems_before == 1

    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            WorkBinding(
                first.id,
                prepare,
                _unverified_candidate(),
                repository_identity=scope.repository_identity,
                allowed_paths=scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "VERIFICATION_REQUIRED"

    problems_after = (
        superuser_db.query(LifeProblem).filter(LifeProblem.mainai_task_id == first.id).count()
    )
    assert problems_after == 1  # no duplicate LifeProblem
    children = (
        superuser_db.query(MainAITask)
        .filter(
            MainAITask.goal_id == goal.id,
            MainAITask.description
            == f"Repair the verification failure blocking: {first.description}",
        )
        .all()
    )
    assert len(children) == 1  # exactly one canonical child, not zero, not two


# ---------------------------------------------------------------- 5. Interruption After Insert


@pytest.mark.asyncio
async def test_interruption_after_insert_discovers_not_recreates(superuser_db, tmp_path):
    from app.autonomous_gap.service import gap_from_verification_required, generate_child_task_for_gap
    from app.models.mainai_execution import MainAIPlan

    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked
    plan = superuser_db.get(MainAIPlan, first.plan_id)
    superuser_db.flush()
    superuser_db.refresh(first)  # forces real enum deserialization (risk_level, status) via a DB round-trip

    # Simulate a crash right after insertion completed (child exists) but before the caller's
    # next iteration observed it: pre-insert the exact same gap the live path would generate,
    # with the SAME requested_by the live run_supervisor() call below uses (see the identical
    # note in test_interruption_after_gap_record_resumes_without_duplicate above).
    pre_gap = gap_from_verification_required(db=superuser_db, scope=scope, goal=goal, task=first)
    pre_outcome = generate_child_task_for_gap(
        superuser_db, scope=scope, goal=goal, plan=plan, gap=pre_gap, requested_by="development-supervisor"
    )
    superuser_db.commit()
    assert pre_outcome.classification == "ACCEPTED"
    pre_child_id = pre_outcome.inserted_tasks[0].id

    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            WorkBinding(
                first.id,
                prepare,
                _unverified_candidate(),
                repository_identity=scope.repository_identity,
                allowed_paths=scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "VERIFICATION_REQUIRED"

    children = (
        superuser_db.query(MainAITask)
        .filter(
            MainAITask.goal_id == goal.id,
            MainAITask.description
            == f"Repair the verification failure blocking: {first.description}",
        )
        .all()
    )
    assert len(children) == 1
    assert children[0].id == pre_child_id  # the SAME row, not a second one
    grant_task_approval(superuser_db, task=children[0], approved_by="founder")

    # The "resumed" child is real, executable work -- not a broken duplicate.
    follow_up = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(_repair_binding(pre_child_id, prepare, scope),),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert follow_up.classification == "RUN_BOUND_REACHED"
    superuser_db.refresh(children[0])
    assert children[0].status == MainAITaskStatus.completed


# ---------------------------------------------------------------- 6. Stale Worker


@pytest.mark.asyncio
async def test_stale_worker_lease_lost_before_driver_fails_closed(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked

    def stale_prepare(task, job):
        context = prepare(task, job)
        # Simulate another worker taking over the lease AFTER this context was captured but
        # BEFORE the driver runs -- the operator's own _require_context() lease-generation
        # check must fail closed before gap generation is ever reached.
        job.lease_generation += 1
        superuser_db.flush()
        return context

    before_problems = superuser_db.query(LifeProblem).count()
    before_tasks = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).count()

    with pytest.raises(OperatorAuthorizationError, match="stale or absent"):
        await run_supervisor(
            superuser_db,
            scope=scope,
            bindings=(
                WorkBinding(
                    first.id,
                    stale_prepare,
                    _unverified_candidate(),
                    repository_identity=scope.repository_identity,
                    allowed_paths=scope.allowed_paths,
                ),
            ),
            bounds=SupervisorBounds(max_jobs=1),
        )

    assert superuser_db.query(LifeProblem).count() == before_problems
    assert (
        superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).count()
        == before_tasks
    )


# ---------------------------------------------------------------- 7. Provider Wait + Independent Gap


class _FailingProvider:
    async def propose(self, *_args, **_kwargs):
        from app.providers.base import ProviderError

        raise ProviderError("quota exhausted", category="rate_limited")


@pytest.mark.asyncio
async def test_provider_wait_and_independent_live_gap_both_progress(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    scope = replace(scope, provider_spend_authorized=True)
    first.priority = 20
    second.priority = 10

    provider_binding = WorkBinding(
        first.id,
        prepare,
        None,
        _FailingProvider(),
        provider_likely=True,
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    gap_binding = WorkBinding(
        second.id,
        prepare,
        _unverified_candidate(),
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )

    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(provider_binding, gap_binding),
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert first.status != MainAITaskStatus.completed  # still provider-blocked
    child = _repair_child(superuser_db, goal, second)
    assert child.status == MainAITaskStatus.ready  # independent gap's child progressed
    assert result.classification in {"VERIFICATION_REQUIRED", "WAITING_PROVIDER"}
    grant_task_approval(superuser_db, task=child, approved_by="founder")

    # The provider task is still excluded from the follow-up binding set -- proving the
    # generated child is independently executable without resolving the provider wait first.
    follow_up = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(_repair_binding(child.id, prepare, scope),),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert follow_up.classification == "RUN_BOUND_REACHED"
    superuser_db.refresh(child)
    assert child.status == MainAITaskStatus.completed


# ---------------------------------------------------------------- 8. Founder Correction


@pytest.mark.asyncio
async def test_founder_correction_between_dispatch_and_gap_handling_rejects_stale_generation(
    superuser_db, tmp_path
):
    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked

    def correcting_prepare(task, job):
        context = prepare(task, job)
        # A founder correction lands AFTER the task was dispatched under the old authority but
        # BEFORE this task's own gap handling runs -- realistic race the live authority check
        # (instruction_sha256 staleness) must catch.
        record_founder_correction(
            superuser_db, scope=scope, corrected_instruction="Only document, never edit."
        )
        return context

    before_tasks = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).count()

    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            WorkBinding(
                first.id,
                correcting_prepare,
                _unverified_candidate(),
                repository_identity=scope.repository_identity,
                allowed_paths=scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    # The driver's own VERIFICATION_REQUIRED result is unaffected by the correction (recorded via
    # this task's own checkpoint below), but since this binding is independent, run_supervisor()
    # defers-and-continues, re-enters its loop, and its OWN top-of-loop validate_scope() now also
    # sees the corrected instruction -- surfacing AUTHORITY_CHANGED as the run's overall result.
    # This is a STRONGER proof than the gap layer alone rejecting it: the whole run recognizes
    # the stale authority, not just gap generation.
    assert result.classification == "AUTHORITY_CHANGED"

    checkpoints = _gap_checkpoints(superuser_db, goal.id)
    assert any(
        row.executor_state["supervisor_state"].get("gap_generation", {}).get("classification")
        == "NEEDS_AUTHORIZATION"
        for row in checkpoints
    )
    after_tasks = superuser_db.query(MainAITask).filter(MainAITask.goal_id == goal.id).count()
    assert after_tasks == before_tasks  # stale generation rejected, nothing inserted
    problem = (
        superuser_db.execute(select(LifeProblem).where(LifeProblem.mainai_task_id == first.id))
        .scalars()
        .one()
    )
    assert problem is not None  # evidence still preserved despite rejection


# ---------------------------------------------------------------- 9. Depth/Count Bound


@pytest.mark.asyncio
async def test_repeated_gap_chain_stops_at_depth_bound_and_checkpoints_resumably(
    superuser_db, tmp_path
):
    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked

    current = first
    # DEFAULT_MAX_GENERATION_DEPTH == 3: task depths 0, 1, 2 each generate a child (depths
    # 1, 2, 3); the depth-3 child's OWN failure (its gap's generation_depth == 3) is the one
    # that must hit the bound and generate no depth-4 child. `first` is pre-approved by
    # _foundation(); each subsequently gap-generated child (repo_edit under
    # autonomous_development_work) needs its own explicit approval before dispatch.
    for index in range(4):
        if current.id != first.id:
            grant_task_approval(superuser_db, task=current, approved_by="founder")
        result = await run_supervisor(
            superuser_db,
            scope=scope,
            bindings=(
                WorkBinding(
                    current.id,
                    prepare,
                    _unverified_candidate(),
                    repository_identity=scope.repository_identity,
                    allowed_paths=scope.allowed_paths,
                ),
            ),
            bounds=SupervisorBounds(max_jobs=1),
        )
        assert result.classification == "VERIFICATION_REQUIRED"
        if index < 3:
            # Depths 0, 1, 2 each generate a real child (depths 1, 2, 3) -- only the 4th
            # (index 3, current's own gap at depth 3) hits the bound and inserts nothing, so
            # `current` deliberately stays the depth-3 task, not a nonexistent 5th child.
            current = _repair_child(superuser_db, goal, current)

    checkpoints = _gap_checkpoints(superuser_db, goal.id)
    assert any(
        row.executor_state["supervisor_state"].get("gap_generation", {}).get("classification")
        == "DEPTH_BOUND_REACHED"
        for row in checkpoints
    )
    no_grandchild = (
        superuser_db.query(MainAITask)
        .filter(
            MainAITask.goal_id == goal.id,
            MainAITask.description
            == f"Repair the verification failure blocking: {current.description}",
        )
        .count()
    )
    assert no_grandchild == 0  # bound stopped generation, resumably (evidence is still durable)
    problem = (
        superuser_db.execute(select(LifeProblem).where(LifeProblem.mainai_task_id == current.id))
        .scalars()
        .one()
    )
    assert problem is not None


# ---------------------------------------------------------------- 10. Self-Work


@pytest.mark.asyncio
async def test_self_work_verification_gap_runs_through_the_exact_same_chain(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked
    instruction = "Improve LifeAI's own bounded development independence in this disposable repository."
    goal.title = "bounded self-improvement work"
    goal.original_instruction = instruction
    self_scope = replace(
        scope, self_work=True, authorized_instruction_sha256=instruction_sha256(instruction)
    )

    result = await run_supervisor(
        superuser_db,
        scope=self_scope,
        bindings=(
            WorkBinding(
                first.id,
                prepare,
                _unverified_candidate(),
                repository_identity=self_scope.repository_identity,
                allowed_paths=self_scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "VERIFICATION_REQUIRED"
    child = _repair_child(superuser_db, goal, first)
    assert child.status == MainAITaskStatus.ready
    grant_task_approval(superuser_db, task=child, approved_by="founder")

    follow_up = await run_supervisor(
        superuser_db,
        scope=self_scope,
        bindings=(_repair_binding(child.id, prepare, self_scope),),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert follow_up.classification == "RUN_BOUND_REACHED"
    superuser_db.refresh(child)
    assert child.status == MainAITaskStatus.completed  # same chain, no self-work bypass needed


# ---------------------------------------------------------------- 11. No Human Job Translation


@pytest.mark.asyncio
async def test_repair_child_provenance_proves_no_human_job_translation(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked

    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            WorkBinding(
                first.id,
                prepare,
                _unverified_candidate(),
                repository_identity=scope.repository_identity,
                allowed_paths=scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert result.classification == "VERIFICATION_REQUIRED"
    child = _repair_child(superuser_db, goal, first)

    created = _created_event(superuser_db, child)
    assert created.detail.get("insertion_idempotency_key", "").startswith("autonomous_gap_child:")
    problem = (
        superuser_db.execute(select(LifeProblem).where(LifeProblem.mainai_task_id == first.id))
        .scalars()
        .one()
    )
    assert problem.provenance["requested_by"] == "development-supervisor"  # the live worker, not "founder"
    assert problem.classification_basis == "deterministic"
    grant_task_approval(superuser_db, task=child, approved_by="founder")

    follow_up = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(_repair_binding(child.id, prepare, scope),),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert follow_up.classification == "RUN_BOUND_REACHED"
    superuser_db.refresh(child)
    assert child.status == MainAITaskStatus.completed  # completed with no founder-authored task


# ---------------------------------------------------------------- 12. No Top-Level Goal Creation


@pytest.mark.asyncio
async def test_live_gap_generation_never_creates_a_new_goal(superuser_db, tmp_path):
    owner, goal, first, second, _, _, prepare, scope = _foundation(superuser_db, tmp_path, tied=True)
    second.status = MainAITaskStatus.blocked
    goals_before = superuser_db.query(MainAIGoal).filter(MainAIGoal.owner_id == owner.id).count()

    await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            WorkBinding(
                first.id,
                prepare,
                _unverified_candidate(),
                repository_identity=scope.repository_identity,
                allowed_paths=scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    child = _repair_child(superuser_db, goal, first)
    grant_task_approval(superuser_db, task=child, approved_by="founder")
    await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(_repair_binding(child.id, prepare, scope),),
        bounds=SupervisorBounds(max_jobs=1),
    )

    goals_after = superuser_db.query(MainAIGoal).filter(MainAIGoal.owner_id == owner.id).count()
    assert goals_after == goals_before  # the ONLY goal remains the original authorized one
    assert child.goal_id == goal.id  # the child is subordinate to the SAME goal


# ---------------------------------------------------------------- SECURITY


def test_non_gap_classifications_can_never_become_a_live_gap(superuser_db, tmp_path):
    from app.models.mainai_execution import MainAIPlan

    _, goal, first, _, _, _, _, scope = _foundation(superuser_db, tmp_path, tied=True)
    plan = superuser_db.get(MainAIPlan, first.plan_id)
    for classification in (
        "WAITING_PROVIDER",
        "WAITING_APPROVAL",
        "EXTERNAL_REVIEW_REQUIRED",
        "BLOCKED",
        "CANCELLED",
        "ACTION_BOUND_REACHED",
        "NEEDS_SELECTION",
        "COMPLETE",
    ):
        outcome = handle_live_gap_signal(
            superuser_db,
            scope=scope,
            goal=goal,
            plan=plan,
            task=first,
            classification=classification,
            requested_by="security-test",
        )
        assert outcome is None
    before = superuser_db.query(LifeProblem).count()
    assert before == 0


def test_generated_child_spec_never_carries_a_capability_field(superuser_db, tmp_path):
    from app.autonomous_gap.service import gap_from_verification_required, propose_child_task_spec
    import dataclasses

    _, goal, first, _, _, _, _, scope = _foundation(superuser_db, tmp_path, tied=True)
    gap = gap_from_verification_required(db=superuser_db, scope=scope, goal=goal, task=first)
    spec = propose_child_task_spec(gap)
    field_names = {f.name for f in dataclasses.fields(spec)}
    assert "capability" not in field_names
    assert "capabilities" not in field_names
    assert spec.task_type == "repo_edit"
