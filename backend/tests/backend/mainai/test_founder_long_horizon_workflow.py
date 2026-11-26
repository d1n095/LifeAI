"""FAS 5 — the founder's long-horizon workflow, composed over the systems that already own it.

These are integration tests of a *journey*, not of a helper. Every scenario drives the real
Postgres schema (RLS enabled, restricted runtime role), the real jobs lease/claim and recovery
pipeline, and the real verification/final-report code. The only faked boundary is the LLM
transport itself (`OpenAIProvider.chat`), which no test in this repo ever calls for real except
the dedicated Ollama acceptance modules; that is stated again in each scenario that needs it.

Reused instead of reimplemented:
  - `management.create_task`      — the founder's own task register (duplicate guard reused)
  - `planner.create_goal/create_plan` — the durable goal/plan/task graph
  - `executor.dispatch_ready_task` + `app.jobs` lease/claim — the one dispatch path
  - `run_task_execution_job`      — the one execution + verification gate
  - `recovery_inspector/classifier/takeover` — the one dead-agent recovery pipeline
  - `final_report` + `project_status` + `founder_memory.prompt` — the founder's read surface

Scenarios:
  A  A project continues in the next session: real work completes, the dependent step is ready,
     a fresh session sees the same durable state, and a retried mission does not duplicate.
  B  The model stops answering: the instruction survives, nothing is reported complete, and
     there is exactly one execution attempt -- never a second parallel job.
  C  A worker dies mid-task: only the recovery pipeline may take the work over, the dead
     worker's lease renewal is fenced off, and exactly one live job remains.
  D  Work that is already done is reused: an identical task and an identically worded mission
     both return the existing durable row instead of creating new work.
  E  A mission needs approval: planning may exist, but dispatch is structurally refused until
     a real approval event exists, and only then does exactly one job appear.
  F  Contradictory founder instructions: the current decision is shown, the superseded one is
     not, and an unresolved contradiction is stated as disputed instead of silently resolved.
  G  A false work report is impossible from durable state: a finished attempt whose verification
     failed is reported as failed/retryable, never as a completed task or a closed goal.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select, text as sa_text

from app.founder_memory import mark_founder_memory_disputed, record_founder_memory
from app.founder_memory.prompt import format_founder_memory_instructions
from app.jobs.mainai_job_lease import (
    JobLeaseLostError,
    claim_next_mainai_job,
    renew_mainai_job_lease,
)
from app.mainai_execution import executor, planner
from app.mainai_execution.approval import ApprovalRequiredError, grant_task_approval
from app.mainai_execution.execution_job import run_task_execution_job
from app.mainai_execution.final_report import generate_goal_report, record_final_report
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.recovery_classifier import classify_recovery_record
from app.mainai_execution.recovery_inspector import get_or_create_recovery_record, inspect_recovery_record
from app.mainai_execution.recovery_takeover import execute_takeover
from app.models.mainai_execution import (
    MainAIGoal,
    MainAIGoalStatus,
    MainAIPlan,
    MainAITask,
    MainAITaskStatus,
)
from app.models.mainai_job import MainAIJob, MainAIJobErrorCategory, MainAIJobStatus
from app.models.mainai_recovery import AUTO_SALVAGEABLE_CLASSIFICATIONS
from app.models.project import Project, ProjectStatus, TaskPriority, TaskStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider
from app.project_status.service import build_project_status, format_project_status_instructions
from app.request_context import current_user_id as current_user_id_var
from app.work_management import service as work_management

MISSION = "Bygg klart rapportflödet i Founder Alpha och verifiera att det fungerar."


@pytest.fixture(autouse=True, scope="module")
def _apply_execution_and_job_privilege_policy_before_this_module():
    """Same ordering constraint every mainai execution/job module documents: both the
    mainai_tasks/events grants and the mainai_jobs runtime grants must exist before any test
    here dispatches a real job."""
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


@pytest.fixture
def project(db_session, owner_id):
    row = Project(name="Founder Alpha", created_by=owner_id, status=ProjectStatus.active)
    db_session.add(row)
    db_session.commit()
    return row


def _intake_goal(db, owner_id, *, project_id=None, instruction: str = MISSION, title: str = "Långsiktigt uppdrag"):
    """The founder's goal intake, exactly as POST /api/mainai/execution/goals calls it."""
    goal = planner.create_goal(
        db,
        owner_id=owner_id,
        title=title,
        original_instruction=instruction,
        created_by="founder",
        project_id=project_id,
        reuse_identical_active=True,
    )
    db.commit()
    return goal


def _plan(db, goal, specs: list[PlannedTaskSpec]):
    plan = planner.create_plan(db, goal=goal, rationale="integrationstest", tasks=specs, created_by="founder")
    db.commit()
    return plan


def _tasks(db, goal) -> list[MainAITask]:
    return list(
        db.execute(
            select(MainAITask).where(MainAITask.goal_id == goal.id).order_by(MainAITask.created_at, MainAITask.id)
        ).scalars()
    )


def _dispatch(db, task, goal) -> MainAIJob:
    job = executor.dispatch_ready_task(db, task=task, goal=goal, dispatched_by="test-worker")
    db.commit()
    return job


async def _run_job(db, job: MainAIJob, owner_id, *, worker_id: str = "worker-a") -> None:
    """Claim exactly this job's lease (the test DB is truncated per test, so it is the oldest
    claimable row) and run the real task-execution entry point."""
    from app.db import SessionLocal

    claim_db = SessionLocal()
    try:
        claimed = claim_next_mainai_job(claim_db, worker_id, 120)
    finally:
        claim_db.close()
    assert claimed is not None, "the dispatched job must be claimable"
    _job_id, _owner_id, generation = claimed
    assert _job_id == job.id
    _set_rls_user(db, owner_id)
    await run_task_execution_job(
        db, job.id, owner_id, worker_id=worker_id, lease_generation=generation, lease_seconds=120
    )


def _fake_chat(response_text: str):
    async def _chat(self, messages, model, **kwargs):
        return ChatResult(content=response_text, provider="openai", model=model, raw_usage={})

    return _chat


def _failing_chat(exc: Exception):
    async def _chat(self, messages, model, **kwargs):
        raise exc

    return _chat


def _job_count(superuser_db, owner_id) -> int:
    return superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o"), {"o": str(owner_id)}
    ).scalar()


def _founder(db):
    from app.models.user import User

    return db.query(User).filter(User.email == "founder@lifeos.local").first()


def _login(client) -> dict[str, str]:
    res = client.post(
        "/api/auth/login",
        json={"email": "founder@lifeos.local", "password": "TestFounderPassword123!"},
    )
    assert res.status_code == 200, res.text
    return {"X-CSRF-Token": res.json()["csrf_token"]}


# ---------------------------------------------------------------- retried intake over HTTP


def test_a_retried_goal_submit_and_a_retried_plan_request_cannot_duplicate_or_cancel_work(
    client, db_session, superuser_db, monkeypatch
):
    """The HTTP face of FAS 2 #5 / FAS 3's retry rules.

    Both requests are the kind an impatient double-click or a client retry after a timeout
    produces. The same mission must return the same durable goal; a second plan request must
    be refused before the paid AI call and before `create_plan()` can supersede the active
    plan and cancel a task a worker may already be running.
    """
    headers = _login(client)
    founder = _founder(superuser_db)
    payload = {
        "title": "Founder Alpha långsiktiga mål",
        "original_instruction": MISSION,
        "approval_policy": "standard_repo_work",
    }

    first = client.post("/api/mainai/execution/goals", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    again = client.post("/api/mainai/execution/goals", json=payload, headers=headers)
    assert again.status_code == 201, again.text
    assert again.json()["id"] == first.json()["id"]
    assert superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_goals WHERE owner_id = :o"), {"o": str(founder.id)}
    ).scalar() == 1

    _set_rls_user(db_session, founder.id)
    goal = db_session.get(MainAIGoal, uuid.UUID(first.json()["id"]))
    _plan(
        db_session,
        goal,
        [PlannedTaskSpec(description="Pågående steg", task_type="read_only_audit", verification_plan=[])],
    )
    (task,) = _tasks(db_session, goal)

    async def _must_not_be_called(self, messages, model, **kwargs):
        raise AssertionError("the AI planner must not be called for a refused retry")

    monkeypatch.setattr(OpenAIProvider, "chat", _must_not_be_called)
    refused = client.post(f"/api/mainai/execution/goals/{goal.id}/plan", headers=headers)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["error"] == "active_plan_exists"

    db_session.expire_all()
    task_after = db_session.get(MainAITask, task.id)
    assert task_after.status == MainAITaskStatus.ready, "the active plan's task must not be cancelled"
    assert len(db_session.execute(select(MainAIPlan).where(MainAIPlan.goal_id == goal.id)).scalars().all()) == 1


# ---------------------------------------------------------------- A. the project continues


@pytest.mark.asyncio
async def test_scenario_a_project_continues_in_the_next_session_and_a_retried_mission_is_reused(
    db_session, superuser_db, owner_id, project, monkeypatch
):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("Nuläget är sammanställt."))
    goal = _intake_goal(db_session, owner_id, project_id=project.id)
    _plan(
        db_session,
        goal,
        [
            PlannedTaskSpec(description="Sammanställ nuläget", task_type="read_only_audit", verification_plan=[]),
            PlannedTaskSpec(
                description="Skriv sammanfattning", task_type="read_only_audit", verification_plan=[], depends_on=[0]
            ),
        ],
    )
    tasks = {t.description: t for t in _tasks(db_session, goal)}
    first, second = tasks["Sammanställ nuläget"], tasks["Skriv sammanfattning"]
    assert second.status == MainAITaskStatus.pending
    assert first.status == MainAITaskStatus.ready

    job = _dispatch(db_session, first, goal)
    await _run_job(db_session, job, owner_id)

    assert superuser_db.get(MainAITask, first.id).status == MainAITaskStatus.completed
    # The dependent step is now genuinely dispatchable -- not left silently pending.
    assert superuser_db.get(MainAITask, second.id).status == MainAITaskStatus.ready
    assert superuser_db.get(MainAIGoal, goal.id).status == MainAIGoalStatus.running

    # A brand-new session (no in-memory state carried over) must see the same durable facts.
    from app.db import SessionLocal

    fresh = SessionLocal()
    try:
        _set_rls_user(fresh, owner_id)
        status = build_project_status(fresh, owner_id=owner_id)
        recent = {(g["title"], g["status"]) for g in status["goals"]["recent"]}
        assert ("Långsiktigt uppdrag", "running") in recent
        prompt = format_project_status_instructions(fresh, owner_id=owner_id, question="var är vi?")
        assert "Långsiktigt uppdrag" in prompt and "running" in prompt

        report = generate_goal_report(fresh, goal_id=goal.id)
        outcomes = {t["description"]: t["task_outcome"] for t in report["tasks"]}
        assert outcomes["Sammanställ nuläget"] == "completed"
        assert outcomes["Skriv sammanfattning"] == "ready"

        # Re-sending the same mission (a retry, or the founder the next morning) reuses it.
        again = _intake_goal(fresh, owner_id, project_id=project.id)
        assert again.id == goal.id
        fresh.commit()
    finally:
        fresh.close()

    assert superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_goals WHERE owner_id = :o"), {"o": str(owner_id)}
    ).scalar() == 1
    assert superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_tasks WHERE owner_id = :o"), {"o": str(owner_id)}
    ).scalar() == 2
    assert _job_count(superuser_db, owner_id) == 1


# ---------------------------------------------------------------- B. the model stops answering


@pytest.mark.asyncio
async def test_scenario_b_a_silent_model_keeps_the_instruction_and_never_fakes_completion(
    db_session, superuser_db, owner_id, project, monkeypatch
):
    monkeypatch.setattr(OpenAIProvider, "chat", _failing_chat(ConnectionError("modellen svarar inte")))
    goal = _intake_goal(db_session, owner_id, project_id=project.id)
    _plan(
        db_session,
        goal,
        [PlannedTaskSpec(description="Granska rapportflödet", task_type="read_only_audit", verification_plan=[])],
    )
    (task,) = _tasks(db_session, goal)
    job = _dispatch(db_session, task, goal)

    # The provider failure surfaces as the registry's own ProviderError; it must never be
    # swallowed into a fake success.
    from app.providers.base import ProviderError

    with pytest.raises(ProviderError):
        await _run_job(db_session, job, owner_id)
    db_session.rollback()

    durable_task = superuser_db.get(MainAITask, task.id)
    assert durable_task.description == "Granska rapportflödet", "the founder's instruction is never lost"
    assert durable_task.status == MainAITaskStatus.running
    assert durable_task.completed_at is None
    assert superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_task_events WHERE task_id = :t AND event_type IN ('completed','verification_passed')"),
        {"t": str(task.id)},
    ).scalar() == 0
    from app.mainai_execution.checkpoint import latest_checkpoint_for_step

    assert latest_checkpoint_for_step(superuser_db, task_id=task.id, job_id=job.id, step="work_result") is None
    # Exactly one attempt exists -- the failed call never spawns a parallel job.
    assert _job_count(superuser_db, owner_id) == 1

    # The worker's own generic handler is what makes the attempt durably failed (worker.py's
    # `except Exception`); reproduce that single step, then let the real recovery pipeline look.
    from app.jobs.service import mark_failed

    job_row = superuser_db.get(MainAIJob, job.id)
    mark_failed(
        superuser_db,
        job_row,
        worker_id="worker-a",
        lease_generation=job_row.lease_generation,
        error_category=MainAIJobErrorCategory.unexpected,
    )
    superuser_db.commit()
    assert superuser_db.get(MainAIJob, job.id).status == MainAIJobStatus.failed
    assert superuser_db.get(MainAITask, task.id).status == MainAITaskStatus.running

    _set_rls_user(db_session, owner_id)
    record = get_or_create_recovery_record(db_session, task=db_session.get(MainAITask, task.id), job=db_session.get(MainAIJob, job.id))
    db_session.commit()
    record = await inspect_recovery_record(
        db_session,
        task=db_session.get(MainAITask, task.id),
        job=db_session.get(MainAIJob, job.id),
        record=record,
    )
    db_session.commit()
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification is not None
    # Nothing durable was produced, so the honest classification is NOTHING_DONE -- never a
    # claim that work exists. And classification alone never creates a second concurrent job.
    assert record.classification.value in {c.value for c in AUTO_SALVAGEABLE_CLASSIFICATIONS} | {"CONFLICTED_STATE", "UNSAFE_TO_AUTO_RECOVER"}
    assert _job_count(superuser_db, owner_id) == 1
    assert superuser_db.get(MainAITask, task.id).status != MainAITaskStatus.completed


# ---------------------------------------------------------------- C. the worker crashes


@pytest.mark.asyncio
async def test_scenario_c_a_dead_workers_job_is_taken_over_once_and_the_old_lease_is_fenced(
    db_session, superuser_db, owner_id, project, monkeypatch
):
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat("Ingen åtgärd krävs."))
    goal = _intake_goal(db_session, owner_id, project_id=project.id)
    _plan(
        db_session,
        goal,
        [PlannedTaskSpec(description="Granska efter omstart", task_type="read_only_audit", verification_plan=[])],
    )
    (task,) = _tasks(db_session, goal)
    dead_job = _dispatch(db_session, task, goal)

    from app.db import SessionLocal

    claim_db = SessionLocal()
    try:
        _dead_id, _owner, dead_generation = claim_next_mainai_job(claim_db, "worker-that-dies", 120)
    finally:
        claim_db.close()
    assert _dead_id == dead_job.id

    # The worker process dies without finalizing: its lease simply runs out.
    superuser_db.execute(
        sa_text("UPDATE mainai_jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = :id"),
        {"id": str(dead_job.id)},
    )
    superuser_db.commit()

    # task_execution jobs are deliberately excluded from the generic reclaim branch: the
    # recovery pipeline, not the poll loop, owns this decision.
    claim_after = SessionLocal()
    try:
        assert claim_next_mainai_job(claim_after, "another-worker", 120) is None
    finally:
        claim_after.close()

    _set_rls_user(db_session, owner_id)
    live_job_row = db_session.get(MainAIJob, dead_job.id)
    live_task = db_session.get(MainAITask, task.id)
    record = get_or_create_recovery_record(db_session, task=live_task, job=live_job_row)
    db_session.commit()
    record = await inspect_recovery_record(db_session, task=live_task, job=live_job_row, record=record)
    db_session.commit()
    record = classify_recovery_record(db_session, record=record)
    db_session.commit()
    assert record.classification is not None

    if record.classification in AUTO_SALVAGEABLE_CLASSIFICATIONS:
        record, takeover_job = await execute_takeover(
            db_session, task=live_task, goal=db_session.get(MainAIGoal, goal.id), record=record, dispatched_by="recovery-worker"
        )
        db_session.commit()
        assert takeover_job.id != dead_job.id
        assert superuser_db.get(MainAIJob, dead_job.id).status == MainAIJobStatus.superseded

        # The dead worker can no longer write to a job it no longer owns.
        with pytest.raises(JobLeaseLostError):
            renew_mainai_job_lease(
                superuser_db, dead_job.id, "worker-that-dies", dead_generation, 120
            )
        superuser_db.rollback()

        # Exactly one live job exists, and only its claimant can renew it.
        claim_live = SessionLocal()
        try:
            live_id, _owner, live_generation = claim_next_mainai_job(claim_live, "worker-that-lives", 120)
            assert live_id == takeover_job.id
            renew_mainai_job_lease(claim_live, live_id, "worker-that-lives", live_generation, 120)
            claim_live.commit()
            with pytest.raises(JobLeaseLostError):
                renew_mainai_job_lease(claim_live, live_id, "worker-that-lives", live_generation + 1, 120)
            claim_live.rollback()
        finally:
            claim_live.close()
    else:
        assert record.manual_review_required is True
        assert superuser_db.get(MainAIJob, dead_job.id).status == MainAIJobStatus.running

    # Exactly one live attempt exists in either branch -- never a duplicate copy of the task.
    assert superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_jobs WHERE owner_id = :o AND status NOT IN ('superseded','failed','cancelled')"),
        {"o": str(owner_id)},
    ).scalar() == 1


# ---------------------------------------------------------------- D. already-done work is reused


def test_scenario_d_an_identical_task_and_mission_are_reused_instead_of_duplicated(
    db_session, superuser_db, owner_id, project
):
    first = work_management.create_task(
        db_session,
        owner_id=owner_id,
        project_id=project.id,
        title="Skriv releaseanteckningar",
        description="Sammanfatta vad som ändrats sedan förra veckan.",
        status="todo",
        priority=TaskPriority.medium.value,
        due_at=None,
        milestone_id=None,
        depends_on_task_ids=[],
    )
    db_session.commit()

    same = work_management.create_task(
        db_session,
        owner_id=owner_id,
        project_id=project.id,
        title="  skriv   releaseanteckningar ",
        description="Sammanfatta vad som ändrats sedan förra veckan.",
        status="todo",
        priority=TaskPriority.medium.value,
        due_at=None,
        milestone_id=None,
        depends_on_task_ids=[],
    )
    db_session.commit()
    assert same.id == first.id
    assert superuser_db.execute(
        sa_text("SELECT count(*) FROM tasks WHERE created_by = :o"), {"o": str(owner_id)}
    ).scalar() == 1

    goal = _intake_goal(db_session, owner_id, project_id=project.id)
    _plan(
        db_session,
        goal,
        [PlannedTaskSpec(description="Skriv releaseanteckningar", task_type="read_only_audit", verification_plan=[])],
    )
    repeated = _intake_goal(db_session, owner_id, project_id=project.id)
    assert repeated.id == goal.id
    assert len(_tasks(db_session, goal)) == 1
    assert superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_goals WHERE owner_id = :o"), {"o": str(owner_id)}
    ).scalar() == 1

    # A finished task never blocks a genuinely new one with the same wording.
    first.status = TaskStatus.done
    first.completed_at = datetime.utcnow()
    db_session.commit()
    after_done = work_management.create_task(
        db_session,
        owner_id=owner_id,
        project_id=project.id,
        title="Skriv releaseanteckningar",
        description="Sammanfatta vad som ändrats sedan förra veckan.",
        status="todo",
        priority=TaskPriority.medium.value,
        due_at=None,
        milestone_id=None,
        depends_on_task_ids=[],
    )
    db_session.commit()
    assert after_done.id != first.id


# ---------------------------------------------------------------- E. approval is a real gate


def test_scenario_e_a_protected_step_is_prepared_but_cannot_run_before_approval(
    db_session, superuser_db, owner_id, project
):
    goal = _intake_goal(db_session, owner_id, project_id=project.id)
    _plan(
        db_session,
        goal,
        [
            PlannedTaskSpec(
                description="Ändra betalningsflödet",
                task_type="repo_edit",
                approval_required=True,
                verification_plan=[],
            )
        ],
    )
    (task,) = _tasks(db_session, goal)

    with pytest.raises(ApprovalRequiredError):
        executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test-worker")
    db_session.rollback()
    assert _job_count(superuser_db, owner_id) == 0
    assert superuser_db.get(MainAITask, task.id).status == MainAITaskStatus.ready

    grant_task_approval(db_session, task=db_session.get(MainAITask, task.id), approved_by="founder@lifeos.local")
    db_session.commit()
    job = _dispatch(db_session, db_session.get(MainAITask, task.id), goal)
    assert job.id is not None
    assert _job_count(superuser_db, owner_id) == 1

    with pytest.raises(ValueError, match="not 'ready'"):
        executor.dispatch_ready_task(db_session, task=db_session.get(MainAITask, task.id), goal=goal, dispatched_by="test-worker")
    db_session.rollback()
    assert _job_count(superuser_db, owner_id) == 1


# ---------------------------------------------------------------- F. conflicting instructions


def test_scenario_f_a_superseded_decision_is_gone_and_an_unresolved_contradiction_is_disputed(
    superuser_db, owner_id
):
    original = record_founder_memory(
        superuser_db,
        owner_id=owner_id,
        note_type="decision",
        content="Använd endast lokala modeller för alla svar.",
        idempotency_key=f"aan-{uuid.uuid4().hex}",
        authority="founder",
        basis="manual",
    )
    superuser_db.commit()

    correction = record_founder_memory(
        superuser_db,
        owner_id=owner_id,
        note_type="decision",
        content="Använd OpenAI för alla svar.",
        idempotency_key=f"beslut-{uuid.uuid4().hex}",
        authority="founder",
        basis="manual",
        supersedes_note_id=original.id,
    )
    superuser_db.commit()

    block = format_founder_memory_instructions(superuser_db, owner_id=owner_id)
    assert "Använd OpenAI för alla svar." in block
    assert "Använd endast lokala modeller" not in block, "a superseded decision must never resurface as current"

    mark_founder_memory_disputed(superuser_db, owner_id=owner_id, note_id=correction.id)
    superuser_db.commit()

    disputed_block = format_founder_memory_instructions(superuser_db, owner_id=owner_id)
    assert "OSTRIDIGT" in disputed_block
    assert "Använd OpenAI för alla svar." in disputed_block
    # The contradictory statement is only ever presented as unresolved, never as the current
    # decision: the only occurrence is on the explicitly disputed line.
    active_lines = [line for line in disputed_block.splitlines() if line.startswith("- [")]
    assert not any("Använd OpenAI" in line for line in active_lines)
    assert any("Använd OpenAI" in line for line in disputed_block.splitlines() if "OSTRIDIGT" in line)


# ---------------------------------------------------------------- G. no false work report


@pytest.mark.asyncio
async def test_scenario_g_a_finished_attempt_with_failed_verification_is_never_reported_as_done(
    db_session, superuser_db, owner_id, project, monkeypatch, tmp_path
):
    from app.mainai_execution import execution_job

    (tmp_path / "backend" / "tests").mkdir(parents=True)
    (tmp_path / "backend" / "tests" / "test_deliberately_failing.py").write_text(
        "def test_this_must_fail():\n    assert False, 'verifieringen ska misslyckas'\n"
    )
    monkeypatch.setattr(execution_job, "_repo_root", lambda: tmp_path)
    monkeypatch.setattr(execution_job, "_backend_root", lambda: tmp_path / "backend")

    goal = _intake_goal(db_session, owner_id, project_id=project.id)
    _plan(
        db_session,
        goal,
        [
            PlannedTaskSpec(
                description="Kör den omöjliga verifieringen",
                task_type="run_tests",
                verification_plan=[{"kind": "targeted_tests", "target": "tests/test_deliberately_failing.py"}],
                max_attempts=1,
            )
        ],
    )
    (task,) = _tasks(db_session, goal)
    job = _dispatch(db_session, task, goal)
    await _run_job(db_session, job, owner_id)

    # The ATTEMPT finished (the job is completed) — the WORK did not pass.
    assert superuser_db.get(MainAIJob, job.id).status == MainAIJobStatus.completed
    durable_task = superuser_db.get(MainAITask, task.id)
    assert durable_task.status in (MainAITaskStatus.retryable_failed, MainAITaskStatus.failed)
    assert durable_task.status != MainAITaskStatus.completed
    assert superuser_db.execute(
        sa_text("SELECT count(*) FROM mainai_task_events WHERE task_id = :t AND event_type = 'completed'"),
        {"t": str(task.id)},
    ).scalar() == 0

    report = record_final_report(db_session, goal=db_session.get(MainAIGoal, goal.id))
    db_session.commit()
    assert report["goal"]["status"] != MainAIGoalStatus.completed.value
    task_report = next(t for t in report["tasks"] if t["task_id"] == str(task.id))
    assert task_report["task_outcome"] in ("retryable_failed", "failed")
    assert task_report["verification_outcome"]["passed"] is False
    assert task_report["unresolved_risk"] is True
    assert superuser_db.get(MainAIGoal, goal.id).status != MainAIGoalStatus.completed
