"""V0.2 hardening finding: run_task_execution_job()'s two `record_checkpoint()` calls
(work_result, finalized) used to commit with NO lease re-verification, even though task-state/
verification writes were already correctly fenced (via the atomic commit they share with
mark_completed()/mark_failed()). A worker whose lease had already expired and been reclaimed
by someone else (`claim_next_mainai_job()`'s own real reclaim -- same job_id, bumped
lease_generation, exactly as it already does for every mainai_jobs row) could still land a
checkpoint write for a job it no longer owns. This module proves the fix: a checkpoint write
attempted under a STALE (worker_id, lease_generation) is rejected before anything durable
lands, while the CURRENT legitimate claimant's own checkpoint write still succeeds normally."""

import pytest
from sqlalchemy import text as sa_text

from app.jobs.mainai_job_lease import claim_next_mainai_job
from app.mainai_execution import executor, planner
from app.mainai_execution.checkpoint import latest_checkpoint_for_step
from app.mainai_execution.execution_job import run_task_execution_job
from app.models.mainai_execution import MainAITask
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _apply_privilege_policy_before_this_module():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _password = make_verified_user()
    _set_rls_user(db_session, user.id)
    return user.id


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


@pytest.mark.asyncio
async def test_stale_worker_cannot_land_a_work_result_checkpoint_after_being_reclaimed(db_session, superuser_db, owner_id, monkeypatch):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("Analysen visar inga problem."))

    goal = planner.create_goal(db_session, owner_id=owner_id, title="Fencing test goal", original_instruction="Audit something.", created_by="test")
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    # worker-1 claims it first (generation 1).
    job_id, _owner, stale_generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    assert stale_generation == 1

    # Simulate the exact staleness scenario this fix protects against. Since migration 0034,
    # `claim_next_mainai_job()` no longer blind-reclaims an expired-lease `task_execution` job
    # itself (see that function's own docstring/SQL) -- a dead one only ever gets a fresh
    # replacement job through app/mainai_execution/recovery_takeover.py, never a same-job_id
    # reclaim. This UPDATE stands in for "some mechanism legitimately took over this exact
    # row" (a manual operator override, or a future change to that policy) so this test still
    # proves the underlying invariant: whoever holds the CURRENT lease_generation is the only
    # one whose writes may land, regardless of how that generation came to be current.
    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET locked_by = 'worker-2', lease_generation = lease_generation + 1 WHERE id = :id"),
        {"id": str(job_id)},
    )
    superuser_db.commit()
    new_generation = stale_generation + 1

    _set_rls_user(db_session, owner_id)

    # worker-1 (stale, still believes it holds generation 1) now tries to actually run the job.
    await run_task_execution_job(db_session, job_id, owner_id, worker_id="worker-1", lease_generation=stale_generation, lease_seconds=120)

    # Nothing durable was written by the stale attempt: no work_result checkpoint exists yet.
    assert latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_id, step="work_result") is None
    db_session.refresh(task)
    from app.models.mainai_execution import MainAITaskStatus

    assert task.status == MainAITaskStatus.running  # untouched -- the stale attempt made zero progress

    # worker-2 (the real current claimant) can still successfully run and checkpoint the SAME job.
    await run_task_execution_job(db_session, job_id, owner_id, worker_id="worker-2", lease_generation=new_generation, lease_seconds=120)
    checkpoint = latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_id, step="work_result")
    assert checkpoint is not None
    db_session.refresh(task)
    assert task.status == MainAITaskStatus.completed


def _git(args, cwd):
    import subprocess

    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _shared_checkout(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    _git(["init", "-q", "."], shared)
    _git(["config", "user.email", "test@lifeai.invalid"], shared)
    _git(["config", "user.name", "Test"], shared)
    (shared / "README.md").write_text("ORIGINAL CONTENT\n", encoding="utf-8")
    _git(["add", "-A"], shared)
    _git(["commit", "-q", "-m", "base"], shared)
    return shared


def _fake_chat_with_concurrent_reclaim(response_text: str, *, job_id, superuser_db):
    """Models PR #478's named gap concretely: the lease is reclaimed by a second worker WHILE
    this provider call is in flight -- not before it starts (already covered by the stale-
    worker test above) and not after the whole task finishes, but between the call starting and
    its result being used. The reclaim happens as a side effect of the fake call itself, which
    is as close to genuine concurrency as a single-threaded synchronous test can model: by the
    time this coroutine returns, a concurrent takeover has already landed in the database."""

    async def _chat(self, messages, model, **kwargs):
        superuser_db.execute(
            sa_text("UPDATE mainai_jobs SET locked_by = 'worker-2', lease_generation = lease_generation + 1 WHERE id = :id"),
            {"id": str(job_id)},
        )
        superuser_db.commit()
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _dispatch_repo_edit(db_session, owner_id, *, repo_root):
    from app.mainai_execution import execution_job

    goal = planner.create_goal(
        db_session, owner_id=owner_id, title="Lease-revocation-in-flight test",
        original_instruction="Propose an edit.", created_by="test", approval_policy="standard_repo_work",
    )
    planner.create_plan(
        db_session, goal=goal, rationale="single repo_edit",
        tasks=[planner.PlannedTaskSpec(description="propose an edit", task_type="repo_edit")],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()
    return execution_job, task, job


@pytest.mark.asyncio
async def test_repo_edit_lease_revoked_while_ai_call_in_flight_rolls_back_shared_checkout(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """Stream 7, item 1 (PR #478's named gap): with proposal-worktree isolation disabled --
    the one configuration where the AI's output is written straight into the shared checkout --
    a lease reclaimed by another worker DURING the chat_with_fallback() call must never leave
    that write behind. Before this fix, the only `_rollback_proposal_writes()` call site was
    gated on a failed verification, which this path never reaches: the function returns as soon
    as the post-call lease re-check fails, before verification ever runs."""
    import json

    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "mainai_proposal_worktree_isolation", False)
    shared = _shared_checkout(tmp_path)

    execution_job, task, job = _dispatch_repo_edit(db_session, owner_id, repo_root=shared)
    monkeypatch.setattr(execution_job, "_repo_root", lambda: shared)

    job_id, _owner, stale_generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    proposal = json.dumps(
        {"files": [{"path": "README.md", "content": "PROPOSED BY A STALE WORKER\n"}], "commit_message": "proposal"}
    )
    monkeypatch.setattr(
        OpenAIProvider, "chat",
        _fake_chat_with_concurrent_reclaim(proposal, job_id=job_id, superuser_db=superuser_db),
    )

    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job_id, owner_id, worker_id="worker-1", lease_generation=stale_generation, lease_seconds=120)

    # The write happened (the fake call's reclaim side effect runs before chat_with_fallback
    # returns, so the file write at line ~502 of execution_job.py still executes) -- the whole
    # point of this test is that it must not survive the lease check that follows it.
    assert (shared / "README.md").read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"
    assert latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_id, step="work_result") is None
    db_session.refresh(task)
    from app.models.mainai_execution import MainAITaskStatus

    assert task.status == MainAITaskStatus.running


@pytest.mark.asyncio
async def test_repo_edit_lease_revoked_while_ai_call_in_flight_is_harmless_under_default_isolation(
    db_session, superuser_db, owner_id, monkeypatch, tmp_path
):
    """Same scenario, default configuration (worktree isolation ON): the shared checkout must
    never be touched at all regardless of the lease outcome, since the proposal write lands in
    a disposable per-attempt checkout, not the tree this backend process runs from."""
    import json

    from app.mainai_execution import proposal_worktree

    monkeypatch.setattr(proposal_worktree, "PROPOSAL_ROOT", tmp_path / "proposal-root")
    shared = _shared_checkout(tmp_path)
    original = (shared / "README.md").read_bytes()

    execution_job, task, job = _dispatch_repo_edit(db_session, owner_id, repo_root=shared)
    monkeypatch.setattr(execution_job, "_repo_root", lambda: shared)

    job_id, _owner, stale_generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    proposal = json.dumps(
        {"files": [{"path": "README.md", "content": "PROPOSED BY A STALE WORKER\n"}], "commit_message": "proposal"}
    )
    monkeypatch.setattr(
        OpenAIProvider, "chat",
        _fake_chat_with_concurrent_reclaim(proposal, job_id=job_id, superuser_db=superuser_db),
    )

    _set_rls_user(db_session, owner_id)
    await run_task_execution_job(db_session, job_id, owner_id, worker_id="worker-1", lease_generation=stale_generation, lease_seconds=120)

    assert (shared / "README.md").read_bytes() == original
    assert latest_checkpoint_for_step(db_session, task_id=task.id, job_id=job_id, step="work_result") is None
    db_session.refresh(task)
    from app.models.mainai_execution import MainAITaskStatus

    assert task.status == MainAITaskStatus.running


def test_claim_next_mainai_job_never_blind_reclaims_a_dead_task_execution_job(db_session, superuser_db, owner_id):
    """The actual SQL invariant migration 0034 establishes: once a `task_execution` job's
    lease expires, `claim_next_mainai_job()` must never hand it back out on its own --
    only app/mainai_execution/recovery_takeover.py's explicit inspect->classify->salvage gate
    may ever act on it. A `queued` task_execution job must still be claimable normally (the
    exclusion is scoped to the expired-lease reclaim branch only, never fresh dispatch)."""
    goal = planner.create_goal(db_session, owner_id=owner_id, title="Reclaim exclusion test goal", original_instruction="x", created_by="test")
    planner.create_plan(
        db_session, goal=goal, rationale="single task",
        tasks=[planner.PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])],
        created_by="test",
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.commit()

    job_id, _owner, generation = claim_next_mainai_job(superuser_db, "worker-1", 120)
    assert job_id == job.id
    assert generation == 1

    superuser_db.execute(sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"), {"id": str(job_id)})
    superuser_db.commit()

    # Nothing else is claimable in this test's isolated owner scope -- a dead task_execution
    # job with an expired lease must be structurally invisible to this query, not merely
    # deprioritized.
    assert claim_next_mainai_job(superuser_db, "worker-2", 120) is None

    row = superuser_db.execute(sa_text("SELECT status, locked_by, lease_generation FROM mainai_jobs WHERE id = :id"), {"id": str(job_id)}).one()
    assert row[0] == "running"
    assert row[1] == "worker-1"
    assert row[2] == 1  # completely untouched by the failed reclaim attempt
