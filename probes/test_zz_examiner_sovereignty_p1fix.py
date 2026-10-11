"""Examiner-only probes for sovereignty P1 fix 80ecfc7 (disposable, never committed). Prints HOLDS/GAP."""
import threading
import uuid

from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from app.db import SessionLocal, migration_engine
from app.founder import FOUNDER_USER_ID
from app.mainai_founder_sovereignty import service as svc
from app.mainai_founder_sovereignty.types import ActorKind, ApprovalMode, FamilyRelationship, StepUpPurpose
from tests.backend.mainai.test_founder_sovereignty import _bind_session, _ensure_founder, _family_user
from tests.backend.test_account_erasure import _narrow_privileges_before_this_module  # noqa: F401

F = str(FOUNDER_USER_ID)


def report(name, holds, detail=""):
    print(f"\n{'HOLDS' if holds else 'GAP  '} {name} {detail}")


def _seed(su):
    """As the authenticated Founder (superuser session, founder context) create a locked policy + a family grant."""
    _ensure_founder(su)
    _bind_session(su, FOUNDER_USER_ID)
    key = f"speech_{uuid.uuid4().hex[:6]}"
    svc.apply_founder_policy(su, actor_kind=ActorKind.FOUNDER, policy_key=key, payload={"style": "GOOD"}, reason="v1")
    svc.set_workflow_lock(su, policy_key=key, locked=True)
    kid = _family_user(su, "kid")
    svc.add_family_member(su, principal_id=kid.id, relationship=FamilyRelationship.CHILD, display_name="kid")
    su.commit()
    return key, kid.id


def _app(db, owner, guc=False):
    db.execute(sa_text("SELECT set_config('app.current_user_id', :o, true)"), {"o": str(owner)})
    if guc:
        db.execute(sa_text("SELECT set_config('app.founder_sovereignty_erasure', 'on', true)"))


def _try(db, sql, params=None):
    try:
        r = db.execute(sa_text(sql), params or {})
        db.flush()
        return True, getattr(r, "rowcount", None)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return False, str(exc).splitlines()[0][:100]


def test_runtime_attacks(db_session, superuser_db):
    key, kid = _seed(superuser_db)
    assert db_session.execute(sa_text("select current_user")).scalar_one() == "mainai_app"
    head = lambda: superuser_db.execute(sa_text("SELECT workflow_locked FROM founder_policy_heads WHERE policy_key=:k"), {"k": key}).scalar_one()

    # 1. self-unlock without step-up (no GUC)
    _app(db_session, F)
    ok, info = _try(db_session, "UPDATE founder_policy_heads SET workflow_locked=false WHERE policy_key=:k", {"k": key})
    db_session.rollback()
    report("runtime (Founder ctx) cannot unlock without step-up", not ok, f"-> {info}")

    # 2. runtime mints its own step-up receipt, then unlocks
    _app(db_session, F)
    ok1, i1 = _try(db_session, "INSERT INTO founder_step_up_receipts (owner_id, purpose, session_jti, verified_at, expires_at) VALUES (:o,'workflow_unlock','forged',now(),now()+interval '5 min')", {"o": F})
    ok2, i2 = (False, "skipped")
    if ok1:
        ok2, i2 = _try(db_session, "UPDATE founder_policy_heads SET workflow_locked=false WHERE policy_key=:k", {"k": key})
    db_session.rollback()
    report("runtime cannot mint its own step-up receipt and self-unlock", not (ok1 and ok2), f"-> mint={ok1} ({i1}) unlock={ok2} ({i2})")

    # 3. self-settable erasure GUC bypasses guards
    cases = [
        ("unlock policy head (no step-up)", "UPDATE founder_policy_heads SET workflow_locked=false WHERE policy_key=:k", {"k": key}),
        ("insert kernel_security_invariant version for founder_identity",
         "INSERT INTO founder_policy_versions (owner_id,policy_key,policy_class,version_number,payload,reason,actor_kind) VALUES (:o,'founder_identity','kernel_security_invariant',1,'{\"founder\":\"ai\"}','x','founder')", {"o": F}),
        ("forge non-founder actor version", "INSERT INTO founder_policy_versions (owner_id,policy_key,policy_class,version_number,payload,reason,actor_kind) VALUES (:o,:k,'founder_policy',77,'{}','x','founder')", {"o": F, "k": key}),
        ("mint step-up for founder without founder checks", "INSERT INTO founder_step_up_receipts (owner_id, purpose, verified_at, expires_at) VALUES (:o,'policy_rollback',now(),now()+interval '1 hour')", {"o": F}),
        ("add Founder as family member", "INSERT INTO family_members (owner_id, principal_id, relationship, display_name) VALUES (:o, :o, 'other', 'x')", {"o": F}),
    ]
    for name, sql, params in cases:
        _app(db_session, F, guc=False)
        base_ok, _ = _try(db_session, sql, params)
        db_session.rollback()
        _app(db_session, F, guc=True)
        ok, info = _try(db_session, sql, params)
        db_session.rollback()
        report(f"self-set app.founder_sovereignty_erasure=on cannot bypass guard: {name}", not ok, f"-> without GUC allowed={base_ok}; WITH GUC allowed={ok} {'' if ok else info}")

    # 4. governed sovereignty wipe callable outside erasure
    _app(db_session, F)
    ok, info = _try(db_session, "SELECT erase_own_founder_sovereignty_children()")
    left = superuser_db.execute(sa_text("SELECT count(*) FROM founder_policy_versions WHERE owner_id=:o"), {"o": F}).scalar_one()
    db_session.rollback()
    report("runtime cannot wipe all Founder policy history/bindings/grants outside a governed erasure op", not ok, f"-> call ok={ok}; versions visible after call (pre-rollback, other session)={left}")

    # 5. non-founder squat
    _app(db_session, kid)
    ok, info = _try(db_session, "INSERT INTO founder_instance_bindings (owner_id, founder_user_id) VALUES (:k, :f)", {"k": str(kid), "f": F})
    db_session.rollback()
    report("non-Founder session cannot squat the Founder binding", not ok, f"-> {info}")
    _app(db_session, kid, guc=True)
    ok, info = _try(db_session, "INSERT INTO founder_instance_bindings (owner_id, founder_user_id) VALUES (:k, :f)", {"k": str(kid), "f": F})
    db_session.rollback()
    report("non-Founder session + self-set erasure GUC cannot squat the Founder binding", not ok, f"-> {info}")


def test_step_up_is_not_reauthentication(superuser_db):
    _ensure_founder(superuser_db)
    _bind_session(superuser_db, FOUNDER_USER_ID)
    r = svc.issue_founder_step_up(superuser_db, purpose=StepUpPurpose.WORKFLOW_UNLOCK if hasattr(StepUpPurpose, "WORKFLOW_UNLOCK") else list(StepUpPurpose)[0])
    report("step-up issuance requires fresh re-authentication (password/second factor)", False,
           f"(issued with NO credential; session_jti={r.session_jti!r}; only check = app.current_user_id is Founder)")


def test_approval_swap_with_guc(db_session, superuser_db):
    _ensure_founder(superuser_db)
    _bind_session(superuser_db, FOUNDER_USER_ID)
    kid = _family_user(superuser_db, "swapkid")
    svc.add_family_member(superuser_db, principal_id=kid.id, relationship=FamilyRelationship.CHILD, display_name="k")
    req = svc.request_family_capability(superuser_db, principal_id=kid.id, capability_key="lights.control")
    superuser_db.commit()
    shown = req.snapshot_hash
    from app.mainai_founder_sovereignty.identity import canonical_approval_snapshot_hash
    new_hash = canonical_approval_snapshot_hash(principal_id=kid.id, resource="purchases", action="create", scope=req.scope,
                                                requested_data=req.requested_data, requested_limits=req.requested_limits,
                                                requested_duration=req.requested_duration, risk_tier="high", consequences=req.consequences,
                                                session_id=req.session_id, device_id=req.device_id)
    _app(db_session, F, guc=True)
    ok, info = _try(db_session, "UPDATE family_approval_requests SET capability_key='purchases.create', resource='purchases', action='create', risk_tier='high', snapshot_hash=:h WHERE id=:r",
                    {"h": new_hash, "r": str(req.id)})
    if ok:
        db_session.commit()
    superuser_db.expire_all()
    _bind_session(superuser_db, FOUNDER_USER_ID)
    try:
        svc.decide_approval(superuser_db, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
        superuser_db.commit()
        decided = True
    except Exception as exc:  # noqa: BLE001
        superuser_db.rollback()
        decided = f"refused: {str(exc)[:60]}"
    report("approved action cannot be swapped after Founder views it (GUC + recomputed hash, decide without expected hash)", not (ok and decided is True),
           f"-> swap={ok} ({'' if ok else info}); decide={decided}")
    if ok:
        _bind_session(superuser_db, FOUNDER_USER_ID)
        try:
            svc.decide_approval(superuser_db, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW, expected_snapshot_hash=shown)
            r2 = "accepted"
        except Exception as exc:  # noqa: BLE001
            superuser_db.rollback()
            r2 = f"refused ({str(exc)[:50]})"
        report("[control] decide WITH the displayed snapshot hash refuses the swapped request", r2.startswith("refused"), f"-> {r2}")


def test_allow_once_race(superuser_db):
    _ensure_founder(superuser_db)
    _bind_session(superuser_db, FOUNDER_USER_ID)
    kid = _family_user(superuser_db, "racer")
    kid_id = kid.id
    svc.add_family_member(superuser_db, principal_id=kid_id, relationship=FamilyRelationship.CHILD, display_name="k")
    req = svc.request_family_capability(superuser_db, principal_id=kid_id, capability_key="family_calendar.create_event")
    _bind_session(superuser_db, FOUNDER_USER_ID)
    svc.decide_approval(superuser_db, request_id=req.id, mode=ApprovalMode.ALLOW_ONCE)
    superuser_db.commit()
    S = sessionmaker(bind=migration_engine)
    for trial in range(5):
        barrier, res = threading.Barrier(2), []

        def use():
            s = S()
            try:
                _bind_session(s, kid_id)
                barrier.wait()
                v = svc.authorize_capability(s, capability_key="family_calendar.create_event").verdict.value
                s.commit()
                res.append(v)
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                res.append(f"EXC:{type(exc).__name__}")
            finally:
                s.close()
        ts = [threading.Thread(target=use) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        report(f"ALLOW_ONCE race trial {trial}: at most one allowed, loser gets a clean denial", res.count("allowed") <= 1 and not any(r.startswith("EXC") for r in res), f"-> {res}")
        if trial < 4:
            superuser_db.execute(sa_text("SELECT set_config('app.founder_sovereignty_erasure','on',true)"))
            superuser_db.execute(sa_text("UPDATE family_capability_grants SET remaining_uses=1 WHERE principal_id=:p"), {"p": str(kid_id)})
            superuser_db.commit()


def test_founder_account_erasure():
    from app.models.user import User, UserRole
    from app.security import hash_password
    from tests.backend.test_account_erasure import _set_rls_user, erase_account_data  # noqa: F401
    s = SessionLocal()
    try:
        u = s.get(User, FOUNDER_USER_ID)
        if u is None:
            u = User(id=FOUNDER_USER_ID, email="f-er@lifeos.local", password_hash=hash_password("Sup3rS3cret!"), role=UserRole.founder, email_verified=True)
            s.add(u)
        else:
            u.password_hash = hash_password("Sup3rS3cret!")
        s.commit()
        _set_rls_user(s, FOUNDER_USER_ID)
        svc.apply_founder_policy(s, actor_kind=ActorKind.FOUNDER, policy_key="speech", payload={"x": 1}, reason="r")
        kid = _family_user(s, "erkid")
        svc.add_family_member(s, principal_id=kid.id, relationship=FamilyRelationship.CHILD, display_name="k")
        req = svc.request_family_capability(s, principal_id=kid.id, capability_key="lights.control")
        svc.decide_approval(s, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
        s.commit()
        _set_rls_user(s, FOUNDER_USER_ID)
        try:
            erase_account_data(s, u)
            ok, err = s.get(User, FOUNDER_USER_ID) is None, ""
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
        report("Founder account erasure succeeds with policy + family approval state (prior P1)", ok, err)
    finally:
        s.rollback()
        s.close()


def _swap_case(db_session, superuser_db, *, cap_from, cap_to, res_to, act_to, risk_to, mint_step_up):
    _ensure_founder(superuser_db)
    _bind_session(superuser_db, FOUNDER_USER_ID)
    kid = _family_user(superuser_db, "swap2")
    svc.add_family_member(superuser_db, principal_id=kid.id, relationship=FamilyRelationship.CHILD, display_name="k")
    req = svc.request_family_capability(superuser_db, principal_id=kid.id, capability_key=cap_from)
    superuser_db.commit()
    from app.mainai_founder_sovereignty.identity import canonical_approval_snapshot_hash
    h = canonical_approval_snapshot_hash(principal_id=kid.id, resource=res_to, action=act_to, scope=req.scope, requested_data=req.requested_data,
                                         requested_limits=req.requested_limits, requested_duration=req.requested_duration, risk_tier=risk_to,
                                         consequences=req.consequences, session_id=req.session_id, device_id=req.device_id)
    _app(db_session, F, guc=True)
    ok, info = _try(db_session, "UPDATE family_approval_requests SET capability_key=:c, resource=:r, action=:a, risk_tier=:t, snapshot_hash=:h WHERE id=:id",
                    {"c": cap_to, "r": res_to, "a": act_to, "t": risk_to, "h": h, "id": str(req.id)})
    if mint_step_up:
        _app(db_session, F)
        _try(db_session, "INSERT INTO founder_step_up_receipts (owner_id, purpose, session_jti, verified_at, expires_at) VALUES (:o,'permanent_high_risk_delegation','forged',now(),now()+interval '5 min')", {"o": F})
    db_session.commit()
    superuser_db.expire_all()
    _bind_session(superuser_db, FOUNDER_USER_ID)
    try:
        svc.decide_approval(superuser_db, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
        superuser_db.commit()
        _bind_session(superuser_db, kid.id)
        v = svc.authorize_capability(superuser_db, capability_key=cap_to).verdict.value
    except Exception as exc:  # noqa: BLE001
        superuser_db.rollback()
        v = f"refused: {str(exc)[:70]}"
    report(f"swap {cap_from} -> {cap_to} after Founder views (runtime GUC rewrite{' + self-minted step-up' if mint_step_up else ''}; decide without expected hash)",
           not (ok and v == "allowed"), f"-> rewrite={ok}; child authorized for {cap_to}: {v}")


def test_swap_low_risk(db_session, superuser_db):
    _swap_case(db_session, superuser_db, cap_from="lights.control", cap_to="tv.control", res_to="home_tv", act_to="control", risk_to="low", mint_step_up=False)


def test_swap_high_risk_with_minted_step_up(db_session, superuser_db):
    _swap_case(db_session, superuser_db, cap_from="lights.control", cap_to="purchases.create", res_to="purchases", act_to="create", risk_to="high", mint_step_up=True)
