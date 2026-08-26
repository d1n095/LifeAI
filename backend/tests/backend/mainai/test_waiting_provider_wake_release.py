"""B7 — WAITING_PROVIDER must not leave a dead running job OR hot-loop ready.

After defer: fence-fail job, park blocked with next_retry_at backoff. Worker wake when due.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.development_supervisor.service import SupervisorBounds, WorkBinding, run_supervisor
from app.mainai_execution.executor import TaskNotRetryableError, reset_task_for_takeover
from app.mainai_execution.provider_wait_wake import (
    WAITING_PROVIDER_BACKOFF_REASON,
    wake_due_waiting_provider_backoff_tasks,
)
from app.models.mainai_execution import MainAICheckpoint, MainAITaskEvent, MainAITaskEventType, MainAITaskStatus
from app.models.mainai_job import MainAIJob, MainAIJobStatus
from tests.backend.mainai.test_scoped_development_supervisor import (
    FailingProvider,
    _foundation,
    _independent_candidate,
)


@pytest.mark.asyncio
async def test_waiting_provider_defer_parks_blocked_with_backoff_not_immediate_ready(
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
    assert first.status == MainAITaskStatus.blocked
    assert first.next_retry_at is not None
    assert first.next_retry_at > datetime.utcnow()

    jobs = (
        superuser_db.execute(
            select(MainAIJob).where(MainAIJob.owner_id == goal.owner_id)
        )
        .scalars()
        .all()
    )
    provider_jobs = [
        j for j in jobs if any(r.get("id") == str(first.id) for r in (j.input_refs or []))
    ]
    assert provider_jobs
    assert all(j.status != MainAIJobStatus.running for j in provider_jobs)

    # Immediate next tick must NOT redispatch (still blocked, retry_at in future).
    again = await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(provider_binding,),
        bounds=SupervisorBounds(max_jobs=1),
    )
    superuser_db.refresh(first)
    assert first.status == MainAITaskStatus.blocked
    assert again.classification != "WAITING_PROVIDER" or first.next_retry_at is not None


@pytest.mark.asyncio
async def test_waiting_provider_backoff_wake_makes_task_ready(superuser_db, tmp_path):
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
    assert first.status == MainAITaskStatus.blocked
    first.next_retry_at = datetime.utcnow() - timedelta(seconds=1)
    superuser_db.flush()

    woken = wake_due_waiting_provider_backoff_tasks(superuser_db, limit=10)
    assert [t.id for t in woken] == [first.id]
    superuser_db.refresh(first)
    assert first.status == MainAITaskStatus.ready
    assert first.next_retry_at is None

    with pytest.raises(TaskNotRetryableError):
        reset_task_for_takeover(superuser_db, task=first)


@pytest.mark.asyncio
async def test_waiting_provider_release_blocks_takeover_reset_path(superuser_db, tmp_path):
    _, _, first, second, _, _, prepare, scope = _foundation(
        superuser_db, tmp_path, tied=True
    )
    scope = replace(scope, provider_spend_authorized=True)
    second.status = MainAITaskStatus.blocked
    await run_supervisor(
        superuser_db,
        scope=scope,
        bindings=(
            WorkBinding(
                first.id,
                prepare,
                None,
                FailingProvider(),
                provider_likely=True,
                independent=True,
                repository_identity=scope.repository_identity,
                allowed_paths=scope.allowed_paths,
            ),
        ),
        bounds=SupervisorBounds(max_jobs=1),
    )
    superuser_db.refresh(first)
    assert first.status == MainAITaskStatus.blocked
    with pytest.raises(TaskNotRetryableError):
        reset_task_for_takeover(superuser_db, task=first)

    events = (
        superuser_db.execute(
            select(MainAITaskEvent).where(
                MainAITaskEvent.task_id == first.id,
                MainAITaskEvent.event_type == MainAITaskEventType.blocked,
            )
        )
        .scalars()
        .all()
    )
    assert any((e.detail or {}).get("reason") == WAITING_PROVIDER_BACKOFF_REASON for e in events)
    assert any(
        row.executor_state.get("phase") == "WAITING_PROVIDER"
        for row in superuser_db.execute(
            select(MainAICheckpoint).where(MainAICheckpoint.goal_id == first.goal_id)
        ).scalars()
    )
