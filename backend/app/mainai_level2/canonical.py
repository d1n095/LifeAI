"""Adapter to the existing canonical MainAI goal/task tables.

No Level-2-specific job ledger is introduced.  The adapter only creates/reads goal and task
rows and leaves execution claims, leases and effects to ``mainai_execution``.
"""
from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution import MainAIGoal, MainAIGoalRiskLevel, MainAITask, MainAITaskStatus


class CanonicalProgramStore:
    def __init__(self, db: Session):
        self.db = db

    def create_program(self, *, owner_id: uuid.UUID, objective: str, title: str = "Level-2 program") -> MainAIGoal:
        goal = MainAIGoal(owner_id=owner_id, title=title, original_instruction=objective,
                          risk_level=MainAIGoalRiskLevel.low, created_by="mainai_level2")
        self.db.add(goal)
        self.db.flush()
        return goal

    def list_tasks(self, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> list[MainAITask]:
        return list(self.db.scalars(select(MainAITask).where(MainAITask.owner_id == owner_id,
                                                              MainAITask.goal_id == goal_id)).all())

    def current_task(self, *, owner_id: uuid.UUID, task_id: uuid.UUID) -> MainAITask | None:
        task = self.db.get(MainAITask, task_id, populate_existing=True)
        if task is None or task.owner_id != owner_id:
            return None
        return task

    def ready_tasks(self, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> list[MainAITask]:
        return list(self.db.scalars(select(MainAITask).where(
            MainAITask.owner_id == owner_id,
            MainAITask.goal_id == goal_id,
            MainAITask.status == MainAITaskStatus.ready,
        ).order_by(MainAITask.priority.desc(), MainAITask.created_at)).all())

    def is_current_and_actionable(self, *, owner_id: uuid.UUID, task_id: uuid.UUID) -> bool:
        task = self.current_task(owner_id=owner_id, task_id=task_id)
        return task is not None and task.status in {MainAITaskStatus.ready, MainAITaskStatus.running}
