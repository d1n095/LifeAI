"""Shared helpers for the Effect-Specific Database Authority examination (migration 0099).

Target: #314+Codex candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`
(frozen parent `078312d7`, backend byte-identical to fully-tested `e8d6fc8`).

The subject is migration 0099's `security_kernel_revoke_execution_envelope(...)` operation,
the REVOKE of all envelope UPDATE from the runtime role, and the route switch to the
privileged `get_security_kernel_db` session. These helpers mint an owner, an active
execution-authorization envelope, and the GLOBAL/owner authority epochs the operation binds
to, through the schema-owner (`superuser_db`) session exactly as the builder's own proofs do,
so the examiner tests all exercise the REAL production function and triggers — never a stub.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text

# The exact SQL identity of the privileged operation under examination (migration 0099) and
# of the previously-certified FA-PRIV/STOP operation (migration 0098), used for privilege
# assertions via has_function_privilege.
REVOKE_FN = "security_kernel_revoke_execution_envelope(uuid,uuid,bigint,bigint,text)"
STOP_FN = "security_kernel_revoke_authority_for_stop(uuid,text)"
ENVELOPE_TABLE = "execution_authorization_envelopes"
AUDIT_TABLE = "security_kernel_authority_events"
REVOKE_EVENT = "execution_envelope_revoked"


def set_runtime_owner(db, owner_id: uuid.UUID) -> None:
    """Set the RLS owner GUC on a runtime (mainai_app) session — client-supplied context.

    Mirrors how a request establishes its owner: the contextvar the `after_begin` listener
    reads, plus an explicit SET LOCAL. A forged value here is only client input; migration
    0099 no longer trusts it for revocation authority.
    """
    from app.request_context import current_user_id

    current_user_id.set(str(owner_id))
    db.execute(text("SET LOCAL app.current_user_id=:owner"), {"owner": str(owner_id)})


def make_active_envelope(superuser_db, owner_id: uuid.UUID):
    """Create a goal -> proposal -> authorized active envelope via the schema-owner session."""
    from app.execution_envelopes.service import (
        authorize_execution_scope,
        propose_execution_scope,
    )
    from app.mainai_execution.planner import create_goal

    goal = create_goal(
        superuser_db,
        owner_id=owner_id,
        title="Effect-specific authority examiner probe",
        original_instruction="Exercise only the bounded revocation effect.",
        created_by="founder",
    )
    proposal = propose_execution_scope(
        superuser_db,
        owner_id=owner_id,
        goal_id=goal.id,
        idempotency_key=f"eff-exam-proposal:{uuid.uuid4()}",
    )
    _, envelope = authorize_execution_scope(
        superuser_db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["backend/app/"],
        authorized_capabilities=["repo_edit"],
        authorized_risk="low",
        envelope_idempotency_key=f"eff-exam-envelope:{uuid.uuid4()}",
    )
    superuser_db.flush()
    return envelope


def init_owner_epoch(superuser_db, owner_id: uuid.UUID) -> tuple[int, int]:
    """Ensure GLOBAL and owner authority-epoch rows exist; return (global_epoch, owner_epoch)."""
    superuser_db.execute(
        text(
            "INSERT INTO workforce_authority_epoch(scope_key,owner_id,epoch,stopped) "
            "VALUES ('GLOBAL',NULL,0,false) ON CONFLICT(scope_key) DO NOTHING"
        )
    )
    superuser_db.execute(
        text(
            "INSERT INTO workforce_authority_epoch(scope_key,owner_id,epoch,stopped) "
            "VALUES (:scope,:owner,0,false) ON CONFLICT(scope_key) DO NOTHING"
        ),
        {"scope": str(owner_id), "owner": owner_id},
    )
    rows = superuser_db.execute(
        text(
            "SELECT scope_key,epoch FROM workforce_authority_epoch "
            "WHERE scope_key IN ('GLOBAL',:scope)"
        ),
        {"scope": str(owner_id)},
    ).all()
    epochs = {row.scope_key: int(row.epoch) for row in rows}
    return epochs["GLOBAL"], epochs[str(owner_id)]


def revoke_fn(db, owner_id, envelope_id, global_epoch, owner_epoch, reason="Founder revoke"):
    """Invoke the real migration-0099 privileged operation and return its scalar result."""
    return db.execute(
        text(
            "SELECT security_kernel_revoke_execution_envelope("
            ":owner,:envelope,:global_epoch,:owner_epoch,:reason)"
        ),
        {
            "owner": owner_id,
            "envelope": envelope_id,
            "global_epoch": global_epoch,
            "owner_epoch": owner_epoch,
            "reason": reason,
        },
    ).scalar_one()


def envelope_status(superuser_db, envelope_id) -> str:
    return superuser_db.execute(
        text(f"SELECT status FROM {ENVELOPE_TABLE} WHERE id=:id"),
        {"id": envelope_id},
    ).scalar_one()


def revoke_audit_count(superuser_db, envelope_id) -> int:
    return superuser_db.execute(
        text(
            f"SELECT count(*) FROM {AUDIT_TABLE} "
            "WHERE event_type=:ev AND subject_id=:id"
        ),
        {"ev": REVOKE_EVENT, "id": str(envelope_id)},
    ).scalar_one()
