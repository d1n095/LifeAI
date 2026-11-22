"""Life Workspaces API.

A workspace is the existing founder project row plus the missing product composition:
purpose/goals, archive/restore, owner-scoped activity and decisions, workspace-linked
conversations, and a bounded context view assembled from the existing document, task,
conversation and retrieval systems. No second project model, chat engine or authorization
system is introduced here.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import require_founder
from app.egress_policy import EgressDeniedError
from app.mainai_execution.liveness import task_liveness
from app.models.conversation import Conversation
from app.models.document import ActiveTruthStatus, Document
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoal,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
)
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import MainAIRecoveryRecord
from app.models.mainai_wait import MainAITaskWait
from app.models.project import Project, Task
from app.models.user import User
from app.models.work_management import WorkspaceMilestone
from app.models.workspace import (
    WorkspaceActivity,
    WorkspaceDecision,
    WorkspaceDecisionStatus,
    WorkspaceMember,
    WorkspaceMemberRole,
    WorkspaceSyncItem,
    WorkspaceSyncItemStatus,
    WorkspaceSyncRun,
    WorkspaceSyncStatus,
)
from app.providers.base import ProviderError
from app.rag.retrieve import GroundingUnavailableError, retrieve_context
from app.schemas import (
    ConversationOut,
    KnowledgeSourceOut,
    MainAICheckpointOut,
    MainAIGoalOut,
    MainAIJobOut,
    MainAIRecoveryRecordOut,
    MainAITaskEventOut,
    MainAITaskOut,
    MainAITaskWaitOut,
    MilestoneOut,
    TaskIn,
    TaskOut,
    WorkspaceActivityOut,
    WorkspaceContextOut,
    WorkspaceContextSourceOut,
    WorkspaceDecisionIn,
    WorkspaceDecisionOut,
    WorkspaceDecisionSupersedeIn,
    WorkspaceDetailOut,
    WorkspaceIn,
    WorkspaceOut,
    WorkspaceSupervisedOut,
    WorkspaceSupervisedTaskOut,
    WorkspaceSyncIn,
    WorkspaceSyncItemOut,
    WorkspaceSyncRunOut,
    WorkspaceUpdate,
)
from app.work_management import create_task, milestone_out, task_out

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"], dependencies=[Depends(require_founder)])


def _owned_project(db: Session, project_id: uuid.UUID, user: User) -> Project:
    project = (
        db.query(Project)
        .filter(
            Project.id == project_id,
            Project.created_by == user.id,
            Project.deleted_at.is_(None),
        )
        .first()
    )
    if project is None:
        raise HTTPException(status_code=404, detail="Workspace hittades inte.")
    return project


def _record_activity(
    db: Session,
    *,
    project: Project,
    user: User,
    kind: str,
    summary: str,
    detail: dict | None = None,
) -> WorkspaceActivity:
    activity = WorkspaceActivity(
        project_id=project.id,
        owner_id=user.id,
        kind=kind,
        summary=summary[:512],
        detail=detail or {},
    )
    project.last_activity_at = datetime.utcnow()
    db.add(activity)
    return activity


def _touch_project(project: Project) -> None:
    """Advance the optimistic workspace version for any real state change."""

    project.version = (project.version or 1) + 1
    project.last_activity_at = datetime.utcnow()


def _workspace_sources(
    db: Session, project: Project, user: User, limit: int = 50, offset: int = 0
) -> list[Document]:
    return (
        db.query(Document)
        .filter(
            Document.project_id == project.id,
            Document.uploaded_by == user.id,
            Document.deleted_at.is_(None),
        )
        .order_by(Document.created_at.desc(), Document.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def _workspace_tasks(
    db: Session, project: Project, user: User, limit: int = 100, offset: int = 0
) -> list[Task]:
    return (
        db.query(Task)
        .filter(Task.project_id == project.id, Task.created_by == user.id)
        .order_by(Task.created_at.asc(), Task.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def _workspace_milestones(
    db: Session, project: Project, user: User, limit: int = 100, offset: int = 0
) -> list[WorkspaceMilestone]:
    return (
        db.query(WorkspaceMilestone)
        .filter(
            WorkspaceMilestone.project_id == project.id,
            WorkspaceMilestone.owner_id == user.id,
        )
        .order_by(
            WorkspaceMilestone.due_at.asc().nulls_last(),
            WorkspaceMilestone.created_at.asc(),
            WorkspaceMilestone.id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )


def _workspace_conversations(
    db: Session, project: Project, user: User, limit: int = 20, offset: int = 0
) -> list[Conversation]:
    return (
        db.query(Conversation)
        .filter(Conversation.project_id == project.id, Conversation.user_id == user.id)
        .order_by(Conversation.updated_at.desc(), Conversation.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def _workspace_decisions(
    db: Session, project: Project, user: User, limit: int = 50, offset: int = 0
) -> list[WorkspaceDecision]:
    return (
        db.query(WorkspaceDecision)
        .filter(WorkspaceDecision.project_id == project.id, WorkspaceDecision.owner_id == user.id)
        .order_by(WorkspaceDecision.created_at.desc(), WorkspaceDecision.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


def _workspace_activities(
    db: Session, project: Project, user: User, limit: int = 50, offset: int = 0
) -> list[WorkspaceActivity]:
    return (
        db.query(WorkspaceActivity)
        .filter(WorkspaceActivity.project_id == project.id, WorkspaceActivity.owner_id == user.id)
        .order_by(WorkspaceActivity.created_at.desc(), WorkspaceActivity.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


class StaleWorkspaceError(Exception):
    """The workspace changed after a sync snapshot was taken."""


def _stable_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _sync_entities(db: Session, project: Project, user: User) -> tuple[int, str, list[dict]]:
    """Build a bounded, deterministic snapshot of the workspace's real owned state.

    Only entity identities, versions and content hashes are stored. Document text and chat
    message content never enter the sync ledger.
    """

    entities: list[dict] = []

    def add(entity_type: str, entity_id: uuid.UUID, entity_version: int, payload: object) -> None:
        entities.append(
            {
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "entity_version": int(entity_version),
                "content_hash": _stable_hash(payload),
            }
        )

    add(
        "workspace",
        project.id,
        project.version or 1,
        {
            "id": str(project.id),
            "name": project.name,
            "description": project.description,
            "purpose": project.purpose,
            "goals": project.goals or [],
            "status": project.status.value if hasattr(project.status, "value") else project.status,
            "settings": project.settings or {},
            "archived_at": project.archived_at,
            "deleted_at": project.deleted_at,
            "updated_at": project.updated_at,
        },
    )
    for source in _workspace_sources(db, project, user, limit=1000):
        add(
            "document",
            source.id,
            source.version_number or 1,
            {
                "id": str(source.id),
                "title": source.title,
                "checksum": source.checksum,
                "version_number": source.version_number,
                "status": source.status.value if hasattr(source.status, "value") else source.status,
                "active_truth_status": (
                    source.active_truth_status.value
                    if hasattr(source.active_truth_status, "value")
                    else source.active_truth_status
                ),
                "updated_at": source.updated_at,
            },
        )
    for task in _workspace_tasks(db, project, user, limit=1000):
        add(
            "task",
            task.id,
            task.version or 1,
            {
                "id": str(task.id),
                "title": task.title,
                "description": task.description,
                "status": task.status.value if hasattr(task.status, "value") else task.status,
                "priority": task.priority.value if hasattr(task.priority, "value") else task.priority,
                "due_at": task.due_at,
                "milestone_id": str(task.milestone_id) if task.milestone_id else None,
                "blocked_reason": task.blocked_reason,
                "updated_at": task.updated_at,
            },
        )
    for milestone in _workspace_milestones(db, project, user, limit=1000):
        add(
            "milestone",
            milestone.id,
            milestone.version or 1,
            {
                "id": str(milestone.id),
                "title": milestone.title,
                "description": milestone.description,
                "due_at": milestone.due_at,
                "status": milestone.status.value if hasattr(milestone.status, "value") else milestone.status,
                "completed_at": milestone.completed_at,
                "updated_at": milestone.updated_at,
            },
        )
    for conversation in _workspace_conversations(db, project, user, limit=1000):
        add(
            "conversation",
            conversation.id,
            1,
            {
                "id": str(conversation.id),
                "title": conversation.title,
                "project_id": str(conversation.project_id) if conversation.project_id else None,
                "updated_at": conversation.updated_at,
            },
        )
    for decision in _workspace_decisions(db, project, user, limit=1000):
        add(
            "decision",
            decision.id,
            decision.version or 1,
            {
                "id": str(decision.id),
                "title": decision.title,
                "decision": decision.decision,
                "rationale": decision.rationale,
                "status": decision.status.value if hasattr(decision.status, "value") else decision.status,
                "epistemic_status": decision.epistemic_status,
                "origin": decision.origin,
                "supersedes_id": str(decision.supersedes_id) if decision.supersedes_id else None,
                "updated_at": decision.updated_at,
            },
        )

    entities.sort(key=lambda item: (item["entity_type"], item["entity_id"]))
    snapshot_hash = _stable_hash(
        {"workspace_id": str(project.id), "version": project.version or 1, "entities": entities}
    )
    return project.version or 1, snapshot_hash, entities


def _reconcile_interrupted_syncs(db: Session, project: Project, user: User) -> int:
    """Fail closed on runs whose worker heartbeat has gone stale.

    Called before a new run is created, so an interrupted request cannot leave a workspace
    permanently looking "running". A later retry can resume from the durable failed run.
    """

    cutoff = datetime.utcnow() - timedelta(minutes=5)
    running = (
        db.query(WorkspaceSyncRun)
        .filter(
            WorkspaceSyncRun.project_id == project.id,
            WorkspaceSyncRun.owner_id == user.id,
            WorkspaceSyncRun.status == WorkspaceSyncStatus.running,
        )
        .all()
    )
    reconciled = 0
    for run in running:
        last_seen = run.heartbeat_at or run.started_at or run.created_at
        if last_seen is None or last_seen >= cutoff:
            continue
        run.status = WorkspaceSyncStatus.failed
        run.error_category = "interrupted"
        run.completed_at = datetime.utcnow()
        run.result = {"interrupted": True, "reconciled": True}
        reconciled += 1
    if reconciled:
        db.flush()
    return reconciled


def _previous_sync_item(
    db: Session,
    *,
    project: Project,
    user: User,
    entity_type: str,
    entity_id: uuid.UUID,
    content_hash: str,
) -> WorkspaceSyncItem | None:
    return (
        db.query(WorkspaceSyncItem)
        .join(WorkspaceSyncRun, WorkspaceSyncItem.run_id == WorkspaceSyncRun.id)
        .filter(
            WorkspaceSyncItem.project_id == project.id,
            WorkspaceSyncItem.owner_id == user.id,
            WorkspaceSyncItem.entity_type == entity_type,
            WorkspaceSyncItem.entity_id == entity_id,
            WorkspaceSyncItem.content_hash == content_hash,
            WorkspaceSyncItem.status.in_(
                [WorkspaceSyncItemStatus.applied, WorkspaceSyncItemStatus.skipped]
            ),
            WorkspaceSyncRun.status == WorkspaceSyncStatus.completed,
        )
        .first()
    )


def _apply_sync_entity(
    db: Session, *, project: Project, user: User, item: WorkspaceSyncItem
) -> None:
    """Verify that one snapshot entity is still the caller's own current state.

    This deliberately performs no provider call and no execution. Synchronization is a
    reconciliation checkpoint over existing durable state, not a second ingestion engine.
    """

    if item.entity_type == "workspace":
        current = (
            db.query(Project)
            .filter(
                Project.id == project.id,
                Project.created_by == user.id,
                Project.deleted_at.is_(None),
            )
            .first()
        )
        if current is None:
            raise ValueError("Workspace saknas eller ägs inte av användaren.")
        if (current.version or 1) != item.entity_version:
            raise StaleWorkspaceError("Workspace-versionen ändrades under synken.")
        return

    if item.entity_type == "document":
        current = (
            db.query(Document)
            .filter(
                Document.id == item.entity_id,
                Document.project_id == project.id,
                Document.uploaded_by == user.id,
                Document.deleted_at.is_(None),
            )
            .first()
        )
    elif item.entity_type == "task":
        current = (
            db.query(Task)
            .filter(
                Task.id == item.entity_id,
                Task.project_id == project.id,
                Task.created_by == user.id,
            )
            .first()
        )
    elif item.entity_type == "conversation":
        current = (
            db.query(Conversation)
            .filter(
                Conversation.id == item.entity_id,
                Conversation.project_id == project.id,
                Conversation.user_id == user.id,
            )
            .first()
        )
    elif item.entity_type == "decision":
        current = (
            db.query(WorkspaceDecision)
            .filter(
                WorkspaceDecision.id == item.entity_id,
                WorkspaceDecision.project_id == project.id,
                WorkspaceDecision.owner_id == user.id,
            )
            .first()
        )
    elif item.entity_type == "milestone":
        current = (
            db.query(WorkspaceMilestone)
            .filter(
                WorkspaceMilestone.id == item.entity_id,
                WorkspaceMilestone.project_id == project.id,
                WorkspaceMilestone.owner_id == user.id,
            )
            .first()
        )
    else:
        raise ValueError(f"Okänd sync-entitet: {item.entity_type}")

    if current is None:
        raise ValueError(f"{item.entity_type} finns inte i workspaceet längre.")


def _execute_sync_run(
    db: Session, *, run: WorkspaceSyncRun, project: Project, user: User
) -> WorkspaceSyncRun:
    now = datetime.utcnow()
    run.status = WorkspaceSyncStatus.running
    run.started_at = run.started_at or now
    run.completed_at = None
    run.error_category = None
    run.heartbeat_at = now

    items = (
        db.query(WorkspaceSyncItem)
        .filter(WorkspaceSyncItem.run_id == run.id)
        .order_by(WorkspaceSyncItem.entity_type.asc(), WorkspaceSyncItem.entity_id.asc())
        .all()
    )
    run.progress_total = len(items)
    run.progress_current = sum(
        1 for item in items if item.status in (WorkspaceSyncItemStatus.applied, WorkspaceSyncItemStatus.skipped)
    )
    db.flush()

    for item in items:
        if item.status in (WorkspaceSyncItemStatus.applied, WorkspaceSyncItemStatus.skipped):
            continue
        try:
            _apply_sync_entity(db, project=project, user=user, item=item)
            duplicate = _previous_sync_item(
                db,
                project=project,
                user=user,
                entity_type=item.entity_type,
                entity_id=item.entity_id,
                content_hash=item.content_hash,
            )
            item.status = (
                WorkspaceSyncItemStatus.skipped if duplicate else WorkspaceSyncItemStatus.applied
            )
        except StaleWorkspaceError as exc:
            item.status = WorkspaceSyncItemStatus.failed
            item.error = str(exc)[:512]
            run.status = WorkspaceSyncStatus.stale
            run.error_category = "stale_snapshot"
            run.completed_at = datetime.utcnow()
            run.result = {"stale": True, "reason": str(exc)}
            db.commit()
            return run
        except Exception as exc:  # noqa: BLE001 - persisted as a failed, retryable run
            item.status = WorkspaceSyncItemStatus.failed
            item.error = str(exc)[:512]
            run.status = WorkspaceSyncStatus.failed
            run.error_category = "sync_failed"
            run.completed_at = datetime.utcnow()
            run.result = {"failed_entity": item.entity_type, "retryable": True}
            db.commit()
            return run
        item.processed_at = datetime.utcnow()
        run.progress_current += 1
        run.heartbeat_at = datetime.utcnow()
        db.flush()

    current_version, current_hash, _ = _sync_entities(db, project, user)
    if current_hash != run.snapshot_hash or current_version != run.snapshot_version:
        run.status = WorkspaceSyncStatus.stale
        run.error_category = "stale_snapshot"
        run.result = {
            "stale": True,
            "current_snapshot_hash": current_hash,
            "current_version": current_version,
        }
    else:
        run.status = WorkspaceSyncStatus.completed
        applied = sum(1 for item in items if item.status == WorkspaceSyncItemStatus.applied)
        skipped = sum(1 for item in items if item.status == WorkspaceSyncItemStatus.skipped)
        run.result = {
            "unchanged": applied == 0,
            "applied": applied,
            "skipped": skipped,
            "entities": len(items),
        }
    run.completed_at = datetime.utcnow()
    run.heartbeat_at = run.completed_at
    db.commit()
    return run


def _create_sync_run(
    db: Session, *, project: Project, user: User, idempotency_key: str
) -> WorkspaceSyncRun:
    version, snapshot_hash, entities = _sync_entities(db, project, user)
    run = WorkspaceSyncRun(
        project_id=project.id,
        owner_id=user.id,
        idempotency_key=idempotency_key,
        status=WorkspaceSyncStatus.queued,
        snapshot_hash=snapshot_hash,
        snapshot_version=version,
        progress_total=len(entities),
        result={},
    )
    db.add(run)
    db.flush()
    for entity in entities:
        db.add(
            WorkspaceSyncItem(
                run_id=run.id,
                project_id=project.id,
                owner_id=user.id,
                entity_type=entity["entity_type"],
                entity_id=uuid.UUID(entity["entity_id"]),
                entity_version=entity["entity_version"],
                content_hash=entity["content_hash"],
                status=WorkspaceSyncItemStatus.pending,
            )
        )
    db.flush()
    return run


def _sync_run_out(db: Session, run: WorkspaceSyncRun) -> WorkspaceSyncRunOut:
    items = (
        db.query(WorkspaceSyncItem)
        .filter(WorkspaceSyncItem.run_id == run.id)
        .order_by(WorkspaceSyncItem.entity_type.asc(), WorkspaceSyncItem.entity_id.asc())
        .all()
    )
    out = WorkspaceSyncRunOut.model_validate(run)
    out.items = [WorkspaceSyncItemOut.model_validate(item) for item in items]
    return out


def _mainai_task_out(db: Session, task: MainAITask) -> MainAITaskOut:
    job = db.get(MainAIJob, task.mainai_job_id) if task.mainai_job_id is not None else None
    out = MainAITaskOut.model_validate(task)
    out.liveness = task_liveness(task, job).value
    return out


def _workspace_supervised(
    db: Session, project: Project, user: User, limit: int = 50, offset: int = 0
) -> WorkspaceSupervisedOut:
    goals = (
        db.query(MainAIGoal)
        .filter(MainAIGoal.project_id == project.id, MainAIGoal.owner_id == user.id)
        .order_by(MainAIGoal.created_at.desc(), MainAIGoal.id.asc())
        .limit(limit)
        .offset(offset)
        .all()
    )
    goal_by_id = {goal.id: goal for goal in goals}
    tasks = (
        db.query(MainAITask)
        .join(MainAIGoal, MainAITask.goal_id == MainAIGoal.id)
        .filter(
            MainAIGoal.project_id == project.id,
            MainAIGoal.owner_id == user.id,
            MainAITask.owner_id == user.id,
        )
        .order_by(MainAITask.created_at.asc(), MainAITask.id.asc())
        .limit(limit)
        .offset(offset)
        .all()
    )

    supervised: list[WorkspaceSupervisedTaskOut] = []
    for task in tasks:
        goal = goal_by_id.get(task.goal_id)
        if goal is None:
            continue
        events = (
            db.query(MainAITaskEvent)
            .filter(MainAITaskEvent.task_id == task.id, MainAITaskEvent.owner_id == user.id)
            .order_by(MainAITaskEvent.created_at.desc(), MainAITaskEvent.id.asc())
            .all()
        )
        checkpoints = (
            db.query(MainAICheckpoint)
            .filter(MainAICheckpoint.task_id == task.id, MainAICheckpoint.owner_id == user.id)
            .order_by(MainAICheckpoint.created_at.desc(), MainAICheckpoint.id.asc())
            .all()
        )
        waits = (
            db.query(MainAITaskWait)
            .filter(MainAITaskWait.task_id == task.id, MainAITaskWait.owner_id == user.id)
            .order_by(MainAITaskWait.created_at.asc(), MainAITaskWait.id.asc())
            .all()
        )
        recovery_records = (
            db.query(MainAIRecoveryRecord)
            .filter(
                MainAIRecoveryRecord.task_id == task.id,
                MainAIRecoveryRecord.owner_id == user.id,
            )
            .order_by(MainAIRecoveryRecord.created_at.asc(), MainAIRecoveryRecord.id.asc())
            .all()
        )
        job = db.get(MainAIJob, task.mainai_job_id) if task.mainai_job_id is not None else None
        verification_evidence = [
            {"event_type": event.event_type.value, "detail": event.detail, "created_at": event.created_at}
            for event in events
            if event.event_type
            in (
                MainAITaskEventType.verification_started,
                MainAITaskEventType.verification_passed,
                MainAITaskEventType.verification_failed,
                MainAITaskEventType.completed,
                MainAITaskEventType.failed,
                MainAITaskEventType.cancelled,
            )
        ]
        supervised.append(
            WorkspaceSupervisedTaskOut(
                goal=MainAIGoalOut.model_validate(goal),
                task=_mainai_task_out(db, task),
                events=[MainAITaskEventOut.model_validate(event) for event in events],
                checkpoints=[MainAICheckpointOut.model_validate(item) for item in checkpoints],
                waits=[MainAITaskWaitOut.model_validate(item) for item in waits],
                recovery_records=[
                    MainAIRecoveryRecordOut.model_validate(item) for item in recovery_records
                ],
                job=MainAIJobOut.model_validate(job) if job is not None else None,
                approval_granted=any(
                    event.event_type == MainAITaskEventType.approval_granted for event in events
                ),
                verification_evidence=verification_evidence,
            )
        )

    return WorkspaceSupervisedOut(
        goals=[MainAIGoalOut.model_validate(goal) for goal in goals],
        tasks=supervised,
    )


async def _build_context(
    db: Session,
    *,
    project: Project,
    user: User,
    query: str | None = None,
) -> WorkspaceContextOut:
    sources = _workspace_sources(db, project, user, limit=20)
    tasks = _workspace_tasks(db, project, user, limit=50)
    milestones = _workspace_milestones(db, project, user, limit=20)
    conversations = _workspace_conversations(db, project, user, limit=10)
    decisions = _workspace_decisions(db, project, user, limit=10)
    activities = _workspace_activities(db, project, user, limit=10)

    objectives = [item for item in [project.purpose, project.description] if item]
    objectives.extend(project.goals or [])

    uncertainties: list[dict] = []
    for source in sources:
        if source.active_truth_status in (
            ActiveTruthStatus.proposed,
            ActiveTruthStatus.disputed,
            ActiveTruthStatus.superseded,
        ):
            uncertainties.append(
                {
                    "kind": "source_status",
                    "document_id": str(source.id),
                    "title": source.title,
                    "status": source.active_truth_status.value,
                }
            )
    for decision in decisions:
        if decision.status == WorkspaceDecisionStatus.proposed:
            uncertainties.append(
                {
                    "kind": "proposed_decision",
                    "decision_id": str(decision.id),
                    "title": decision.title,
                    "status": decision.status.value,
                }
            )

    retrieval_sources: list[WorkspaceContextSourceOut] = []
    degraded_reason: str | None = None
    if query and query.strip():
        try:
            hits = await retrieve_context(
                db,
                user.id,
                query.strip(),
                top_k=8,
                project_id=project.id,
                effect_guard=True,
            )
            retrieval_sources = [
                WorkspaceContextSourceOut(
                    document_id=uuid.UUID(str(hit["document_id"])),
                    title=hit["title"],
                    snippet=hit["text"][:300],
                    score=hit["score"],
                    active_truth_status=hit.get("active_truth_status"),
                )
                for hit in hits
            ]
        except (GroundingUnavailableError, EgressDeniedError, ProviderError) as exc:
            # Context assembly is useful even when semantic retrieval is unavailable. The
            # founder gets the deterministic workspace facts plus an explicit degraded reason.
            degraded_reason = str(exc)

    return WorkspaceContextOut(
        workspace_id=project.id,
        context_version=project.version or 1,
        objectives=objectives,
        milestones=[milestone_out(db, item, user.id) for item in milestones],
        source_documents=[KnowledgeSourceOut.model_validate(source) for source in sources],
        decisions=[WorkspaceDecisionOut.model_validate(decision) for decision in decisions],
        outstanding_tasks=[
            task_out(db, task, user.id)
            for task in tasks
            if (task.status.value if hasattr(task.status, "value") else task.status)
            not in ("done", "cancelled")
        ],
        past_conversations=[ConversationOut.model_validate(conversation) for conversation in conversations],
        recent_changes=[WorkspaceActivityOut.model_validate(activity) for activity in activities],
        uncertainties=uncertainties,
        retrieval_sources=retrieval_sources,
        retrieval_degraded_reason=degraded_reason,
    )


def _detail(db: Session, project: Project, user: User, context: WorkspaceContextOut) -> WorkspaceDetailOut:
    return WorkspaceDetailOut(
        **WorkspaceOut.model_validate(project).model_dump(),
        tasks=[task_out(db, task, user.id) for task in _workspace_tasks(db, project, user)],
        milestones=[
            milestone_out(db, item, user.id)
            for item in _workspace_milestones(db, project, user)
        ],
        sources=[KnowledgeSourceOut.model_validate(source) for source in _workspace_sources(db, project, user)],
        conversations=[
            ConversationOut.model_validate(conversation)
            for conversation in _workspace_conversations(db, project, user)
        ],
        activities=[
            WorkspaceActivityOut.model_validate(activity)
            for activity in _workspace_activities(db, project, user)
        ],
        decisions=[
            WorkspaceDecisionOut.model_validate(decision)
            for decision in _workspace_decisions(db, project, user)
        ],
        context=context,
    )


@router.get("", response_model=list[WorkspaceOut])
def list_workspaces(
    include_archived: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    query = db.query(Project).filter(Project.created_by == user.id, Project.deleted_at.is_(None))
    if not include_archived:
        query = query.filter(Project.archived_at.is_(None))
    return (
        query.order_by(Project.updated_at.desc(), Project.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.post("", response_model=WorkspaceOut)
def create_workspace(payload: WorkspaceIn, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    project = Project(
        created_by=user.id,
        name=payload.name,
        description=payload.description,
        purpose=payload.purpose,
        goals=payload.goals,
        status=payload.status,
        settings=payload.settings,
        last_activity_at=datetime.utcnow(),
    )
    db.add(project)
    db.flush()
    db.add(WorkspaceMember(project_id=project.id, user_id=user.id, role=WorkspaceMemberRole.owner))
    _record_activity(
        db,
        project=project,
        user=user,
        kind="workspace.created",
        summary=f"Workspace skapades: {project.name}",
    )
    db.commit()
    db.refresh(project)
    return project


@router.get("/{project_id}", response_model=WorkspaceDetailOut)
async def get_workspace(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    context = await _build_context(db, project=project, user=user)
    return _detail(db, project, user, context)


@router.patch("/{project_id}", response_model=WorkspaceOut)
def update_workspace(
    project_id: uuid.UUID,
    payload: WorkspaceUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    changed = payload.model_dump(exclude_unset=True)
    expected_version = changed.pop("expected_version", None)
    if expected_version is not None and expected_version != (project.version or 1):
        raise HTTPException(
            status_code=409,
            detail=(
                "Workspace ändrades av en annan uppdatering. Läs in den senaste "
                "versionen och försök igen."
            ),
        )
    for key, value in changed.items():
        setattr(project, key, value)
    if changed:
        _touch_project(project)
        _record_activity(
            db,
            project=project,
            user=user,
            kind="workspace.updated",
            summary="Workspace-metadata uppdaterades",
            detail={"fields": sorted(changed), "version": project.version},
        )
    db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/archive", response_model=WorkspaceOut)
def archive_workspace(project_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    project = _owned_project(db, project_id, user)
    if project.archived_at is None:
        project.archived_at = datetime.utcnow()
        _touch_project(project)
        _record_activity(
            db,
            project=project,
            user=user,
            kind="workspace.archived",
            summary="Workspace arkiverades",
        )
        db.commit()
    db.refresh(project)
    return project


@router.post("/{project_id}/restore", response_model=WorkspaceOut)
def restore_workspace(project_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    project = _owned_project(db, project_id, user)
    if project.archived_at is not None:
        project.archived_at = None
        _touch_project(project)
        _record_activity(
            db,
            project=project,
            user=user,
            kind="workspace.restored",
            summary="Workspace återställdes",
        )
        db.commit()
    db.refresh(project)
    return project


@router.delete("/{project_id}")
def delete_workspace(project_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_founder)):
    project = _owned_project(db, project_id, user)
    project.deleted_at = datetime.utcnow()
    project.archived_at = project.archived_at or datetime.utcnow()
    _touch_project(project)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="workspace.deleted",
        summary="Workspace soft-raderades",
    )
    db.commit()
    return {"status": "deleted", "retained": True}


@router.get("/{project_id}/knowledge", response_model=list[KnowledgeSourceOut])
def list_workspace_knowledge(
    project_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [
        KnowledgeSourceOut.model_validate(source)
        for source in _workspace_sources(db, project, user, limit=limit, offset=offset)
    ]


@router.post("/{project_id}/documents/{document_id}", response_model=KnowledgeSourceOut)
def associate_document(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    document = (
        db.query(Document)
        .filter(Document.id == document_id, Document.uploaded_by == user.id, Document.deleted_at.is_(None))
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Källan hittades inte.")
    previous = document.project_id
    document.project_id = project.id
    _touch_project(project)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="document.associated",
        summary=f"Källa knöts till workspace: {document.title}",
        detail={"document_id": str(document.id), "previous_project_id": str(previous) if previous else None},
    )
    db.commit()
    db.refresh(document)
    return document


@router.delete("/{project_id}/documents/{document_id}", response_model=KnowledgeSourceOut)
def disassociate_document(
    project_id: uuid.UUID,
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    document = (
        db.query(Document)
        .filter(
            Document.id == document_id,
            Document.uploaded_by == user.id,
            Document.project_id == project.id,
            Document.deleted_at.is_(None),
        )
        .first()
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Källan finns inte i det här workspaceet.")
    document.project_id = None
    _touch_project(project)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="document.disassociated",
        summary=f"Källa kopplades loss: {document.title}",
        detail={"document_id": str(document.id)},
    )
    db.commit()
    db.refresh(document)
    return document


@router.get("/{project_id}/tasks", response_model=list[TaskOut])
def list_workspace_tasks(
    project_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [
        task_out(db, task, user.id)
        for task in _workspace_tasks(db, project, user, limit=limit, offset=offset)
    ]


@router.post("/{project_id}/tasks", response_model=TaskOut)
def create_workspace_task(
    project_id: uuid.UUID,
    payload: TaskIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    task = create_task(
        db,
        owner_id=user.id,
        project_id=project.id,
        title=payload.title,
        description=payload.description,
        status=payload.status,
        priority=payload.priority,
        due_at=payload.due_at,
        milestone_id=payload.milestone_id,
        depends_on_task_ids=payload.depends_on_task_ids,
    )
    _touch_project(project)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="task.created",
        summary=f"Uppgift skapades: {task.title}",
        detail={"task_id": str(task.id)},
    )
    db.commit()
    db.refresh(task)
    return task


@router.get("/{project_id}/conversations", response_model=list[ConversationOut])
def list_workspace_conversations(
    project_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [
        ConversationOut.model_validate(conversation)
        for conversation in _workspace_conversations(
            db, project, user, limit=limit, offset=offset
        )
    ]


@router.post("/{project_id}/conversations", response_model=ConversationOut)
def create_workspace_conversation(
    project_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    conversation = Conversation(
        user_id=user.id,
        project_id=project.id,
        title=f"Workspace: {project.name}"[:256],
    )
    db.add(conversation)
    _touch_project(project)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="conversation.created",
        summary="En workspace-konversation startades",
        detail={"conversation_id": str(conversation.id)},
    )
    db.commit()
    db.refresh(conversation)
    return conversation


@router.get("/{project_id}/activity", response_model=list[WorkspaceActivityOut])
def list_workspace_activity(
    project_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [
        WorkspaceActivityOut.model_validate(activity)
        for activity in _workspace_activities(db, project, user, limit=limit, offset=offset)
    ]


@router.get("/{project_id}/decisions", response_model=list[WorkspaceDecisionOut])
def list_workspace_decisions(
    project_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return [
        WorkspaceDecisionOut.model_validate(decision)
        for decision in _workspace_decisions(db, project, user, limit=limit, offset=offset)
    ]


@router.post("/{project_id}/decisions", response_model=WorkspaceDecisionOut)
def create_workspace_decision(
    project_id: uuid.UUID,
    payload: WorkspaceDecisionIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    decision = WorkspaceDecision(
        project_id=project.id,
        owner_id=user.id,
        title=payload.title,
        decision=payload.decision,
        rationale=payload.rationale,
        status=payload.status,
        version=1,
        epistemic_status=payload.epistemic_status,
        origin=payload.origin,
    )
    db.add(decision)
    _touch_project(project)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="decision.recorded",
        summary=f"Beslut registrerades: {decision.title}",
        detail={"decision_id": str(decision.id)},
    )
    db.commit()
    db.refresh(decision)
    return decision


@router.post(
    "/{project_id}/decisions/{decision_id}/supersede", response_model=WorkspaceDecisionOut
)
def supersede_workspace_decision(
    project_id: uuid.UUID,
    decision_id: uuid.UUID,
    payload: WorkspaceDecisionSupersedeIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    previous = (
        db.query(WorkspaceDecision)
        .filter(
            WorkspaceDecision.id == decision_id,
            WorkspaceDecision.project_id == project.id,
            WorkspaceDecision.owner_id == user.id,
        )
        .first()
    )
    if previous is None:
        raise HTTPException(status_code=404, detail="Beslutet hittades inte.")
    if previous.status == WorkspaceDecisionStatus.superseded:
        raise HTTPException(status_code=409, detail="Beslutet är redan ersatt.")
    previous.status = WorkspaceDecisionStatus.superseded
    previous.superseded_at = datetime.utcnow()
    previous.updated_at = datetime.utcnow()
    decision = WorkspaceDecision(
        project_id=project.id,
        owner_id=user.id,
        title=payload.title,
        decision=payload.decision,
        rationale=payload.rationale,
        status=payload.status,
        version=(previous.version or 1) + 1,
        supersedes_id=previous.id,
        epistemic_status=payload.epistemic_status,
        origin=payload.origin,
    )
    db.add(decision)
    _touch_project(project)
    _record_activity(
        db,
        project=project,
        user=user,
        kind="decision.superseded",
        summary=f"Beslut ersattes: {previous.title}",
        detail={"previous_decision_id": str(previous.id), "decision_id": str(decision.id)},
    )
    db.commit()
    db.refresh(decision)
    return decision


@router.get("/{project_id}/context", response_model=WorkspaceContextOut)
async def get_workspace_context(
    project_id: uuid.UUID,
    q: str | None = Query(None, max_length=2000),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return await _build_context(db, project=project, user=user, query=q)


@router.get("/{project_id}/supervised-tasks", response_model=WorkspaceSupervisedOut)
def list_workspace_supervised_tasks(
    project_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    return _workspace_supervised(db, project, user, limit=limit, offset=offset)


@router.get("/{project_id}/sync", response_model=list[WorkspaceSyncRunOut])
def list_workspace_sync_runs(
    project_id: uuid.UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    if _reconcile_interrupted_syncs(db, project, user):
        db.commit()
    runs = (
        db.query(WorkspaceSyncRun)
        .filter(
            WorkspaceSyncRun.project_id == project.id,
            WorkspaceSyncRun.owner_id == user.id,
        )
        .order_by(WorkspaceSyncRun.created_at.desc(), WorkspaceSyncRun.id.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [_sync_run_out(db, run) for run in runs]


@router.post("/{project_id}/sync", response_model=WorkspaceSyncRunOut)
def start_workspace_sync(
    project_id: uuid.UUID,
    payload: WorkspaceSyncIn,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    reconciled = _reconcile_interrupted_syncs(db, project, user)

    existing = (
        db.query(WorkspaceSyncRun)
        .filter(
            WorkspaceSyncRun.project_id == project.id,
            WorkspaceSyncRun.owner_id == user.id,
            WorkspaceSyncRun.idempotency_key == payload.idempotency_key,
        )
        .first()
    )
    if existing is not None:
        if reconciled:
            db.commit()
        return _sync_run_out(db, existing)
    if reconciled:
        db.commit()

    try:
        run = _create_sync_run(
            db,
            project=project,
            user=user,
            idempotency_key=payload.idempotency_key,
        )
        db.commit()
    except IntegrityError:
        # A concurrent request won the same idempotency key. Replay its run rather than
        # creating a second ledger or a second reconciliation pass.
        db.rollback()
        existing = (
            db.query(WorkspaceSyncRun)
            .filter(
                WorkspaceSyncRun.project_id == project.id,
                WorkspaceSyncRun.owner_id == user.id,
                WorkspaceSyncRun.idempotency_key == payload.idempotency_key,
            )
            .first()
        )
        if existing is None:
            raise
        return _sync_run_out(db, existing)

    run = _execute_sync_run(db, run=run, project=project, user=user)
    return _sync_run_out(db, run)


@router.get("/{project_id}/sync/{run_id}", response_model=WorkspaceSyncRunOut)
def get_workspace_sync_run(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    run = (
        db.query(WorkspaceSyncRun)
        .filter(
            WorkspaceSyncRun.id == run_id,
            WorkspaceSyncRun.project_id == project.id,
            WorkspaceSyncRun.owner_id == user.id,
        )
        .first()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Synkroniseringen hittades inte.")
    return _sync_run_out(db, run)


@router.post("/{project_id}/sync/{run_id}/retry", response_model=WorkspaceSyncRunOut)
def retry_workspace_sync(
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    user: User = Depends(require_founder),
):
    project = _owned_project(db, project_id, user)
    run = (
        db.query(WorkspaceSyncRun)
        .filter(
            WorkspaceSyncRun.id == run_id,
            WorkspaceSyncRun.project_id == project.id,
            WorkspaceSyncRun.owner_id == user.id,
        )
        .first()
    )
    if run is None:
        raise HTTPException(status_code=404, detail="Synkroniseringen hittades inte.")
    if run.status not in (WorkspaceSyncStatus.failed, WorkspaceSyncStatus.stale):
        raise HTTPException(status_code=409, detail="Endast en misslyckad synk kan köras om.")

    version, snapshot_hash, _ = _sync_entities(db, project, user)
    if snapshot_hash != run.snapshot_hash or version != run.snapshot_version:
        raise HTTPException(
            status_code=409,
            detail=(
                "Workspace ändrades efter den här synken. Starta en ny synk i stället för "
                "att återanvända ett gammalt resultat."
            ),
        )

    items = db.query(WorkspaceSyncItem).filter(WorkspaceSyncItem.run_id == run.id).all()
    for item in items:
        item.status = WorkspaceSyncItemStatus.pending
        item.error = None
        item.processed_at = None
    run.attempt += 1
    run.progress_current = 0
    db.commit()

    run = _execute_sync_run(db, run=run, project=project, user=user)
    return _sync_run_out(db, run)
