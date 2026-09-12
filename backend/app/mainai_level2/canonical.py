"""Adapter to the existing canonical MainAI goal/task tables.

No Level-2-specific job ledger is introduced.  The adapter only creates/reads goal and task
rows and leaves execution claims, leases and effects to ``mainai_execution``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution import MainAIGoal, MainAIGoalRiskLevel, MainAITask, MainAITaskStatus
from app.models.mainai_job import MainAIJob
from app.models.mainai_level2 import MainAILevel2Event, MainAILevel2Program


@dataclass(frozen=True)
class CanonicalRecoverySnapshot:
    program: MainAILevel2Program
    events: tuple[MainAILevel2Event, ...]
    owner_jobs: tuple[MainAIJob, ...]
    source: str = "postgresql"


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

    def create_level2_program(self, *, owner_id: uuid.UUID, objective: str, scope: list | None = None,
                              acceptance: list | None = None, verification: list | None = None,
                              authority_boundary: list | None = None, budget: dict | None = None) -> MainAILevel2Program:
        program = MainAILevel2Program(owner_id=owner_id, objective=objective, scope=scope or [],
                                      acceptance_criteria=acceptance or [], verification_criteria=verification or [],
                                      authority_boundary=authority_boundary or ["no_merge", "no_deploy"],
                                      budget_state=budget or {})
        self.db.add(program)
        self.db.flush()
        self.append_event(program=program, event_type="PROGRAM_CREATED", metadata={"objective": objective})
        return program

    def append_event(self, *, program: MainAILevel2Program, event_type: str, metadata: dict | None = None) -> MainAILevel2Event:
        """Append journal evidence in the same transaction as the caller's state change."""
        last = self.db.scalar(select(MainAILevel2Event.sequence).where(
            MainAILevel2Event.program_id == program.id).order_by(MainAILevel2Event.sequence.desc()).limit(1))
        event = MainAILevel2Event(owner_id=program.owner_id, program_id=program.id,
                                  sequence=(last or 0) + 1, event_type=event_type, metadata=metadata or {})
        self.db.add(event)
        self.db.flush()
        return event

    def level2_program(self, *, owner_id: uuid.UUID, program_id: uuid.UUID) -> MainAILevel2Program | None:
        program = self.db.get(MainAILevel2Program, program_id, populate_existing=True)
        return program if program is not None and program.owner_id == owner_id else None

    def journal(self, *, owner_id: uuid.UUID, program_id: uuid.UUID) -> list[MainAILevel2Event]:
        return list(self.db.scalars(select(MainAILevel2Event).where(
            MainAILevel2Event.owner_id == owner_id, MainAILevel2Event.program_id == program_id
        ).order_by(MainAILevel2Event.sequence)).all())

    def recover_level2(self, *, owner_id: uuid.UUID, program_id: uuid.UUID) -> CanonicalRecoverySnapshot:
        """Rebuild a fresh recovery view from canonical PostgreSQL rows.

        Journal rows are included as evidence, but current program/job rows are always read with
        populate_existing so stale ORM identity-map state cannot revive work or authority.
        """
        program = self.db.get(MainAILevel2Program, program_id, populate_existing=True)
        if program is None or program.owner_id != owner_id:
            raise LookupError("program is not visible to owner")
        events = tuple(self.journal(owner_id=owner_id, program_id=program_id))
        jobs = tuple(self.db.scalars(select(MainAIJob).where(MainAIJob.owner_id == owner_id)).all())
        return CanonicalRecoverySnapshot(program=program, events=events, owner_jobs=jobs)
