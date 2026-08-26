"""B7 — WAITING_PROVIDER defer must not leave a dead running job.

After an independent provider-planning wait, the mid-flight `mainai_jobs` row is
fence-released and the task returns to `ready` so the next supervisor tick is the wake —
not expired-lease auto-recovery → ordinary `task_execution` claim.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.development_supervisor.service import SupervisorBounds, WorkBinding, run_supervisor
from app.mainai_execution.executor import TaskNotRetryableError, reset_task_for_takeover
from app.models.mainai_execution import MainAICheckpoint, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from tests.backend.mainai.test_scoped_development_supervisor import (
    FailingProvider,
    _foundation,
    _independent_candidate,
)


@pytest.mark.asyncio
async def test_waiting_provider_defer_releases_job_and_returns_task_ready(
    superuser_db, tmp_path
):
    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    scope = replace(scope, provider_spend_authorized=True)
    first.priority = 20
    second.priority = 10
    provider_binding = WorkBinding(
        first.id,
        prepare,
        None,
        FailingProvider(),
        provider_likely=True,
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
    outage = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(provider_binding, independent),
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert outage.classification == "WAITING_PROVIDER"
    assert second.status == MainAITaskStatus.completed
    superuser_db.refresh(first)
    assert first.status == MainAITaskStatus.ready

    jobs = (
        superuser_db.execute(
            select(MainAIJob)
            .where(MainAIJob.owner_id == goal.owner_id)
            .order_by(MainAIJob.created_at.asc())
        )
        .scalars()
        .all()
    )
    provider_jobs = [
        j for j in jobs if any(r.get("id") == str(first.id) for r in (j.input_refs or []))
    ]
    assert provider_jobs
    assert all(j.status != MainAIJobStatus.running for j in provider_jobs)
    assert any(j.status == MainAIJobStatus.failed for j in provider_jobs)

    checkpoints = [
        row
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
        if row.executor_state.get("phase") == "WAITING_PROVIDER"
    ]
    assert checkpoints


@pytest.mark.asyncio
async def test_waiting_provider_release_blocks_takeover_reset_path(
    superuser_db, tmp_path
):
    """After defer-release, takeover reset (running→ready) must fail closed — task is already ready."""
    _, _, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    scope = replace(scope, provider_spend_authorized=True)
    second.status = MainAITaskStatus.blocked
    provider_binding = WorkBinding(
        first.id,
        prepare,
        None,
        FailingProvider(),
        provider_likely=True,
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(provider_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    superuser_db.refresh(first)
    assert first.status == MainAITaskStatus.ready

    job = superuser_db.execute(
        select(MainAIJob).where(MainAIJob.id == first.mainai_job_id)
    ).scalar_one()
    job.lease_expires_at = datetime.utcnow() - timedelta(minutes=5)
    superuser_db.flush()

    with pytest.raises(TaskNotRetryableError):
        reset_task_for_takeover(superuser_db, task=first)


@pytest.mark.asyncio
async def test_waiting_provider_next_tick_can_redispatch(
    superuser_db, tmp_path
):
    _, _, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    scope = replace(scope, provider_spend_authorized=True)
    second.status = MainAITaskStatus.blocked
    provider_binding = WorkBinding(
        first.id,
        prepare,
        None,
        FailingProvider(),
        provider_likely=True,
        independent=True,
        repository_identity=scope.repository_identity,
        allowed_paths=scope.allowed_paths,
    )
    first_tick = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(provider_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert first_tick.classification == "WAITING_PROVIDER"
    superuser_db.commit()

    second_tick = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(provider_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    assert second_tick.classification == "WAITING_PROVIDER"
    superuser_db.refresh(first)
    assert first.status == MainAITaskStatus.ready
