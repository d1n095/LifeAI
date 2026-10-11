"""Examiner-only sovereignty probes (disposable, never committed). Prints HOLDS/GAP per property."""
import threading
import uuid
from datetime import timedelta

from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from app.db import migration_engine
from app.founder import FOUNDER_USER_ID
from app.mainai_founder_sovereignty.service import (
    add_family_member, apply_founder_policy, approval_context, authorize_capability, decide_approval,
    list_pending_approvals, request_family_capability, revoke_grant, rollback_founder_policy, set_workflow_lock,
)
from app.mainai_founder_sovereignty.types import ActorKind, ApprovalMode, AuthorizationVerdict, FamilyRelationship
from app.models.founder_sovereignty import FamilyCapabilityGrant, FounderPolicyHead, FounderPolicyVersion
from tests.backend.mainai.test_founder_sovereignty import _ensure_founder, _family_user

A = AuthorizationVerdict.ALLOWED


def report(name, holds, detail=""):
    print(f"\n{'HOLDS' if holds else 'GAP  '} {name} {detail}")


def _child(db, label="child"):
    _ensure_founder(db)
    kid = _family_user(db, label)
    add_family_member(db, actor_id=FOUNDER_USER_ID, principal_id=kid.id, relationship=FamilyRelationship.CHILD, display_name=label)
    db.flush()
    return kid


def test_approval_action_swap_after_founder_views(superuser_db):
    kid = _child(superuser_db)
    req = request_family_capability(superuser_db, principal_id=kid.id, capability_key="lights.control")
    shown = approval_context(req, who_label="child")
    superuser_db.execute(sa_text("UPDATE family_approval_requests SET capability_key='purchases.create', resource='purchases', action='create' WHERE id=:i"), {"i": str(req.id)})
    superuser_db.expire_all()
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
    r = authorize_capability(superuser_db, principal_id=kid.id, capability_key="purchases.create")
    report("approval_binds_to_action_founder_saw", r.verdict is not A, f"(Founder saw {shown.capability_key!r}; after swap purchases.create -> {r.verdict.value})")


def test_requested_data_and_limits_not_bound(superuser_db):
    import inspect
    sig = inspect.signature(authorize_capability)
    report("authorize_checks_resource_data_or_limits", any(p in sig.parameters for p in ("resource", "data", "limits", "amount", "requested_data")),
           f"(authorize_capability params = {list(sig.parameters)})")


def test_one_time_default_and_reuse(superuser_db):
    kid = _child(superuser_db)
    req = request_family_capability(superuser_db, principal_id=kid.id, capability_key="family_calendar.create_event")
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALLOW_ONCE)
    verdicts = [authorize_capability(superuser_db, principal_id=kid.id, capability_key="family_calendar.create_event").verdict for _ in range(3)]
    report("ALLOW_ONCE_not_reusable_when_caller_omits_consume", verdicts.count(A) <= 1, f"(consume defaults False; 3 checks -> {[v.value for v in verdicts]})")


def test_one_time_concurrent_double_spend(superuser_db):
    kid = _child(superuser_db, "racer")
    req = request_family_capability(superuser_db, principal_id=kid.id, capability_key="family_calendar.create_event")
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALLOW_ONCE)
    superuser_db.commit()
    S = sessionmaker(bind=migration_engine)
    barrier, results = threading.Barrier(2), []

    def use():
        s = S()
        try:
            barrier.wait()
            v = authorize_capability(s, principal_id=kid.id, capability_key="family_calendar.create_event", consume=True).verdict
            s.commit()
            results.append(v)
        except Exception as exc:  # noqa: BLE001
            results.append(f"EXC:{type(exc).__name__}:{str(exc).splitlines()[0][:80]}")
        finally:
            s.close()

    ts = [threading.Thread(target=use) for _ in range(2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    report("ALLOW_ONCE_single_use_under_concurrency", results.count(A) == 1, f"(two concurrent consumers -> {[getattr(v, 'value', v) for v in results]})")


def test_rollback_reaches_known_good(superuser_db):
    _ensure_founder(superuser_db)
    key = f"speech_style_{uuid.uuid4().hex[:6]}"
    for i, p in enumerate(({"style": "GOOD"}, {"style": "BAD-1"}, {"style": "BAD-2"})):
        apply_founder_policy(superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER, policy_key=key, payload=p, reason=f"v{i+1}")
    seq = []
    for _ in range(3):
        v = rollback_founder_policy(superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER, policy_key=key, reason="recover")
        seq.append(v.payload["style"])
    report("repeated_rollback_can_reach_known_good_v1", "GOOD" in seq, f"(successive rollbacks yield {seq}; no restore-to-version API)")


def test_internal_caller_self_unlock(superuser_db):
    _ensure_founder(superuser_db)
    key = f"lock_{uuid.uuid4().hex[:6]}"
    apply_founder_policy(superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER, policy_key=key, payload={"x": 1}, reason="r")
    set_workflow_lock(superuser_db, actor_id=FOUNDER_USER_ID, policy_key=key, locked=True)
    # an internal MainAI code path only needs the (public) Founder UUID — no authenticated human session required
    set_workflow_lock(superuser_db, actor_id=FOUNDER_USER_ID, policy_key=key, locked=False)
    head = superuser_db.get(FounderPolicyHead, {"owner_id": FOUNDER_USER_ID, "policy_key": key})
    report("service_requires_authenticated_founder_session_not_just_uuid", head.workflow_locked is True,
           "(set_workflow_lock(actor_id=FOUNDER_USER_ID) from non-HTTP code unlocked it)")


def _app_ctx(db, owner):
    db.execute(sa_text("SELECT set_config('app.current_user_id', :o, true)"), {"o": str(owner)})


def _try(db, sql, params=None):
    try:
        res = db.execute(sa_text(sql), params or {})
        db.flush()
        return True, getattr(res, "rowcount", None)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return False, str(exc).splitlines()[0][:110]


def test_runtime_raw_sql_as_mainai_app_in_founder_context(db_session, superuser_db):
    kid = _child(superuser_db, "rawsql")
    key = f"raw_{uuid.uuid4().hex[:6]}"
    v1 = apply_founder_policy(superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER, policy_key=key, payload={"a": 1}, reason="r")
    other = apply_founder_policy(superuser_db, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER, policy_key=key + "_other", payload={"evil": True}, reason="r")
    set_workflow_lock(superuser_db, actor_id=FOUNDER_USER_ID, policy_key=key, locked=True)
    req = request_family_capability(superuser_db, principal_id=kid.id, capability_key="lights.control")
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALLOW_ONCE)
    g = superuser_db.query(FamilyCapabilityGrant).filter_by(principal_id=kid.id).one()
    revoke_grant(superuser_db, actor_id=FOUNDER_USER_ID, grant_id=g.id)
    superuser_db.commit()
    assert db_session.execute(sa_text("select current_user")).scalar_one() == "mainai_app"
    cases = [
        ("self-unlock: UPDATE heads SET workflow_locked=false", False, "UPDATE founder_policy_heads SET workflow_locked=false WHERE policy_key=:k", {"k": key}),
        ("re-point head to another policy's version", False, "UPDATE founder_policy_heads SET current_version_id=:v WHERE policy_key=:k", {"v": str(other.id), "k": key}),
        ("forge founder-authored policy version (actor_kind='founder')", False,
         "INSERT INTO founder_policy_versions (owner_id,policy_key,policy_class,version_number,payload,reason,actor_kind) VALUES (:o,:k,'founder_policy',99,'{}','forged by AI','founder')", {"o": str(FOUNDER_USER_ID), "k": key}),
        ("insert kernel_security_invariant version for founder_identity", False,
         "INSERT INTO founder_policy_versions (owner_id,policy_key,policy_class,version_number,payload,reason,actor_kind) VALUES (:o,'founder_identity','kernel_security_invariant',1,'{\"founder\":\"ai\"}','x','founder')", {"o": str(FOUNDER_USER_ID)}),
        ("un-revoke grant + widen (revoked_at=NULL, uses=999, capability=purchases.create)", False,
         "UPDATE family_capability_grants SET revoked_at=NULL, remaining_uses=999, capability_key='purchases.create' WHERE id=:g", {"g": str(g.id)}),
        ("rewrite approved request after decision", False, "UPDATE family_approval_requests SET capability_key='economy.read', status='pending' WHERE id=:r", {"r": str(req.id)}),
        ("UPDATE old policy version reason", False, "UPDATE founder_policy_versions SET reason='rewritten' WHERE id=:v", {"v": str(v1.id)}),
        ("change previous-version pointer", False, "UPDATE founder_policy_versions SET previous_version_id=NULL WHERE id=:v", {"v": str(other.id)}),
        ("DELETE old policy version", False, "DELETE FROM founder_policy_versions WHERE id=:v", {"v": str(v1.id)}),
        ("UPDATE approval receipt decision", False, "UPDATE family_approval_receipts SET decision='always_allow'", None),
        ("DELETE approval receipt", False, "DELETE FROM family_approval_receipts", None),
    ]
    for name, should_allow, sql, params in cases:
        _app_ctx(db_session, FOUNDER_USER_ID)
        ok, info = _try(db_session, sql, params)
        changed = ok and (info is None or info > 0)
        report(f"runtime(mainai_app, founder ctx) cannot: {name}", not changed, f"-> {'ALLOWED rows=' + str(info) if ok else 'DENIED ' + str(info)}")
        db_session.rollback()


def test_cross_owner_and_family_context(db_session, superuser_db):
    kid = _child(superuser_db, "xowner")
    superuser_db.commit()
    for table in ("founder_policy_versions", "founder_policy_heads", "family_capability_grants", "family_approval_requests", "family_members"):
        _app_ctx(db_session, kid.id)
        n = db_session.execute(sa_text(f"SELECT count(*) FROM {table}")).scalar_one()
        report(f"family member's own DB context reads 0 Founder rows in {table}", n == 0, f"(count={n})")
        db_session.rollback()
    _app_ctx(db_session, kid.id)
    ok, info = _try(db_session, "INSERT INTO family_capability_grants (owner_id,principal_id,capability_key,resource,action,duration_mode,issuer_id) VALUES (:o,:p,'purchases.create','purchases','create','always_allow',:o)",
                    {"o": str(FOUNDER_USER_ID), "p": str(kid.id)})
    report("family context cannot insert grant into Founder scope", not ok, f"-> {info}")
    db_session.rollback()


def test_binding_squat_by_non_founder_owner(db_session, superuser_db):
    _ensure_founder(superuser_db)
    superuser_db.execute(sa_text("DELETE FROM founder_instance_bindings"))
    superuser_db.commit()
    other = _family_user(superuser_db, "squatter")
    superuser_db.commit()
    _app_ctx(db_session, other.id)
    ok, info = _try(db_session, "INSERT INTO founder_instance_bindings (owner_id, founder_user_id) VALUES (:o, :f)", {"o": str(other.id), "f": str(FOUNDER_USER_ID)})
    if ok:
        db_session.commit()
    report("non-Founder owner cannot create the singleton Founder-instance binding", not ok, f"-> {'ALLOWED' if ok else info}")
    if ok:
        from app.mainai_founder_sovereignty.service import bind_founder_instance
        try:
            bind_founder_instance(superuser_db, actor_id=FOUNDER_USER_ID)
            res = "founder bind ok"
        except Exception as exc:  # noqa: BLE001
            res = f"founder bind FAILED: {type(exc).__name__}"
        superuser_db.rollback()
        report("real Founder can still bind after squat", res == "founder bind ok", f"({res})")
        superuser_db.execute(sa_text("DELETE FROM founder_instance_bindings"))
        superuser_db.commit()
