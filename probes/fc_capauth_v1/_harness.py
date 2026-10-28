"""Shared helpers for the Founder Capability & Authority-Change examination (migration 0094).

Target: Codex candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`. Subject: the DB
authority for the founder capability/authority-change core:

  * founder_authority_change_proposals — proposals; one terminal transition proposed->{applied,
    rejected,cancelled}; owner CHECK = FOUNDER.
  * founder_step_up_receipts — step-up receipts; one-way consumption.
  * founder_capability_grants — capability grants; one terminal transition active->{consumed,
    cancelled,expired}; UNIQUE(owner, idempotency_key).
  * founder_capability_policies — versioned policies; authority fields immutable; at most one
    ACTIVE policy per (owner, capability_key) (partial unique index).
  * founder_authority_audit_receipts — immutable, hash-chained audit.

Every table CHECKs owner_id = FOUNDER and FORCE owner-scoped RLS; the immutable guard fires on
UPDATE. There is no INSERT-time trigger, so the authority rows are built with privileged raw
INSERTs (owner = FOUNDER) in dependency order. Read-only examiner; loopback only; no paid calls.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import text

PROPOSALS = "founder_authority_change_proposals"
STEP_UPS = "founder_step_up_receipts"
GRANTS = "founder_capability_grants"
POLICIES = "founder_capability_policies"
AUDIT = "founder_authority_audit_receipts"


def _d() -> str:
    return uuid.uuid4().hex  # 32-char unique digest/signature (<=64)


def ensure_founder():
    from app.db import SecurityKernelSessionLocal
    from app.founder import FOUNDER_USER_ID
    from app.models.user import User, UserRole
    from app.security import hash_password

    now = datetime.utcnow()
    with SecurityKernelSessionLocal() as adb:
        u = adb.get(User, FOUNDER_USER_ID)
        if u is None:
            adb.add(User(id=FOUNDER_USER_ID, email="founder@lifeos.local",
                         password_hash=hash_password("CorrectHorseBattery9!"), role=UserRole.founder,
                         is_active=True, email_verified=True, email_verified_at=now,
                         sessions_valid_after=now - timedelta(seconds=2)))
        else:
            u.role = UserRole.founder
            u.is_active = True
        adb.commit()
    return FOUNDER_USER_ID


def set_owner(db, owner_id):
    db.execute(text("SET LOCAL app.current_user_id = :o"), {"o": str(owner_id)})


def new_proposal(sdb, owner, *, status="proposed", change_kind="policy"):
    pid = uuid.uuid4()
    sdb.execute(text(
        f"INSERT INTO {PROPOSALS} (id, owner_id, change_kind, requested_changes, proposal_digest, "
        "proposed_by, status) VALUES (:id, :o, :k, '[{\"c\": 1}]'::jsonb, :dg, 'founder', :st)"),
        {"id": pid, "o": owner, "k": change_kind, "dg": _d(), "st": status})
    sdb.commit()
    return pid


def new_step_up(sdb, owner, *, consumed=False):
    sid = uuid.uuid4()
    sdb.execute(text(
        f"INSERT INTO {STEP_UPS} (id, owner_id, access_jti, purpose, receipt_digest, issuer_signature, "
        "expires_at, consumed_at) VALUES (:id, :o, :jti, 'apply_authority_change', :dg, :sig, "
        "now()+interval '5 min', :c)"),
        {"id": sid, "o": owner, "jti": _d()[:16], "dg": _d(), "sig": _d(),
         "c": datetime.utcnow() if consumed else None})
    sdb.commit()
    return sid


def new_grant(sdb, owner, proposal_id, step_up_id, *, idempotency_key=None, status="active"):
    gid = uuid.uuid4()
    sdb.execute(text(
        f"INSERT INTO {GRANTS} (id, owner_id, proposal_id, step_up_receipt_id, proposal_digest, "
        "status, issuer_signature, idempotency_key, expires_at) VALUES (:id, :o, :p, :s, :dg, :st, "
        ":sig, :idem, now()+interval '5 min')"),
        {"id": gid, "o": owner, "p": proposal_id, "s": step_up_id, "dg": _d(), "st": status,
         "sig": _d(), "idem": idempotency_key or _d()})
    sdb.commit()
    return gid


def new_policy(sdb, owner, grant_id, *, capability_key, version, decision="allow", status="active"):
    pid = uuid.uuid4()
    sdb.execute(text(
        f"INSERT INTO {POLICIES} (id, owner_id, capability_key, decision, version, status, "
        "authorized_by, grant_id, policy_digest, issuer_signature) VALUES (:id, :o, :ck, :dec, :v, "
        ":st, 'founder', :g, :dg, :sig)"),
        {"id": pid, "o": owner, "ck": capability_key, "dec": decision, "v": version, "st": status,
         "g": grant_id, "dg": _d(), "sig": _d()})
    sdb.commit()
    return pid


def new_audit(sdb, owner, *, previous_digest=None):
    aid = uuid.uuid4()
    dg = _d()
    sdb.execute(text(
        f"INSERT INTO {AUDIT} (id, owner_id, event_type, subject_kind, subject_id, actor, "
        "previous_digest, receipt_digest, issuer_signature) VALUES (:id, :o, 'grant_issued', "
        "'capability_grant', :sid, 'founder', :prev, :dg, :sig)"),
        {"id": aid, "o": owner, "sid": str(uuid.uuid4()), "prev": previous_digest, "dg": dg, "sig": _d()})
    sdb.commit()
    return aid, dg


def full_chain(sdb, owner, *, capability_key=None, version=1):
    """proposal -> step_up -> grant -> policy. Returns a dict of ids."""
    p = new_proposal(sdb, owner)
    s = new_step_up(sdb, owner)
    g = new_grant(sdb, owner, p, s)
    pol = new_policy(sdb, owner, g, capability_key=capability_key or f"cap-{uuid.uuid4().hex[:8]}",
                     version=version)
    return {"proposal": p, "step_up": s, "grant": g, "policy": pol}


def status_of(sdb, table, row_id, col="status"):
    return sdb.execute(text(f"SELECT {col} FROM {table} WHERE id=:i"), {"i": row_id}).scalar_one()
