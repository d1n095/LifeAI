"""Shared helpers for the Provider Spend Database Authority examination.

Target: Codex candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298` (latest examined
lineage). The subject is the PostgreSQL authorization boundary for provider spending:

  * provider_spend_authorizations — the Founder ceiling + reserved_*/spent_* counters,
    guarded by the BEFORE UPDATE/DELETE trigger `security_kernel_spend_guard`
    (migration 0095). The runtime role keeps a column-scoped UPDATE grant on
    (status, spent_*, reserved_*) only; DELETE is revoked.
  * provider_spend_usage_events — append-only ledger (migration 0060); runtime has
    INSERT + SELECT only (UPDATE/DELETE revoked); settle/release mutate it solely through
    SECURITY DEFINER functions under an internal GUC.
  * settle_provider_spend_usage / release_provider_spend_usage — SECURITY DEFINER, the only
    audited path that may lower a reservation hold.

Setup is done through the schema-owner session (superuser_db), exactly as the product's own
fixtures do, so every probe exercises the REAL guard, functions, grants and RLS on the exact
SHA. Read-only examiner; no paid calls; loopback only.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import text

SPEND_TABLE = "provider_spend_authorizations"
USAGE_TABLE = "provider_spend_usage_events"
AUDIT_TABLE = "security_kernel_authority_events"


def set_runtime_owner(db, owner_id: uuid.UUID) -> None:
    """Set the RLS owner GUC on a runtime (mainai_app) session — client-supplied context."""
    from app.request_context import current_user_id

    current_user_id.set(str(owner_id))
    db.execute(text("SET LOCAL app.current_user_id=:owner"), {"owner": str(owner_id)})


def make_goal_and_envelope(superuser_db, owner_id: uuid.UUID):
    """goal -> proposal -> authorized active execution envelope (the spend grant binds to it)."""
    from app.execution_envelopes.service import (
        authorize_execution_scope,
        propose_execution_scope,
    )
    from app.mainai_execution.planner import create_goal

    goal = create_goal(
        superuser_db,
        owner_id=owner_id,
        title="Provider spend authority examiner probe",
        original_instruction="Exercise only the bounded spend effect.",
        created_by="founder",
    )
    proposal = propose_execution_scope(
        superuser_db,
        owner_id=owner_id,
        goal_id=goal.id,
        idempotency_key=f"spend-exam-proposal:{uuid.uuid4()}",
    )
    _, envelope = authorize_execution_scope(
        superuser_db,
        owner_id=owner_id,
        proposal_id=proposal.id,
        authorized_by="founder",
        authorized_paths=["backend/app/"],
        authorized_capabilities=["repo_edit"],
        authorized_risk="low",
        envelope_idempotency_key=f"spend-exam-envelope:{uuid.uuid4()}",
    )
    superuser_db.flush()
    return goal, envelope


def authorize_spend(
    superuser_db,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    envelope_id: uuid.UUID,
    *,
    max_cost_usd: str = "10.00",
    max_requests: int = 5,
    per_request: str = "1.00",
    providers=("openai",),
    models=("gpt-4o",),
):
    """Create the ONLY kind of active grant via the real production authorize path."""
    from app.provider_spend.service import authorize_provider_spend

    auth = authorize_provider_spend(
        superuser_db,
        owner_id=owner_id,
        goal_id=goal_id,
        execution_envelope_id=envelope_id,
        authorized_by="founder",
        max_cost_usd=Decimal(max_cost_usd),
        max_requests=max_requests,
        max_cost_per_request_usd=Decimal(per_request),
        allowed_providers=list(providers),
        allowed_models=list(models),
        idempotency_key=f"spend-grant:{uuid.uuid4()}",
    )
    superuser_db.flush()
    return auth


def reserve(db, *, owner_id, goal_id, source_ref, provider="openai", model="gpt-4o"):
    """Deterministic $per_request hold (0 token holds -> cost taken from max_cost_per_request)."""
    from app.provider_spend.service import reserve_provider_spend_call

    return reserve_provider_spend_call(
        db,
        owner_id=owner_id,
        goal_id=goal_id,
        source_ref=source_ref,
        provider=provider,
        model=model,
        hold_prompt_tokens=0,
        hold_completion_tokens=0,
    )


def init_owner_epoch(superuser_db, owner_id: uuid.UUID) -> tuple[int, int]:
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
        text("SELECT scope_key,epoch FROM workforce_authority_epoch "
             "WHERE scope_key IN ('GLOBAL',:scope)"),
        {"scope": str(owner_id)},
    ).all()
    e = {r.scope_key: int(r.epoch) for r in rows}
    return e["GLOBAL"], e[str(owner_id)]


def owner_stop(superuser_db, owner_id: uuid.UUID, reason: str = "examiner STOP") -> None:
    """A real owner STOP: advances the owner epoch (the AFTER-UPDATE cascade revokes the
    owner's active envelopes AND provider-spend authorizations)."""
    init_owner_epoch(superuser_db, owner_id)
    superuser_db.execute(
        text(
            "UPDATE workforce_authority_epoch SET stopped=true, epoch=epoch+1, "
            "reason=:r, activated_at=clock_timestamp() WHERE scope_key=:scope"
        ),
        {"r": reason, "scope": str(owner_id)},
    )
    superuser_db.flush()


def auth_row(superuser_db, auth_id):
    return superuser_db.execute(
        text(f"SELECT status, max_cost_usd, max_requests, spent_cost_usd, spent_requests, "
             f"reserved_cost_usd, reserved_requests FROM {SPEND_TABLE} WHERE id=:id"),
        {"id": auth_id},
    ).mappings().one()
