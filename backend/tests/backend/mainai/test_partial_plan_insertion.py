"""PARTIAL PLAN INSERTION (app/mainai_execution/plan_insertion.py) -- the primitive that
safely inserts one or more new bounded child tasks into an EXISTING, authorized, active plan
without cancelling or superseding any valid existing sibling task. See
docs/LIFE_PARTIAL_PLAN_INSERTION.md for the full design rationale.

Covers the founder's 16 required end-to-end scenarios, in order:
  1.  Basic Insert -- existing siblings' ids/state are untouched, the new task is added.
  2.  Insert Prerequisite -- an existing task gains an ADDITIVE new dependency edge; its
      original edge is never rewritten or removed.
  3.  Multi-task Atomic Insert -- two new tasks, one depending on the other, both land.
  4.  Invalid Second Task -- one bad spec in a multi-task insertion voids the whole batch.
  5.  Cycle Attack -- a combined new+existing edge set that would create a cycle is rejected.
  6.  Replay -- an identical repeat call returns the SAME canonical tasks, no duplicates.
  7.  Conflicting Replay -- reusing an idempotency key with different content fails closed.
  8.  Concurrent Insert -- two real threads/sessions racing the same key produce exactly one
      canonical insertion (a real Postgres advisory lock, not manual signalling, decides).
  9.  Cross-Owner -- a dependency reference to another owner's task is rejected.
  10. Completed Sibling -- inserting beside a completed task leaves it byte-for-byte unchanged.
  11. Waiting Sibling -- a waiting_external task stays waiting; an independent new task still
      inserts cleanly.
  12. Running Sibling -- an unrelated insert succeeds; a retroactive prerequisite on a running
      task fails closed instead of corrupting active execution.
  13. Founder Correction -- a changed goal.original_instruction invalidates a stale
      authority hash.
  14. Approval Gate -- an inserted task still goes through the goal's named approval policy;
      insertion itself grants no execution bypass.
  15. Interruption/Resume -- a crash before the caller's commit leaves zero partial state; a
      retried call with the same idempotency key then succeeds cleanly.
  16. Supervisor Compatibility -- the Scoped Development Supervisor's own candidate discovery
      sees the newly inserted task as ordinary MainAI child work (it does NOT generate it).

Security tests (SECURITY section, bottom of file): no provider dependency; provider outage
does not break deterministic insertion; no shell/filesystem/deploy/production code path; real
RLS owner isolation; fail-closed on non-authoritative/unknown authority kinds; fail-closed on
dangling/self dependencies; insertion never auto-grants approval; no source-type-based
self-work approval exemption.

Real local Postgres (RLS included), same conventions as test_mainai_execution_planner.py."""

import hashlib
import threading
import uuid
from datetime import datetime

import pytest
from sqlalchemy import text as sa_text

from app.development_supervisor.service import SupervisorBounds, SupervisorScope, discover_candidates
from app.mainai_execution import approval, lessons, planner
from app.mainai_execution.approval import ApprovalRequiredError
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.plan_insertion import (
    ExistingTaskDependencyEdge,
    InsertedTaskSpec,
    PlanInsertionAuthorityError,
    PlanInsertionConflictError,
    PlanInsertionValidationError,
    insert_plan_tasks,
)
from app.models.mainai_execution import (
    EngineeringLessonSeverity,
    MainAIGoal,
    MainAIPlan,
    MainAITask,
    MainAITaskDependency,
    MainAITaskStatus,
)
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_privilege_policy_before_this_module():
    """Same ordering-trap closure as test_mainai_execution_planner.py's own identical fixture
    -- this module's writes must not depend on some OTHER test module having already applied
    app/rls.py's apply_mainai_execution_privileges() first."""
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
    goal = planner.create_goal(db_session, owner_id=owner_id, title="Test goal", original_instruction=instruction, created_by="test")
    if approval_policy is not None:
        goal.approval_policy = approval_policy
    if task_specs is None:
        task_specs = [
            PlannedTaskSpec(description="A", task_type="read_only_audit"),
            PlannedTaskSpec(description="B", task_type="read_only_audit"),
        ]
    plan = planner.create_plan(db_session, goal=goal, rationale="base plan", tasks=task_specs, created_by="test")
    db_session.commit()
    return goal, plan


def _auth_hash(goal) -> str:
    return hashlib.sha256(goal.original_instruction.encode()).hexdigest()


def _tasks(db_session, plan_id):
    return db_session.query(MainAITask).filter(MainAITask.plan_id == plan_id).order_by(MainAITask.created_at).all()


# ---------------------------------------------------------------- 1. Basic Insert


def test_basic_insert_preserves_existing_siblings_and_adds_the_new_task(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, b = _tasks(db_session, plan.id)
    a_id, b_id, a_status, b_status = a.id, b.id, a.status, b.status

    inserted = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="basic-insert-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="add C",
        requested_by="test",
    )
    db_session.commit()

    assert len(inserted) == 1
    c = inserted[0]
    assert c.plan_id == plan.id
    assert c.goal_id == goal.id

    db_session.refresh(a)
    db_session.refresh(b)
    assert a.id == a_id and a.status == a_status
    assert b.id == b_id and b.status == b_status
    assert db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count() == 3


# ---------------------------------------------------------------- 2. Insert Prerequisite


def test_insert_prerequisite_between_existing_tasks_is_purely_additive(db_session, owner_id):
    goal, plan = _goal_and_plan(
        db_session,
        owner_id,
        task_specs=[
            PlannedTaskSpec(description="A", task_type="read_only_audit"),
            PlannedTaskSpec(description="B", task_type="read_only_audit", depends_on=[0]),
        ],
    )
    a, b = _tasks(db_session, plan.id)
    b_id = b.id

    inserted = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="prereq-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit", depends_on=[a.id])],
        existing_task_dependencies=[ExistingTaskDependencyEdge(existing_task_id=b.id, depends_on_new_task_index=0)],
        source_type="founder_action",
        source_ref="test",
        reason="insert prerequisite between A and B",
        requested_by="test",
    )
    db_session.commit()
    c = inserted[0]

    db_session.refresh(b)
    assert b.id == b_id  # never recreated

    b_deps = {row.depends_on_task_id for row in db_session.query(MainAITaskDependency).filter(MainAITaskDependency.task_id == b.id).all()}
    assert b_deps == {a.id, c.id}  # additive: original A edge kept, new C edge added

    c_deps = {row.depends_on_task_id for row in db_session.query(MainAITaskDependency).filter(MainAITaskDependency.task_id == c.id).all()}
    assert c_deps == {a.id}


# ---------------------------------------------------------------- 3. Multi-task Atomic Insert


def test_multi_task_atomic_insert_with_a_new_to_new_dependency(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)

    c, d = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="multi-1",
        tasks=[
            InsertedTaskSpec(description="C", task_type="read_only_audit"),
            InsertedTaskSpec(description="D", task_type="read_only_audit", depends_on=[0]),
        ],
        source_type="founder_action",
        source_ref="test",
        reason="C then D",
        requested_by="test",
    )
    db_session.commit()

    dep = db_session.query(MainAITaskDependency).filter(MainAITaskDependency.task_id == d.id).one()
    assert dep.depends_on_task_id == c.id


# ---------------------------------------------------------------- 4. Invalid Second Task


def test_invalid_second_task_in_a_multi_task_insert_voids_the_whole_batch(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    before = db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count()

    with pytest.raises(PlanInsertionValidationError):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind="founder_requirement",
            authorized_instruction_sha256=_auth_hash(goal),
            idempotency_key="invalid-2nd",
            tasks=[
                InsertedTaskSpec(description="C", task_type="read_only_audit"),
                InsertedTaskSpec(description="D", task_type="not_a_real_task_type"),
            ],
            source_type="founder_action",
            source_ref="test",
            reason="bad second spec",
            requested_by="test",
        )
    db_session.rollback()
    assert db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count() == before


# ---------------------------------------------------------------- 5. Cycle Attack


def test_cycle_attack_via_new_task_depending_on_existing_which_gains_a_reverse_edge_is_rejected(db_session, owner_id):
    goal, plan = _goal_and_plan(
        db_session,
        owner_id,
        task_specs=[
            PlannedTaskSpec(description="A", task_type="read_only_audit"),
            PlannedTaskSpec(description="B", task_type="read_only_audit", depends_on=[0]),
        ],
    )
    a, b = _tasks(db_session, plan.id)
    assert b.status == MainAITaskStatus.pending  # still waiting on A -- eligible for a new edge

    before_tasks = db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count()
    before_deps = db_session.query(MainAITaskDependency).count()

    with pytest.raises(PlanInsertionValidationError, match="cycle"):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind="founder_requirement",
            authorized_instruction_sha256=_auth_hash(goal),
            idempotency_key="cycle-1",
            tasks=[InsertedTaskSpec(description="X", task_type="read_only_audit", depends_on=[b.id])],
            existing_task_dependencies=[ExistingTaskDependencyEdge(existing_task_id=b.id, depends_on_new_task_index=0)],
            source_type="founder_action",
            source_ref="test",
            reason="cycle attempt X->B->X",
            requested_by="test",
        )
    db_session.rollback()

    assert db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count() == before_tasks
    assert db_session.query(MainAITaskDependency).count() == before_deps


# ---------------------------------------------------------------- 6. Replay


def test_identical_replay_returns_the_same_canonical_tasks_with_no_duplicates(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    call = dict(
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="replay-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="replay test",
        requested_by="test",
    )

    first = insert_plan_tasks(db_session, **call)
    db_session.commit()
    second = insert_plan_tasks(db_session, **call)
    db_session.commit()

    assert [t.id for t in first] == [t.id for t in second]
    count = db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id, MainAITask.description == "C").count()
    assert count == 1


# ---------------------------------------------------------------- 7. Conflicting Replay


def test_conflicting_replay_with_the_same_key_but_different_content_fails_closed(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="conflict-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="first use of this key",
        requested_by="test",
    )
    db_session.commit()

    with pytest.raises(PlanInsertionConflictError):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind="founder_requirement",
            authorized_instruction_sha256=_auth_hash(goal),
            idempotency_key="conflict-1",
            tasks=[InsertedTaskSpec(description="Something entirely different", task_type="read_only_audit")],
            source_type="founder_action",
            source_ref="test",
            reason="second, semantically different use of the same key",
            requested_by="test",
        )
    db_session.rollback()


# ---------------------------------------------------------------- 8. Concurrent Insert


def test_concurrent_insert_with_the_same_idempotency_key_produces_exactly_one_canonical_insertion(db_session, owner_id, superuser_db):
    from app.db import SessionLocal

    goal, plan = _goal_and_plan(db_session, owner_id)
    goal_id, plan_id = goal.id, plan.id
    auth_hash = _auth_hash(goal)
    key = f"concurrent-{uuid.uuid4()}"

    results: list[list[uuid.UUID]] = []
    errors: list[str] = []
    barrier = threading.Barrier(2, timeout=5)

    def _worker():
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            g = session.get(MainAIGoal, goal_id)
            p = session.get(MainAIPlan, plan_id)
            barrier.wait()
            inserted = insert_plan_tasks(
                session,
                goal=g,
                plan=p,
                authority_kind="founder_requirement",
                authorized_instruction_sha256=auth_hash,
                idempotency_key=key,
                tasks=[InsertedTaskSpec(description="Concurrent C", task_type="read_only_audit")],
                source_type="founder_action",
                source_ref="test",
                reason="race",
                requested_by="test",
            )
            session.commit()
            results.append([t.id for t in inserted])
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
    assert results[0] == results[1], "both callers must get back the SAME canonical task id(s)"

    row_count = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_tasks WHERE plan_id = :p AND description = 'Concurrent C'"), {"p": str(plan_id)}
    ).scalar()
    assert row_count == 1, "exactly one row, no duplicate created by the race"


# ---------------------------------------------------------------- 9. Cross-Owner


def test_cross_owner_dependency_reference_is_rejected(db_session, superuser_db, owner_id, make_verified_user):
    other_user, _password = make_verified_user()
    goal, plan = _goal_and_plan(db_session, owner_id)

    other_goal = planner.create_goal(superuser_db, owner_id=other_user.id, title="Other owner goal", original_instruction="Other owner's work.", created_by="test")
    other_plan = planner.create_plan(
        superuser_db, goal=other_goal, rationale="other owner's plan", tasks=[PlannedTaskSpec(description="Other task", task_type="read_only_audit")], created_by="test"
    )
    superuser_db.commit()
    other_task_id = superuser_db.query(MainAITask).filter(MainAITask.plan_id == other_plan.id).one().id

    with pytest.raises(PlanInsertionValidationError):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind="founder_requirement",
            authorized_instruction_sha256=_auth_hash(goal),
            idempotency_key="cross-owner-1",
            tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit", depends_on=[other_task_id])],
            source_type="founder_action",
            source_ref="test",
            reason="cross owner dependency attempt",
            requested_by="test",
        )
    db_session.rollback()
    assert db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id, MainAITask.description == "C").count() == 0


# ---------------------------------------------------------------- 10. Completed Sibling


def test_insert_beside_a_completed_sibling_leaves_it_unchanged(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    a.status = MainAITaskStatus.completed
    a.completed_at = datetime.utcnow()
    db_session.commit()

    insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="completed-sibling-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="insert beside a completed task",
        requested_by="test",
    )
    db_session.commit()

    db_session.refresh(a)
    assert a.status == MainAITaskStatus.completed


# ---------------------------------------------------------------- 11. Waiting Sibling


def test_waiting_sibling_remains_waiting_while_an_independent_task_is_still_inserted(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    a.status = MainAITaskStatus.waiting_external
    db_session.commit()

    insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="waiting-sibling-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="independent insert beside a waiting task",
        requested_by="test",
    )
    db_session.commit()

    db_session.refresh(a)
    assert a.status == MainAITaskStatus.waiting_external


def test_waiting_sibling_cannot_receive_a_new_retroactive_prerequisite(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    a.status = MainAITaskStatus.waiting_ci
    db_session.commit()

    with pytest.raises(PlanInsertionValidationError):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind="founder_requirement",
            authorized_instruction_sha256=_auth_hash(goal),
            idempotency_key="waiting-prereq-1",
            tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
            existing_task_dependencies=[ExistingTaskDependencyEdge(existing_task_id=a.id, depends_on_new_task_index=0)],
            source_type="founder_action",
            source_ref="test",
            reason="attempt retroactive prerequisite on a waiting task",
            requested_by="test",
        )
    db_session.rollback()


# ---------------------------------------------------------------- 12. Running Sibling


def test_running_sibling_unaffected_insert_succeeds_but_invalidating_prerequisite_fails_closed(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    a, _b = _tasks(db_session, plan.id)
    a.status = MainAITaskStatus.running
    db_session.commit()

    insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="running-ok-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="unrelated insert beside a running task",
        requested_by="test",
    )
    db_session.commit()
    db_session.refresh(a)
    assert a.status == MainAITaskStatus.running

    with pytest.raises(PlanInsertionValidationError):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind="founder_requirement",
            authorized_instruction_sha256=_auth_hash(goal),
            idempotency_key="running-invalidate-1",
            tasks=[InsertedTaskSpec(description="D", task_type="read_only_audit")],
            existing_task_dependencies=[ExistingTaskDependencyEdge(existing_task_id=a.id, depends_on_new_task_index=0)],
            source_type="founder_action",
            source_ref="test",
            reason="attempt to retroactively gate a running task",
            requested_by="test",
        )
    db_session.rollback()
    db_session.refresh(a)
    assert a.status == MainAITaskStatus.running


# ---------------------------------------------------------------- 13. Founder Correction


def test_founder_correction_invalidates_a_stale_authority_hash(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    stale_hash = _auth_hash(goal)

    goal.original_instruction = "Do something completely different now -- founder correction."
    db_session.commit()

    with pytest.raises(PlanInsertionAuthorityError):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind="founder_requirement",
            authorized_instruction_sha256=stale_hash,
            idempotency_key="stale-auth-1",
            tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
            source_type="founder_action",
            source_ref="test",
            reason="insertion authorized under a now-superseded instruction",
            requested_by="test",
        )
    db_session.rollback()


# ---------------------------------------------------------------- 14. Approval Gate


def test_inserted_task_still_requires_the_goals_named_approval_policy(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id, approval_policy="autonomous_development_work")

    inserted = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="approval-gate-1",
        tasks=[InsertedTaskSpec(description="Edit the repo", task_type="repo_edit")],
        source_type="founder_action",
        source_ref="test",
        reason="inserted repo edit",
        requested_by="test",
    )
    db_session.commit()
    new_task = inserted[0]

    with pytest.raises(ApprovalRequiredError):
        approval.require_task_approval(db_session, task=new_task, goal_approval_policy=goal.approval_policy)

    approval.grant_task_approval(db_session, task=new_task, approved_by="founder")
    db_session.commit()
    approval.require_task_approval(db_session, task=new_task, goal_approval_policy=goal.approval_policy)  # no longer raises


# ---------------------------------------------------------------- 15. Interruption/Resume


def test_interruption_before_commit_leaves_no_partial_state_and_resume_succeeds(db_session, owner_id, superuser_db):
    goal, plan = _goal_and_plan(db_session, owner_id)
    call = dict(
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="interrupt-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="about to be interrupted",
        requested_by="test",
    )

    insert_plan_tasks(db_session, **call)
    # Simulate a crash BEFORE the caller's commit -- flush() sent statements to Postgres but
    # nothing is durable, and the advisory lock releases automatically with the rollback.
    db_session.rollback()

    count = superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_tasks WHERE plan_id = :p AND description = 'C'"), {"p": str(plan.id)}
    ).scalar()
    assert count == 0, "an interrupted insertion must leave zero rows, never a partial task/dependency state"

    # Resume: an identical retried call is a fresh insertion (nothing landed before), and must
    # succeed cleanly -- not be mistaken for a replay of something that never actually committed.
    retried = insert_plan_tasks(db_session, **call)
    db_session.commit()
    assert len(retried) == 1
    assert retried[0].description == "C"


# ---------------------------------------------------------------- 16. Supervisor Compatibility


def test_supervisor_candidate_discovery_sees_a_newly_inserted_task_as_ordinary_child_work(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)

    inserted = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="supervisor-visibility-1",
        tasks=[InsertedTaskSpec(description="Newly inserted work", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="supervisor visibility check",
        requested_by="test",
    )
    db_session.commit()
    new_task = inserted[0]

    scope = SupervisorScope(
        owner_id=owner_id,
        goal_id=goal.id,
        authority_kind="founder_requirement",
        authority_ref="test",
        authorized_instruction_sha256=_auth_hash(goal),
        repository_identity="lifeai",
        allowed_paths=("backend/app",),
        allowed_capabilities=("read_only_audit",),
    )
    candidates = discover_candidates(db_session, scope=scope, bindings=[], bounds=SupervisorBounds())

    assert any(c.task_id == new_task.id for c in candidates), (
        "the Supervisor's own candidate discovery must see the inserted task as ordinary MainAI "
        "child work -- this test proves visibility only, it must NOT prove or require the "
        "Supervisor to have generated the task itself"
    )


# ================================================================== SECURITY


def test_security_no_provider_dependency_insertion_succeeds_with_no_provider_call_anywhere(db_session, owner_id):
    """insert_plan_tasks() is 100% deterministic -- proven by construction (no provider call
    exists anywhere in its own code path); this test is the behavioral confirmation."""
    goal, plan = _goal_and_plan(db_session, owner_id)
    inserted = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="no-provider-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="no provider configured",
        requested_by="test",
    )
    assert len(inserted) == 1


@pytest.mark.asyncio
async def test_security_provider_outage_does_not_break_deterministic_insertion(db_session, owner_id, monkeypatch):
    from app.providers.base import ProviderError
    from app.providers.openai_provider import OpenAIProvider

    async def _always_fails(self, *args, **kwargs):
        raise ProviderError("simulated total provider outage", category="rate_limited")

    monkeypatch.setattr(OpenAIProvider, "chat", _always_fails)

    goal, plan = _goal_and_plan(db_session, owner_id)
    inserted = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="provider-outage-1",
        tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
        source_type="founder_action",
        source_ref="test",
        reason="provider is completely down",
        requested_by="test",
    )
    db_session.commit()
    assert len(inserted) == 1


def test_security_module_source_has_no_shell_filesystem_or_deploy_production_path():
    import inspect

    import app.mainai_execution.plan_insertion as plan_insertion_module

    source = inspect.getsource(plan_insertion_module)
    forbidden = ["subprocess", "os.system", "os.popen", "shutil.", "merge_pull_request", "kubectl", "\ndeploy(", "docker "]
    for token in forbidden:
        assert token not in source, f"plan_insertion.py must never reference {token!r}"


def test_security_owner_isolation_rls_hides_another_owners_plan_tasks(db_session, owner_id, make_verified_user):
    from app.db import SessionLocal

    other_user, _password = make_verified_user()
    goal, plan = _goal_and_plan(db_session, owner_id)
    plan_id = plan.id

    other_session = SessionLocal()
    try:
        _set_rls_user(other_session, other_user.id)
        visible = other_session.execute(sa_text("SELECT count(*) FROM mainai_tasks WHERE plan_id = :p"), {"p": str(plan_id)}).scalar()
        assert visible == 0, "RLS must hide another owner's plan tasks entirely, not just from the insertion primitive"
    finally:
        other_session.close()


@pytest.mark.parametrize("bad_kind", ["idea", "suggestion", "hypothesis", "ai_interpretation", "unknown", "founder_preference", "not_a_real_kind"])
def test_security_fail_closed_on_non_authoritative_or_unknown_authority_kind(db_session, owner_id, bad_kind):
    goal, plan = _goal_and_plan(db_session, owner_id)
    before = db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count()

    with pytest.raises(PlanInsertionAuthorityError):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind=bad_kind,
            authorized_instruction_sha256=_auth_hash(goal),
            idempotency_key=f"bad-authority-{bad_kind}",
            tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit")],
            source_type="founder_action",
            source_ref="test",
            reason="insufficient authority",
            requested_by="test",
        )
    db_session.rollback()
    assert db_session.query(MainAITask).filter(MainAITask.plan_id == plan.id).count() == before


def test_security_fail_closed_on_a_dangling_dependency(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    with pytest.raises(PlanInsertionValidationError):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind="founder_requirement",
            authorized_instruction_sha256=_auth_hash(goal),
            idempotency_key="dangling-dep-1",
            tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit", depends_on=[uuid.uuid4()])],
            source_type="founder_action",
            source_ref="test",
            reason="dangling dependency",
            requested_by="test",
        )
    db_session.rollback()


def test_security_fail_closed_on_a_self_dependency(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id)
    with pytest.raises(PlanInsertionValidationError):
        insert_plan_tasks(
            db_session,
            goal=goal,
            plan=plan,
            authority_kind="founder_requirement",
            authorized_instruction_sha256=_auth_hash(goal),
            idempotency_key="self-dep-1",
            tasks=[InsertedTaskSpec(description="C", task_type="read_only_audit", depends_on=[0])],
            source_type="founder_action",
            source_ref="test",
            reason="self dependency",
            requested_by="test",
        )
    db_session.rollback()


def test_security_insertion_never_auto_grants_approval(db_session, owner_id):
    goal, plan = _goal_and_plan(db_session, owner_id, approval_policy="autonomous_development_work")

    inserted = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="no-auto-approval-1",
        tasks=[InsertedTaskSpec(description="Edit", task_type="repo_edit")],
        source_type="founder_action",
        source_ref="test",
        reason="no bypass",
        requested_by="test",
    )
    db_session.commit()
    new_task = inserted[0]

    granted = db_session.execute(
        sa_text("SELECT count(*) FROM mainai_task_events WHERE task_id = :id AND event_type = 'approval_granted'"),
        {"id": str(new_task.id)},
    ).scalar()
    assert granted == 0


def test_security_no_self_work_bypass_regardless_of_source_type(db_session, owner_id):
    """The primitive has no concept of 'self-proposed work' that skips approval -- the
    approval gate (app/mainai_execution/approval.py) only ever looks at task_type + the goal's
    named policy, never at source_type/requested_by, so an AI-labelled source cannot grant
    itself an exemption a founder-labelled source wouldn't also get."""
    goal, plan = _goal_and_plan(db_session, owner_id, approval_policy="autonomous_development_work")

    inserted = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="no-self-work-bypass-1",
        tasks=[InsertedTaskSpec(description="Edit", task_type="repo_edit")],
        source_type="ai_self_proposed",
        source_ref="internal-agent",
        reason="self-proposed insertion",
        requested_by="mainai-agent",
    )
    db_session.commit()
    new_task = inserted[0]

    with pytest.raises(ApprovalRequiredError):
        approval.require_task_approval(db_session, task=new_task, goal_approval_policy=goal.approval_policy)


def test_insert_applies_active_lessons_like_create_plan(db_session, owner_id):
    """Partial insertion must not bypass lesson binding that create_plan already applies.
    A repo_edit insert with an empty verification_plan still inherits the owner's active
    regression lesson and records lessons_applied on the created event."""
    lesson = lessons.record_lesson(
        db_session,
        problem="A repo_edit task once shipped without running its own regression test.",
        root_cause="Planner did not attach a targeted_tests step for repo_edit by default.",
        affected_component="mainai_execution.plan_insertion",
        severity=EngineeringLessonSeverity.medium,
        evidence="Regression for insert_plan_tasks lesson bypass.",
        fix="Always attach the relevant regression test to repo_edit tasks touching this area.",
        general_rule="A repo_edit task must always verify itself with a real targeted test.",
        applies_to=["repo_edit"],
        source_type="branch_registry_pass",
        source_ref="plan_insertion lesson binding",
        created_by="test",
        first_seen_at=datetime.utcnow(),
        regression_test="tests/backend/test_lesson_regression.py",
    )
    db_session.commit()

    goal, plan = _goal_and_plan(db_session, owner_id)

    inserted = insert_plan_tasks(
        db_session,
        goal=goal,
        plan=plan,
        authority_kind="founder_requirement",
        authorized_instruction_sha256=_auth_hash(goal),
        idempotency_key="lesson-binding-insert-1",
        tasks=[InsertedTaskSpec(description="Edit a file", task_type="repo_edit")],
        source_type="founder_action",
        source_ref="test",
        reason="prove lesson binding on insertion",
        requested_by="test",
    )
    db_session.commit()

    new_task = inserted[0]
    assert {"kind": "targeted_tests", "target": "tests/backend/test_lesson_regression.py"} in new_task.verification_plan

    created_event = db_session.execute(
        sa_text("SELECT detail FROM mainai_task_events WHERE task_id = :t AND event_type = 'created'"),
        {"t": str(new_task.id)},
    ).scalar_one()
    assert created_event["lessons_applied"] == [str(lesson.id)]
