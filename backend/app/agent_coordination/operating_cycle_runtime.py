"""Persistent, lease-fenced RT3 operating-cycle runner.

The cycle is scheduling state only. Authority remains the current execution envelope,
STOP epoch, broker-produced WorkforceAssignment and independent verification registry.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.execution_envelopes import get_current_execution_envelope
from app.mainai_execution.graph import recompute_task_readiness
from app.mainai_verification_registry import find_independent_pass
from app.models.execution_envelope import ExecutionAuthorizationEnvelope
from app.models.mainai_execution import (
    MainAIGoal,
    MainAIGoalStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.operating_cycle import AuthorizedOperatingCycle
from app.models.workforce import WorkforceAssignment
from app.workforce.authority import AuthorityEnvelopeError, require_live_assignment_authority
from app.workforce.broker import DelegationBrokerError, mark_verification, resolve_delegation, submit_delegation_request
from app.workforce.kill_switch import KillSwitchError, assert_grant_allowed
from app.workforce.registry import WorkforceRegistryError, get_workforce_agent

ACTIVE_STATES = ("pending", "delegating", "awaiting_verification", "blocked")
TERMINAL_STATES = ("verified", "failed", "cancelled")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class CycleAuthorityError(RuntimeError):
    pass


def create_authorized_cycle(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    envelope_id: uuid.UUID,
    verifier_profile_id: uuid.UUID,
    max_attempts: int = 3,
) -> AuthorizedOperatingCycle:
    """Founder-facing creation primitive. Client fields are comparisons, never authority."""
    goal = db.execute(select(MainAIGoal).where(MainAIGoal.id == goal_id, MainAIGoal.owner_id == owner_id)).scalar_one_or_none()
    if goal is None or goal.status != MainAIGoalStatus.running:
        raise CycleAuthorityError("goal is missing, cross-owner, or not running")
    current = get_current_execution_envelope(db, owner_id=owner_id, goal_id=goal_id)
    if current is None or current.id != envelope_id:
        raise CycleAuthorityError("execution envelope is absent, superseded, or mismatched")
    get_workforce_agent(db, owner_id=owner_id, agent_id=verifier_profile_id)
    fence = assert_grant_allowed(db, owner_id=owner_id)
    db.execute(
        text("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended(:key, 9))"),
        {"key": f"rt3-cycle:{owner_id}:{goal_id}:{envelope_id}"},
    )
    existing = db.execute(
        select(AuthorizedOperatingCycle).where(
            AuthorizedOperatingCycle.owner_id == owner_id,
            AuthorizedOperatingCycle.goal_id == goal_id,
            AuthorizedOperatingCycle.execution_envelope_id == envelope_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.verifier_profile_id != verifier_profile_id:
            raise CycleAuthorityError("cycle idempotency conflict")
        return existing
    cycle = AuthorizedOperatingCycle(
        owner_id=owner_id,
        goal_id=goal_id,
        execution_envelope_id=envelope_id,
        verifier_profile_id=verifier_profile_id,
        external_agent_id=None,
        state="pending",
        max_attempts=max_attempts,
        authority_epoch=int(fence["owner_epoch"]),
        global_authority_epoch=int(fence["global_epoch"]),
        provenance={
            "created_by": "founder",
            "first_cycle_go": False,
            "silence_is_not_authority": True,
            "provider_dispatch_claimed": False,
        },
    )
    db.add(cycle)
    db.flush()
    return cycle


def bind_cycle_dispatch(
    db: Session,
    *,
    owner_id: uuid.UUID,
    cycle_id: uuid.UUID,
    external_agent_id: str,
    dispatch_receipt: str,
) -> AuthorizedOperatingCycle:
    """Bind evidence of an already-authorized dispatch; never dispatches itself."""
    agent_id = external_agent_id.strip()
    receipt = dispatch_receipt.strip()
    if not agent_id or not receipt:
        raise CycleAuthorityError("dispatch binding requires external id and receipt")
    cycle = db.execute(
        select(AuthorizedOperatingCycle)
        .where(AuthorizedOperatingCycle.id == cycle_id, AuthorizedOperatingCycle.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if cycle is None or cycle.state != "awaiting_dispatch" or cycle.workforce_assignment_id is None:
        raise CycleAuthorityError("cycle is not awaiting a dispatch binding")
    _assert_cycle_authority(db, cycle)
    assignment = db.execute(
        select(WorkforceAssignment)
        .where(WorkforceAssignment.id == cycle.workforce_assignment_id, WorkforceAssignment.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if assignment is None:
        raise CycleAuthorityError("bound assignment is unavailable")
    require_live_assignment_authority(assignment)
    cycle.external_agent_id = agent_id
    cycle.state = "awaiting_provider"
    cycle.next_attempt_at = None
    assignment.status = "running"
    provenance = dict(assignment.provenance or {})
    provenance["rt3_binding"] = {
        "cycle_id": str(cycle.id),
        "external_agent_id": agent_id,
        "provider": cycle.provider,
        "authority_epoch": cycle.authority_epoch,
        "dispatch_receipt": receipt,
        "recorded_by": "founder",
    }
    assignment.provenance = provenance
    db.flush()
    return cycle


def claim_next_cycle(
    db: Session, *, worker_id: str, lease_seconds: int, limit_states: tuple[str, ...] = ACTIVE_STATES
) -> tuple[uuid.UUID, uuid.UUID, int] | None:
    """Cross-owner worker claim using SKIP LOCKED and a monotonically increasing fence."""
    row = db.execute(
        text(
            """
            SELECT id, owner_id FROM authorized_operating_cycles
            WHERE state = ANY(:states)
              AND (state <> 'blocked' OR next_attempt_at IS NOT NULL)
              AND (next_attempt_at IS NULL OR next_attempt_at <= now())
              AND (locked_by IS NULL OR lease_expires_at < now())
            ORDER BY created_at, id
            FOR UPDATE SKIP LOCKED LIMIT 1
            """
        ),
        {"states": list(limit_states)},
    ).mappings().first()
    if row is None:
        db.commit()
        return None
    claimed = db.execute(
        text(
            """
            UPDATE authorized_operating_cycles
            SET locked_by=:worker, lease_generation=lease_generation+1,
                lease_expires_at=now() + (:lease || ' seconds')::interval, updated_at=now()
            WHERE id=:id RETURNING lease_generation
            """
        ),
        {"worker": worker_id, "lease": max(30, int(lease_seconds)), "id": row["id"]},
    ).scalar_one()
    db.commit()
    return uuid.UUID(str(row["id"])), uuid.UUID(str(row["owner_id"])), int(claimed)


def _fenced_cycle(db: Session, *, cycle_id: uuid.UUID, worker_id: str, generation: int) -> AuthorizedOperatingCycle:
    row = db.execute(
        select(AuthorizedOperatingCycle)
        .where(
            AuthorizedOperatingCycle.id == cycle_id,
            AuthorizedOperatingCycle.locked_by == worker_id,
            AuthorizedOperatingCycle.lease_generation == generation,
            AuthorizedOperatingCycle.state.notin_(TERMINAL_STATES),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if row is None:
        raise CycleAuthorityError("cycle lease lost")
    return row


def _assert_cycle_authority(db: Session, cycle: AuthorizedOperatingCycle) -> ExecutionAuthorizationEnvelope:
    fence = assert_grant_allowed(db, owner_id=cycle.owner_id)
    if int(fence["owner_epoch"]) != cycle.authority_epoch or int(fence["global_epoch"]) != cycle.global_authority_epoch:
        raise CycleAuthorityError("authority epoch changed; old cycle cannot resume")
    current = get_current_execution_envelope(db, owner_id=cycle.owner_id, goal_id=cycle.goal_id)
    if current is None or current.id != cycle.execution_envelope_id:
        raise CycleAuthorityError("execution envelope superseded or revoked")
    return current


def _release(cycle: AuthorizedOperatingCycle) -> None:
    cycle.locked_by = None
    cycle.lease_expires_at = None
    cycle.updated_at = datetime.utcnow()


def _backoff(cycle: AuthorizedOperatingCycle, code: str) -> None:
    cycle.attempt_count += 1
    cycle.last_error_code = code
    if cycle.attempt_count >= cycle.max_attempts:
        cycle.state = "failed"
        cycle.completed_at = datetime.utcnow()
    else:
        cycle.state = "blocked"
        cycle.next_attempt_at = datetime.utcnow() + timedelta(seconds=min(300, 2 ** cycle.attempt_count))


def _next_ready_task(db: Session, cycle: AuthorizedOperatingCycle) -> MainAITask | None:
    recompute_task_readiness(db, goal_id=cycle.goal_id)
    return db.execute(
        select(MainAITask)
        .where(
            MainAITask.owner_id == cycle.owner_id,
            MainAITask.goal_id == cycle.goal_id,
            MainAITask.status == MainAITaskStatus.ready,
        )
        .order_by(MainAITask.priority.desc(), MainAITask.created_at, MainAITask.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    ).scalar_one_or_none()


def run_claimed_cycle_step(
    db: Session, *, cycle_id: uuid.UUID, owner_id: uuid.UUID, worker_id: str, generation: int
) -> dict:
    """Advance at most one bounded state transition and commit it atomically."""
    cycle = _fenced_cycle(db, cycle_id=cycle_id, worker_id=worker_id, generation=generation)
    if cycle.owner_id != owner_id:
        raise CycleAuthorityError("cycle owner mismatch")
    try:
        _assert_cycle_authority(db, cycle)
        if cycle.state in {"pending", "delegating", "blocked"} and cycle.workforce_assignment_id is None:
            task = _next_ready_task(db, cycle)
            if task is None:
                remaining = db.execute(
                    select(MainAITask.id).where(
                        MainAITask.owner_id == owner_id,
                        MainAITask.goal_id == cycle.goal_id,
                        MainAITask.status.notin_((MainAITaskStatus.completed, MainAITaskStatus.cancelled, MainAITaskStatus.failed)),
                    )
                ).first()
                cycle.state = "verified" if remaining is None else "blocked"
                cycle.completed_at = datetime.utcnow() if remaining is None else None
                cycle.next_attempt_at = None if remaining is None else datetime.utcnow() + timedelta(seconds=30)
            else:
                cycle.state = "delegating"
                savepoint = db.begin_nested()
                try:
                    request = submit_delegation_request(
                        db,
                        owner_id=owner_id,
                        goal_id=cycle.goal_id,
                        task_id=task.id,
                        goal_text=task.description,
                        required_capability=task.task_type,
                        verification_requirement="independent_verifier",
                        provenance={"source": "rt3_operating_cycle", "cycle_id": str(cycle.id)},
                    )
                    assignment = resolve_delegation(
                        db,
                        owner_id=owner_id,
                        request=request,
                        verifier_profile_id=cycle.verifier_profile_id,
                        required_provider_type=cycle.provider,
                    )
                    savepoint.commit()
                except Exception:
                    savepoint.rollback()
                    raise
                assignment.execution_envelope_id = cycle.execution_envelope_id
                cycle.current_task_id = task.id
                cycle.workforce_assignment_id = assignment.id
                cycle.state = "awaiting_dispatch"
                assignment.status = "assigned"
                provenance = dict(assignment.provenance or {})
                provenance["rt3_binding"] = {
                    "cycle_id": str(cycle.id),
                    "external_agent_id": cycle.external_agent_id,
                    "provider": cycle.provider,
                    "authority_epoch": cycle.authority_epoch,
                }
                assignment.provenance = provenance
        elif cycle.state == "awaiting_verification":
            if not cycle.candidate_sha or not SHA_RE.fullmatch(cycle.candidate_sha):
                cycle.state = "blocked"
                cycle.last_error_code = "candidate_sha_missing"
                cycle.next_attempt_at = datetime.utcnow() + timedelta(seconds=30)
            else:
                assignment = db.get(WorkforceAssignment, cycle.workforce_assignment_id)
                if assignment is None or assignment.owner_id != owner_id:
                    raise CycleAuthorityError("workforce assignment missing or cross-owner")
                require_live_assignment_authority(assignment)
                profile = get_workforce_agent(db, owner_id=owner_id, agent_id=assignment.profile_id)
                passed = find_independent_pass(
                    db,
                    component_id=f"workforce_assignment:{assignment.id}",
                    candidate_sha=cycle.candidate_sha,
                    builder_identity=profile.agent_key,
                    owner_id=owner_id,
                )
                if passed is None:
                    cycle.next_attempt_at = datetime.utcnow() + timedelta(seconds=30)
                else:
                    verifier = get_workforce_agent(db, owner_id=owner_id, agent_id=cycle.verifier_profile_id)
                    if passed.examiner_identity != verifier.agent_key:
                        cycle.next_attempt_at = datetime.utcnow() + timedelta(seconds=30)
                        _release(cycle)
                        db.commit()
                        return {
                            "outcome": "verification_required",
                            "reason": "registered PASS is not from the assigned verifier",
                            "cycle_id": str(cycle.id),
                            "state": cycle.state,
                            "first_cycle_go": False,
                        }
                    mark_verification(
                        db,
                        owner_id=owner_id,
                        assignment=assignment,
                        status="VERIFIED",
                        verifier_profile_id=cycle.verifier_profile_id,
                    )
                    task = db.get(MainAITask, cycle.current_task_id)
                    if task is None or task.owner_id != owner_id:
                        raise CycleAuthorityError("cycle task missing or cross-owner")
                    task.status = MainAITaskStatus.completed
                    task.completed_at = datetime.utcnow()
                    db.add(
                        MainAITaskEvent(
                            task_id=task.id,
                            owner_id=owner_id,
                            event_type=MainAITaskEventType.completed,
                            detail={
                                "source": "rt3_independent_verification",
                                "candidate_sha": cycle.candidate_sha,
                                "verification_receipt_id": str(passed.id),
                                "workforce_assignment_id": str(assignment.id),
                            },
                        )
                    )
                    cycle.current_task_id = None
                    cycle.workforce_assignment_id = None
                    cycle.external_agent_id = None
                    cycle.candidate_sha = None
                    cycle.state = "pending"
                    cycle.attempt_count = 0
                    cycle.next_attempt_at = None
                    recompute_task_readiness(db, goal_id=cycle.goal_id)
        _release(cycle)
        db.commit()
        return {"outcome": "advanced", "cycle_id": str(cycle.id), "state": cycle.state, "first_cycle_go": False}
    except (CycleAuthorityError, KillSwitchError, AuthorityEnvelopeError) as exc:
        cycle.state = "blocked"
        cycle.last_error_code = "authority_invalid"
        cycle.next_attempt_at = None
        _release(cycle)
        db.commit()
        return {"outcome": "blocked", "reason": str(exc), "cycle_id": str(cycle.id), "first_cycle_go": False}
    except (DelegationBrokerError, WorkforceRegistryError) as exc:
        _backoff(cycle, "delegation_failed")
        _release(cycle)
        db.commit()
        return {"outcome": "retry_scheduled", "reason": str(exc), "cycle_id": str(cycle.id), "first_cycle_go": False}
