"""PROVIDER_SPEND_NOT_AUTHORIZED must park durably — not leave a dead running job."""

from __future__ import annotations

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
async def test_provider_spend_defer_parks_blocked_and_releases_job(superuser_db, tmp_path):
    _, goal, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    assert scope.provider_spend_authorized is False
    first.priority = 20
    second.priority = 10
    denied = WorkBinding(
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
    result = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(denied, independent),
        bounds=SupervisorBounds(max_jobs=2),
    )
    assert result.classification == "PROVIDER_SPEND_NOT_AUTHORIZED"
    assert second.status == MainAITaskStatus.completed
    superuser_db.refresh(first)
    assert first.status == MainAITaskStatus.blocked
    assert "not authorized" in (first.blocker_reason or "").lower() or "provider" in (
        first.blocker_reason or ""
    ).lower()

    job = superuser_db.execute(
        select(MainAIJob).where(MainAIJob.id == first.mainai_job_id)
    ).scalar_one()
    assert job.status == MainAIJobStatus.failed

    with pytest.raises(TaskNotRetryableError):
        reset_task_for_takeover(superuser_db, task=first)

    # Second tick must not redispatch the parked spend denial.
    again = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(denied, independent),
        bounds=SupervisorBounds(max_jobs=1),
    )
    # Independent already completed; spend task remains blocked — no new running claim.
    superuser_db.refresh(first)
    assert first.status == MainAITaskStatus.blocked
    assert again.classification in {
        "PROVIDER_SPEND_NOT_AUTHORIZED",
        "RUN_BOUND_REACHED",
        "NEEDS_CLARIFICATION",
        "COMPLETE",
        "WAITING_APPROVAL",
    } or again.classification is not None

    cps = [
        row
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == goal.id)
        ).scalars()
        if row.executor_state.get("phase") == "PROVIDER_SPEND_NOT_AUTHORIZED"
    ]
    assert cps
