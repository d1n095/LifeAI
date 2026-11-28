"""A goal parked on founder approval must not grow a checkpoint row every poll cycle.

Regression for a measured defect (2026-11-27 local stack): one goal waiting for founder
approval accumulated **9 855** `mainai_checkpoints` rows in about three hours, one row every
2-second worker tick, because the Supervisor re-wrote an identical WAITING_APPROVAL checkpoint
each time it re-observed the same unchanged state. WAITING_APPROVAL is a durable state, not
progress, so re-observing it must reuse the existing row — while any genuinely different state
(another selected task, another phase) must still get its own append-only row.
"""

from __future__ import annotations

import uuid

from tests.backend.mainai.test_scoped_development_supervisor import _foundation


def test_reobserving_the_same_approval_wait_reuses_its_checkpoint(superuser_db, tmp_path):
    _, goal, task, _, _, _, _, _ = _foundation(superuser_db, tmp_path)
    from app.development_supervisor.service import _checkpoint

    state = {"completed_task_ids": [], "selected_task_id": str(task.id)}
    first = _checkpoint(
        superuser_db,
        goal=goal,
        task=task,
        job_id=None,
        phase="WAITING_APPROVAL",
        state=state,
    )
    second = _checkpoint(
        superuser_db,
        goal=goal,
        task=task,
        job_id=None,
        phase="WAITING_APPROVAL",
        state=dict(state),
    )

    assert second.id == first.id


def test_a_different_approval_wait_still_appends(superuser_db, tmp_path):
    _, goal, task, other, _, _, _, _ = _foundation(superuser_db, tmp_path)
    from app.development_supervisor.service import _checkpoint

    first = _checkpoint(
        superuser_db,
        goal=goal,
        task=task,
        job_id=None,
        phase="WAITING_APPROVAL",
        state={"completed_task_ids": [], "selected_task_id": str(task.id)},
    )
    other_task = _checkpoint(
        superuser_db,
        goal=goal,
        task=task,
        job_id=None,
        phase="WAITING_APPROVAL",
        state={"completed_task_ids": [], "selected_task_id": str(other.id)},
    )
    another_phase = _checkpoint(
        superuser_db,
        goal=goal,
        task=task,
        job_id=None,
        phase="WAITING_PROVIDER",
        state={"completed_task_ids": [], "selected_task_id": str(task.id)},
    )

    assert other_task.id != first.id
    assert another_phase.id != first.id


def test_an_unrelated_goal_is_never_collapsed_into_another(superuser_db, tmp_path):
    _, goal, task, _, _, _, _, _ = _foundation(superuser_db, tmp_path)
    from app.development_supervisor.service import _checkpoint
    from app.mainai_execution.planner import create_goal

    other_goal = create_goal(
        superuser_db,
        owner_id=goal.owner_id,
        title="unrelated goal",
        original_instruction="unrelated work",
        created_by="test",
        approval_policy="autonomous_development_work",
    )
    superuser_db.flush()
    other_task = task

    mine = _checkpoint(
        superuser_db,
        goal=goal,
        task=task,
        job_id=None,
        phase="WAITING_APPROVAL",
        state={"completed_task_ids": [], "selected_task_id": str(task.id)},
    )
    theirs = _checkpoint(
        superuser_db,
        goal=other_goal,
        task=other_task,
        job_id=None,
        phase="WAITING_APPROVAL",
        state={"completed_task_ids": [], "selected_task_id": str(other_task.id)},
    )

    assert uuid.UUID(str(theirs.id)) != uuid.UUID(str(mine.id))
