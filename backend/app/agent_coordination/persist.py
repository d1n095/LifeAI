"""Durable backing storage for the coordination ledger.

DEF-1 / GH-4: GitHub discovery used to create in-memory builder/examiner obligations
and persist only when an AgentWorkAssignment already existed. A later refresh or a
fresh session then recreated those obligations. This module creates one owner-scoped
backing row when none exists and writes every obligation onto it.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent_coordination.durable import DurableCoordinationService, persist_to_assignment
from app.models.agent_coordination import AgentWorkAssignment
from app.models.mainai_execution import MainAIGoal, MainAITask

LEDGER_BACKING_KEY = "ledger_backing"
LEDGER_AGENT_KEY = "mainai-ledger"
LEDGER_GOAL_TITLE = "MainAI durable coordination ledger"


def _is_backing(row: AgentWorkAssignment) -> bool:
    provenance = row.provenance or {}
    return bool(provenance.get(LEDGER_BACKING_KEY) or (provenance.get("durable_coordination") or {}).get(LEDGER_BACKING_KEY))


def find_owner_assignments(db: Session, *, owner_id: uuid.UUID) -> list[AgentWorkAssignment]:
    return list(db.execute(select(AgentWorkAssignment).where(AgentWorkAssignment.owner_id == owner_id)).scalars())


def ensure_owner_ledger_backing(db: Session, *, owner_id: uuid.UUID) -> AgentWorkAssignment:
    """Return a durable assignment that can hold the ledger. Create one if discovery starts cold."""
    rows = find_owner_assignments(db, owner_id=owner_id)
    for row in rows:
        if _is_backing(row):
            return row
    if rows:
        first = rows[0]
        provenance = dict(first.provenance or {})
        provenance[LEDGER_BACKING_KEY] = True
        first.provenance = provenance
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(first, "provenance")
        db.flush()
        return first
    return _create_backing_assignment(db, owner_id=owner_id)


def _create_backing_assignment(db: Session, *, owner_id: uuid.UUID) -> AgentWorkAssignment:
    from app.agent_coordination.service import create_work_assignment, register_agent
    from app.mainai_execution.planner import PlannedTaskSpec, create_goal, create_plan

    existing_goal = db.execute(
        select(MainAIGoal).where(MainAIGoal.owner_id == owner_id, MainAIGoal.title == LEDGER_GOAL_TITLE)
    ).scalars().first()
    if existing_goal is None:
        goal = create_goal(
            db,
            owner_id=owner_id,
            title=LEDGER_GOAL_TITLE,
            original_instruction="Hold builder and examiner obligations discovered without a prior manual assignment.",
            created_by="coordination",
            approval_policy="autonomous_development_work",
        )
        plan = create_plan(
            db,
            goal=goal,
            rationale="durable ledger backing",
            tasks=[
                PlannedTaskSpec(
                    description="Persist coordination ledger",
                    task_type="read_only_audit",
                    verification_plan=[],
                )
            ],
            created_by="coordination",
        )
        task = db.execute(select(MainAITask).where(MainAITask.plan_id == plan.id)).scalars().one()
    else:
        goal = existing_goal
        task = db.execute(select(MainAITask).where(MainAITask.goal_id == goal.id)).scalars().first()
    agent = register_agent(
        db,
        agent_key=LEDGER_AGENT_KEY,
        display_name="MainAI ledger",
        adapter_kind="internal",
        supports_write=False,
    )
    return create_work_assignment(
        db,
        owner_id=owner_id,
        goal_id=goal.id,
        task_id=None if task is None else task.id,
        agent_id=agent.id,
        role="researcher",
        read_write_mode="read_only",
        repository_identity="lifeai",
        allowed_paths=["docs/**"],
        requested_by="coordination",
        provenance={LEDGER_BACKING_KEY: True, "created_without_manual_assignment": True},
    )


def persist_owner_ledger(
    db: Session,
    *,
    owner_id: uuid.UUID,
    service: DurableCoordinationService,
    actor: str = "coordination",
) -> AgentWorkAssignment:
    """Write every obligation, handoff, GitHub intake, lifecycle and delivery slice."""
    backing = ensure_owner_ledger_backing(db, owner_id=owner_id)
    for record in service._obligations.values():
        record.setdefault("backing_assignment_id", str(backing.id))
    assignments = find_owner_assignments(db, owner_id=owner_id)
    if backing not in assignments:
        assignments.append(backing)
    obligation_id = next(iter(service._obligations), str(backing.id))
    persist_to_assignment(
        db,
        assignment=backing,
        service=service,
        obligation_id=obligation_id,
        actor=actor,
    )
    dump = service.dump_for(obligation_id)
    dump[LEDGER_BACKING_KEY] = True
    provenance = dict(backing.provenance or {})
    provenance[LEDGER_BACKING_KEY] = True
    provenance["durable_coordination"] = dump
    backing.provenance = provenance
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(backing, "provenance")
    # Keep sibling assignment dumps current so GH-4b rows also restore the full ledger.
    for assignment in assignments:
        if assignment.id == backing.id:
            continue
        persist_to_assignment(
            db,
            assignment=assignment,
            service=service,
            obligation_id=obligation_id,
            actor=actor,
        )
    db.flush()
    return backing


def persist_status(db: Session, *, owner_id: uuid.UUID) -> dict[str, Any]:
    rows = find_owner_assignments(db, owner_id=owner_id)
    backing = [row for row in rows if _is_backing(row)]
    return {
        "backing_present": bool(backing or rows),
        "assignment_count": len(rows),
        "created_without_manual_assignment": any(
            (row.provenance or {}).get("created_without_manual_assignment") for row in backing
        ),
    }
