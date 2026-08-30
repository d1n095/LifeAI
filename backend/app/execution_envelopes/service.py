"""Life Execution Authorization Envelope -- the staging layer between a proposed execution
scope (MainAI's own suggestion) and real, founder-granted execution authority for a
MainAIGoal. See migration 0057's own module docstring for the full architecture.

Hard rule, structural not just documented: `propose_execution_scope()` NEVER writes to
`execution_authorization_envelopes`, directly or indirectly. The ONLY function in this module
that can create an `ExecutionAuthorizationEnvelope` is `authorize_execution_scope()`, and it
ALWAYS requires the caller to supply `authorized_by`/`authorized_paths`/
`authorized_capabilities`/`authorized_risk` explicitly -- the proposal's own suggested values
are never silently copied in. A `proposed_risk="low"` proposal does not imply
`authorized_risk="low"`; the founder's own decision, passed explicitly, is what actually
grants it -- the founder may accept the proposal as-is (by passing the same values back),
narrow it, or explicitly expand it. This module only decides WHETHER to call
`authorize_execution_scope()` and WITH WHAT ARGUMENTS -- WHETHER THE CALLER IS ALLOWED TO
remains entirely the caller's own responsibility (its router, gated by
`Depends(require_founder)`), exactly like `app.work_candidates.service.
authorize_work_candidate()` already established for the layer directly below this one."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.execution_envelope import ExecutionAuthorizationEnvelope, ExecutionScopeProposal
from app.models.mainai_execution import MainAIGoal


class ExecutionEnvelopeError(ValueError):
    pass


def _same(row: ExecutionScopeProposal, values: dict[str, Any]) -> ExecutionScopeProposal:
    differing = [key for key, value in values.items() if getattr(row, key) != value]
    if differing:
        raise ExecutionEnvelopeError(f"idempotency key reused with different fields: {', '.join(sorted(differing))}")
    return row


def propose_execution_scope(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    idempotency_key: str,
    repository_identity: str | None = None,
    proposed_paths: list[str] | None = None,
    proposed_capabilities: list[str] | None = None,
    proposed_risk: str = "low",
    proposal_reasoning: str | None = None,
    proposal_strategy: str = "unknown",
    provenance: dict[str, Any] | None = None,
) -> ExecutionScopeProposal:
    """Records ONE execution scope proposal -- never a claim that this scope is authorized,
    only a claim that MainAI suggests a goal might need it. `proposed_paths` deliberately
    defaults to an empty list, never a guessed path: this module has no reliable signal for
    WHICH files a goal should touch (see migration 0057's own module docstring on why a
    task_type-derived guess was explicitly rejected) -- an honest empty proposal, for the
    founder to fill in or for later planning to request explicitly (see the "required_scope -
    authorized_scope != empty -> blocked" doctrine this foundation exists to eventually
    support), beats a fabricated one that only looks informative.

    Fails closed BEFORE any write if `goal_id` does not structurally belong to `owner_id` -- a
    clear, typed error here, with the database's own composite FK (migration 0057) as the
    final backstop."""

    goal = db.execute(select(MainAIGoal).where(MainAIGoal.id == goal_id, MainAIGoal.owner_id == owner_id)).scalar_one_or_none()
    if goal is None:
        raise ExecutionEnvelopeError(f"goal_id={goal_id} does not belong to owner_id={owner_id}")

    values: dict[str, Any] = dict(
        goal_id=goal_id, repository_identity=repository_identity, proposed_paths=proposed_paths or [],
        proposed_capabilities=proposed_capabilities or [], proposed_risk=proposed_risk,
        proposal_reasoning=proposal_reasoning, proposal_strategy=proposal_strategy, provenance=provenance or {},
    )
    existing = db.execute(
        select(ExecutionScopeProposal).where(ExecutionScopeProposal.owner_id == owner_id, ExecutionScopeProposal.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        return _same(existing, values)

    # Incidental but real: this INSERT's own composite FK to mainai_goals(id, owner_id) makes
    # Postgres take an implicit FOR KEY SHARE lock on the goal row, which already contends
    # with execute_takeover()'s explicit FOR UPDATE lock on that same row (see
    # authorize_execution_scope()'s own FIRST-GOVERNANCE TOCTOU FENCE docstring below) --
    # never relied upon AS the fence (a later authorize_execution_scope() call against an
    # already-existing, already-committed proposal from an earlier transaction gets none of
    # this insert's protection), but real defense-in-depth worth knowing about, not assuming.
    #
    # Genuine concurrent callers with the SAME idempotency_key (e.g. two truly simultaneous
    # calls -- Path A's automatic trigger and a founder's own explicit Path B proposal racing,
    # or a client-side retry) can both pass the existing-row check above (neither committed
    # yet) and both attempt an insert. uq_execution_scope_proposals_idem correctly prevents a
    # duplicate row, but a plain INSERT's loser would raise an unhandled IntegrityError
    # instead of gracefully returning the winner's row -- the exact same race shape as
    # app.mainai_execution.planner.create_plan()'s own goal-row lock fix (found and closed the
    # same session this function's own Path B caller was added). Uses
    # INSERT ... ON CONFLICT DO NOTHING (this project's own established atomic primitive for
    # this exact idempotent-insert shape, see e.g. provider_spend/service.py's reservation
    # insert) rather than a SAVEPOINT+exception-catch -- a genuine CHECK-constraint violation
    # (e.g. an invalid proposed_risk) still raises normally, uncaught, since ON CONFLICT only
    # ever suppresses the target unique-index conflict, never any other constraint.
    row_id = uuid.uuid4()
    now = datetime.utcnow()
    stmt = (
        pg_insert(ExecutionScopeProposal.__table__)
        .values(id=row_id, owner_id=owner_id, idempotency_key=idempotency_key, status="unreviewed", observed_at=now, created_at=now, updated_at=now, **values)
        .on_conflict_do_nothing(index_elements=["owner_id", "idempotency_key"])
    )
    result = db.execute(stmt)
    db.flush()
    if result.rowcount == 0:
        # Lost the race (or this is a genuine idempotent replay) -- the winner's row is the
        # one now durably present under this (owner_id, idempotency_key) pair.
        winner = db.execute(
            select(ExecutionScopeProposal).where(
                ExecutionScopeProposal.owner_id == owner_id, ExecutionScopeProposal.idempotency_key == idempotency_key
            )
        ).scalar_one()
        return _same(winner, values)
    return db.get(ExecutionScopeProposal, row_id)


def reject_execution_scope(db: Session, *, owner_id: uuid.UUID, proposal_id: uuid.UUID, reason: str) -> ExecutionScopeProposal:
    """An explicit "this proposed scope is not right, and no envelope should be authorized
    from it" outcome -- never deletes the row, so the same rejected proposal is not
    re-surfaced for review indefinitely without a durable record that it was already
    considered."""

    row = db.execute(
        select(ExecutionScopeProposal).where(ExecutionScopeProposal.id == proposal_id, ExecutionScopeProposal.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ExecutionEnvelopeError("execution scope proposal is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise ExecutionEnvelopeError(f"execution scope proposal is already {row.status}, not unreviewed")
    row.status = "rejected"
    row.rejected_reason = reason
    row.updated_at = datetime.utcnow()
    db.flush()
    return row


def authorize_execution_scope(
    db: Session,
    *,
    owner_id: uuid.UUID,
    proposal_id: uuid.UUID,
    authorized_by: str,
    authorized_paths: list[str],
    authorized_capabilities: list[str],
    authorized_risk: str,
    envelope_idempotency_key: str,
) -> tuple[ExecutionScopeProposal, ExecutionAuthorizationEnvelope]:
    """The ONLY path from a proposed execution scope to real, founder-granted execution
    authority -- PROPOSED_SCOPE != AUTHORIZED_SCOPE enforced here, not just in the schema's
    own CHECK constraints. `authorized_by`/`authorized_paths`/`authorized_capabilities`/
    `authorized_risk` are ALWAYS the caller's own explicit assertion (this function has no
    default that reads them off the proposal itself) -- accepting the proposal as-is means the
    caller passes the SAME `proposed_paths`/`proposed_capabilities`/`proposed_risk` back
    explicitly, narrowing or expanding means passing different ones; either way it is a
    deliberate, reviewed act of granting authority, never an automatic copy.

    If this goal already has a current (`status='active'`) envelope, it is superseded (never
    mutated) by the new one -- the founder's original authorization decision remains durably
    auditable even after a later re-authorization changes it.

    FIRST-GOVERNANCE TOCTOU FENCE: locks the goal row (`SELECT ... FOR UPDATE`) BEFORE this
    transition, for the exact same reason `app.mainai_execution.recovery_takeover.
    execute_takeover()` locks the identical row before its own `goal_has_ever_been_envelope_
    governed()` decision -- a founder authorizing a goal's FIRST-EVER envelope and a
    concurrent dead-job recovery pass for that same goal must be strictly serialized. Without
    this, `execute_takeover()` could read EVER_GOVERNED=false, this function could commit the
    first envelope a moment later, and the recovery pass would still dispatch through V0.1's
    envelope-blind executor -- a real window, not a hypothetical one, since neither side
    previously took any lock in common. Both orderings are safe (whichever transaction gets
    here first fully completes before the other proceeds); only "governance becomes effective
    mid-flight of an already-decided legacy dispatch" is forbidden, and holding this lock
    across the whole decision closes exactly that window. See recovery_takeover.py's own
    matching docstring and, in test_recovery_takeover_authority_fencing.py,
    test_first_governance_toctou_race_governance_committed_while_recovery_waits_is_observed /
    test_first_governance_toctou_race_recovery_committed_first_then_governance_follows (both
    orderings) plus test_authorize_execution_scope_itself_locks_the_goal_row_no_manual_
    prelock_needed (proves this function's OWN lock, not a caller's, closes the window)."""

    row = db.execute(
        select(ExecutionScopeProposal).where(ExecutionScopeProposal.id == proposal_id, ExecutionScopeProposal.owner_id == owner_id).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ExecutionEnvelopeError("execution scope proposal is missing or belongs to another owner")
    if row.status != "unreviewed":
        raise ExecutionEnvelopeError(f"execution scope proposal is already {row.status}, not unreviewed")

    # The goal-row lock the docstring above promises: taken here (not earlier, since goal_id
    # is only known once the proposal row above is read) and held through the envelope
    # creation below. execute_takeover() only ever locks the goal row, never the proposal row,
    # so this ordering (proposal, then goal) can never form a lock-ordering cycle with it.
    db.execute(
        select(MainAIGoal).where(MainAIGoal.id == row.goal_id, MainAIGoal.owner_id == owner_id).with_for_update()
    ).scalar_one()

    prior_envelope = db.execute(
        select(ExecutionAuthorizationEnvelope).where(
            ExecutionAuthorizationEnvelope.owner_id == owner_id,
            ExecutionAuthorizationEnvelope.goal_id == row.goal_id,
            ExecutionAuthorizationEnvelope.status == "active",
        ).with_for_update()
    ).scalar_one_or_none()

    envelope = ExecutionAuthorizationEnvelope(
        owner_id=owner_id, goal_id=row.goal_id, source_proposal_id=row.id,
        repository_identity=row.repository_identity, authorized_paths=authorized_paths,
        authorized_capabilities=authorized_capabilities, authorized_risk=authorized_risk,
        authorized_by=authorized_by, idempotency_key=envelope_idempotency_key,
        supersedes_envelope_id=prior_envelope.id if prior_envelope is not None else None,
        provenance={"authorized_from_proposal_id": str(row.id)},
    )
    db.add(envelope)
    db.flush()

    if prior_envelope is not None:
        prior_envelope.status = "superseded"
        db.flush()

    row.status = "authorized"
    row.authorized_envelope_id = envelope.id
    row.updated_at = datetime.utcnow()
    db.flush()
    return row, envelope


def get_execution_scope_proposal(db: Session, *, owner_id: uuid.UUID, proposal_id: uuid.UUID) -> ExecutionScopeProposal | None:
    return db.execute(select(ExecutionScopeProposal).where(ExecutionScopeProposal.id == proposal_id, ExecutionScopeProposal.owner_id == owner_id)).scalar_one_or_none()


def list_execution_scope_proposals(db: Session, *, owner_id: uuid.UUID, status: str | None = None, goal_id: uuid.UUID | None = None) -> list[ExecutionScopeProposal]:
    stmt = select(ExecutionScopeProposal).where(ExecutionScopeProposal.owner_id == owner_id)
    if status is not None:
        stmt = stmt.where(ExecutionScopeProposal.status == status)
    if goal_id is not None:
        stmt = stmt.where(ExecutionScopeProposal.goal_id == goal_id)
    return list(db.execute(stmt.order_by(ExecutionScopeProposal.observed_at)).scalars().all())


def list_unreviewed_execution_scope_proposals(db: Session, *, owner_id: uuid.UUID) -> list[ExecutionScopeProposal]:
    return list_execution_scope_proposals(db, owner_id=owner_id, status="unreviewed")


def get_execution_authorization_envelope(db: Session, *, owner_id: uuid.UUID, envelope_id: uuid.UUID) -> ExecutionAuthorizationEnvelope | None:
    return db.execute(select(ExecutionAuthorizationEnvelope).where(ExecutionAuthorizationEnvelope.id == envelope_id, ExecutionAuthorizationEnvelope.owner_id == owner_id)).scalar_one_or_none()


def get_current_execution_envelope(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> ExecutionAuthorizationEnvelope | None:
    """The safe-by-default "what is this goal currently authorized to touch, if anything"
    query -- the same role `list_current_project_entities()`/`list_current_diagnoses()` play
    for their own sibling foundations. Returns None (not the most recent row regardless of
    status) if the goal has never been authorized, or if its only envelope was superseded
    without a replacement -- callers must treat "no current envelope" as "not eligible for
    Supervisor execution", never fall back to a stale one."""

    return db.execute(
        select(ExecutionAuthorizationEnvelope).where(
            ExecutionAuthorizationEnvelope.owner_id == owner_id,
            ExecutionAuthorizationEnvelope.goal_id == goal_id,
            ExecutionAuthorizationEnvelope.status == "active",
        )
    ).scalar_one_or_none()


def goal_has_ever_been_envelope_governed(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> bool:
    """The durable EVER_GOVERNED fact: whether ANY `ExecutionAuthorizationEnvelope` row has
    ever existed for this goal, regardless of its current status (active/superseded/revoked).
    `execution_authorization_envelopes` rows are never deleted, only superseded (see this
    module's own module docstring / migration 0057), so mere row existence is itself the
    proof -- no separate governance-state column needed.

    Once true for a goal, it is true forever: authority must never fall back to an
    envelope-blind execution path just because the CURRENT envelope is absent, superseded, or
    revoked (see `get_current_execution_envelope()`'s own docstring for the companion "no
    current envelope = not eligible" half of this invariant). `app.worker.py`'s
    `_advance_mainai_execution_tasks()` established this exact predicate first (PR #154,
    found by Cursor's #152); `app.mainai_execution.recovery_takeover.execute_takeover()` is
    the second independent caller -- see that module for why the dead-job-takeover path needs
    the identical check."""
    return (
        db.execute(
            select(ExecutionAuthorizationEnvelope.id).where(
                ExecutionAuthorizationEnvelope.owner_id == owner_id,
                ExecutionAuthorizationEnvelope.goal_id == goal_id,
            )
        ).first()
        is not None
    )


def list_execution_authorization_envelopes(db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID) -> list[ExecutionAuthorizationEnvelope]:
    """Full history for a goal, oldest first -- including superseded envelopes, for audit."""

    return list(
        db.execute(
            select(ExecutionAuthorizationEnvelope)
            .where(ExecutionAuthorizationEnvelope.owner_id == owner_id, ExecutionAuthorizationEnvelope.goal_id == goal_id)
            .order_by(ExecutionAuthorizationEnvelope.authorized_at)
        ).scalars().all()
    )
