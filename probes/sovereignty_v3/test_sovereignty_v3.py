"""Examiner-only adversarial suite: Founder Sovereignty P1-fix-2 (4ccf1ed). Runtime attacks run as `mainai_app`.
Each check prints `MATRIX|id|HOLDS/GAP|evidence`; tests never raise on GAP so the full matrix is produced.
Non-security SQL errors are reported as PROBE_ERROR, never as HOLDS."""
from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker

from app.db import SessionLocal, engine as app_engine
from app.founder import FOUNDER_USER_ID
from app.mainai_founder_sovereignty import service as svc
from app.mainai_founder_sovereignty.types import ActorKind, ApprovalMode, FamilyRelationship, StepUpPurpose
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.security import hash_password
from tests.backend.mainai.test_founder_sovereignty import FOUNDER_PASSWORD, _bind_founder_jti, _bind_session, _ensure_founder, _family_user
from tests.backend.test_account_erasure import _narrow_privileges_before_this_module  # noqa: F401  (startup narrowing)

F = str(FOUNDER_USER_ID)
DENY = ("InsufficientPrivilege", "permission denied", "RaiseException", "CheckViolation", "row-level security", "UniqueViolation",
        "ForeignKeyViolation", "SovereigntyError", "cannot", "requires", "must", "immutable", "forbidden")


def M(pid, holds, ev):
    print(f"\nMATRIX|{pid}|{'HOLDS' if holds else 'GAP'}|{ev}")


def ctx(db, uid, jti=None, **gucs):
    db.execute(sa_text("SELECT set_config('app.current_user_id', :u, true)"), {"u": str(uid)})
    if jti:
        db.execute(sa_text("SELECT set_config('app.current_access_jti', :j, true)"), {"j": jti})
    for k, v in gucs.items():
        db.execute(sa_text(f"SELECT set_config('app.{k}', :v, true)"), {"v": v})


def attempt(db, sql, params=None):
    try:
        with db.begin_nested():
            r = db.execute(sa_text(sql), params or {})
            out = r.fetchall() if r.returns_rows else r.rowcount
            db.flush()
        if not r.returns_rows and not out:
            return "PROBE_ERROR", "0 rows matched"
        return "ALLOWED", str(out)[:80]
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).splitlines()[0][:140]
        return ("DENIED" if any(d in msg for d in DENY) else "PROBE_ERROR"), msg


def runtime_session():
    s = SessionLocal()
    assert s.execute(sa_text("select current_user")).scalar_one() == "mainai_app"
    return s


def founder_jti(su):
    """Real Founder session state (refresh token row + sessions_valid_after) created by the privileged fixture."""
    user = _ensure_founder(su)
    jti = _bind_founder_jti(su, user)
    su.commit()
    return jti


@pytest.fixture
def locked(superuser_db):
    _ensure_founder(superuser_db)
    jti = founder_jti(superuser_db)
    _bind_session(superuser_db, FOUNDER_USER_ID, jti)
    key = f"k_{uuid.uuid4().hex[:6]}"
    svc.apply_founder_policy(superuser_db, actor_kind=ActorKind.FOUNDER, policy_key=key, payload={"v": 1}, reason="v1")
    svc.set_workflow_lock(superuser_db, policy_key=key, locked=True)
    superuser_db.commit()
    return key, jti


def head_locked(su, key):
    su.expire_all()
    return su.execute(sa_text("SELECT workflow_locked FROM founder_policy_heads WHERE policy_key=:k"), {"k": key}).scalar_one()


# ============================================================ environment parity with production boot order
def test_00_grant_parity(superuser_db):
    g = dict(superuser_db.execute(sa_text("select table_name, string_agg(privilege_type,',' order by privilege_type) from information_schema.role_table_grants "
                                          "where grantee='mainai_app' and table_name in ('founder_step_up_receipts','founder_step_up_reauth_receipts','account_erasure_operations',"
                                          "'account_erasure_reauth_receipts','founder_policy_heads','users','refresh_tokens') group by 1")).all())
    M("ENV", g.get("founder_step_up_receipts") == "SELECT" and g.get("account_erasure_operations") == "SELECT",
      f"runtime grants in test DB (must equal boot-order DB): {g}")


# ============================================================ P1-1 GUC bypass of deletion protections
GUCS = {"founder_sovereignty_erasure": "on", "founder_step_up_issue": "on", "account_erasure": "on", "erasure_in_progress": "on"}


def test_p1_1_guc_bypass(locked, superuser_db):
    key, jti = locked
    s = runtime_session()
    try:
        cases = [
            ("unlock head", "UPDATE founder_policy_heads SET workflow_locked=false WHERE policy_key=:k", {"k": key}),
            ("delete head", "DELETE FROM founder_policy_heads WHERE policy_key=:k", {"k": key}),
            ("delete versions", "DELETE FROM founder_policy_versions WHERE policy_key=:k", {"k": key}),
            ("rewrite version payload", "UPDATE founder_policy_versions SET payload=CAST(:p AS jsonb) WHERE policy_key=:k", {"k": key, "p": '{"v": 999}'}),
            ("delete binding", "DELETE FROM founder_instance_bindings", {}),
            ("governed wipe fn", "SELECT erase_own_founder_sovereignty_children()", {}),
        ]
        for name, sql, p in cases:
            ctx(s, F, jti, **GUCS)
            st, d = attempt(s, sql, p)
            s.rollback()
            M(f"1:{name}", st == "DENIED", f"runtime + all self-set GUCs + valid Founder jti -> {st} {d}")
        ctx(s, F, jti)
        st, d = attempt(s, "SELECT account_erasure_begin_operation(:o, :r)", {"o": F, "r": str(uuid.uuid4())})
        s.rollback()
        M("1:forge erasure op", st == "DENIED", f"begin erasure op with random receipt -> {st} {d}")
        for t in ("account_erasure_operations", "account_erasure_reauth_receipts"):
            ctx(s, F, jti)
            st, d = attempt(s, f"INSERT INTO {t} (owner_id) VALUES (:o)", {"o": F})
            s.rollback()
            M(f"1:raw insert {t}", st == "DENIED", f"-> {st} {d}")
    finally:
        s.close()
    M("1:head still locked", head_locked(superuser_db, key) is True, "")


# ============================================================ P1-2 / P1-3 reauth, JTI binding, step-up minting
def test_p1_23_step_up_minting_and_binding(locked, superuser_db):
    key, jti = locked
    s = runtime_session()
    try:
        ctx(s, F, jti, founder_step_up_issue="on")
        st, d = attempt(s, "INSERT INTO founder_step_up_receipts (owner_id,purpose,session_jti,verified_at,expires_at) VALUES (:o,'workflow_unlock',:j,now(),now()+interval '5 min')", {"o": F, "j": jti})
        s.rollback()
        M("3:raw mint step-up", st == "DENIED", f"-> {st} {d}")
        ctx(s, F, jti)
        st, d = attempt(s, "INSERT INTO founder_step_up_reauth_receipts (receipt_id,owner_id,access_jti,purpose,issued_at,expires_at) VALUES (gen_random_uuid(),:o,:j,'workflow_unlock',now(),now()+interval '5 min')", {"o": F, "j": jti})
        s.rollback()
        M("3:raw mint reauth receipt", st == "DENIED", f"-> {st} {d}")
        ctx(s, F, jti)
        st, d = attempt(s, "SELECT issue_founder_step_up_from_reauth('workflow_unlock', :r)", {"r": str(uuid.uuid4())})
        s.rollback()
        M("3:issue from forged receipt id", st == "DENIED", f"-> {st} {d}")
        ctx(s, F, jti)
        try:
            svc.issue_founder_step_up(s, purpose=StepUpPurpose.WORKFLOW_UNLOCK, password="wrong-password")
            st = "ALLOWED"
        except Exception as exc:  # noqa: BLE001
            st = f"DENIED {str(exc)[:60]}"
        s.rollback()
        M("2:wrong password", st.startswith("DENIED"), st)
        # cross-session: reauth for jti A, then issue from a session presenting jti B (another real Founder session)
        jti_b = founder_jti(superuser_db)
        ctx(s, F, jti)
        from app.mainai_founder_sovereignty.reauth import create_founder_step_up_reauth_receipt
        user = s.get(User, FOUNDER_USER_ID)
        rr = create_founder_step_up_reauth_receipt(s, user=user, password=FOUNDER_PASSWORD, access_jti=jti, purpose=StepUpPurpose.WORKFLOW_UNLOCK)
        s.commit()
        ctx(s, F, jti_b)
        st, d = attempt(s, "SELECT issue_founder_step_up_from_reauth('workflow_unlock', :r)", {"r": str(rr.receipt_id)})
        s.rollback()
        M("2:cross-session reauth use", st == "DENIED", f"reauth bound to jti A used from jti B -> {st} {d}")
        ctx(s, F, jti)
        st, d = attempt(s, "SELECT issue_founder_step_up_from_reauth('policy_rollback', :r)", {"r": str(rr.receipt_id)})
        s.rollback()
        M("2:purpose substitution", st == "DENIED", f"workflow_unlock reauth used for policy_rollback -> {st} {d}")
        # cross-account: a non-Founder session presenting the receipt
        kid = _family_user(superuser_db, "x"); superuser_db.commit()
        ctx(s, kid.id, jti)
        st, d = attempt(s, "SELECT issue_founder_step_up_from_reauth('workflow_unlock', :r)", {"r": str(rr.receipt_id)})
        s.rollback()
        M("2:cross-account", st == "DENIED", f"non-Founder session -> {st} {d}")
        # concurrent double-issue from ONE reauth receipt
        res = []
        bar = threading.Barrier(2)

        def issue():
            t = runtime_session()
            try:
                ctx(t, F, jti)
                bar.wait()
                t.execute(sa_text("SELECT issue_founder_step_up_from_reauth('workflow_unlock', :r)"), {"r": str(rr.receipt_id)})
                t.commit(); res.append("OK")
            except Exception as exc:  # noqa: BLE001
                t.rollback(); res.append(type(exc).__name__)
            finally:
                t.close()
        ts = [threading.Thread(target=issue) for _ in range(2)]
        [x.start() for x in ts]; [x.join() for x in ts]
        M("2:replay/race one reauth -> two step-ups", res.count("OK") == 1, f"{res}")
        # replay after consume
        ctx(s, F, jti)
        st, d = attempt(s, "SELECT issue_founder_step_up_from_reauth('workflow_unlock', :r)", {"r": str(rr.receipt_id)})
        s.rollback()
        M("2:replay consumed reauth", st == "DENIED", f"-> {st} {d}")
        # one step-up -> two unlocks (consume twice)
        key2 = f"k2_{uuid.uuid4().hex[:5]}"
        _bind_session(superuser_db, FOUNDER_USER_ID, jti)
        svc.apply_founder_policy(superuser_db, actor_kind=ActorKind.FOUNDER, policy_key=key2, payload={"v": 1}, reason="r")
        svc.set_workflow_lock(superuser_db, policy_key=key2, locked=True); superuser_db.commit()
        ctx(s, F, jti)
        a = attempt(s, "UPDATE founder_policy_heads SET workflow_locked=false WHERE policy_key=:k", {"k": key}); s.commit()
        ctx(s, F, jti)
        b = attempt(s, "UPDATE founder_policy_heads SET workflow_locked=false WHERE policy_key=:k", {"k": key2}); s.commit()
        M("3:one step-up unlocks two policies", not (a[0] == "ALLOWED" and b[0] == "ALLOWED"), f"first={a} second={b}")
        # revoked session: revoke jti then try issue with a fresh reauth
        superuser_db.execute(sa_text("INSERT INTO revoked_access_tokens (jti, expires_at) VALUES (:j, now()+interval '1 hour')"), {"j": jti}); superuser_db.commit()
        ctx(s, F, jti)
        try:
            svc.issue_founder_step_up(s, purpose=StepUpPurpose.WORKFLOW_UNLOCK, password=FOUNDER_PASSWORD); st = "ALLOWED"
        except Exception as exc:  # noqa: BLE001
            st = f"DENIED {str(exc)[:70]}"
        s.rollback()
        M("2:revoked session", st.startswith("DENIED"), st)
    finally:
        s.close()


# ============================================================ root of trust: runtime-writable credentials/session
def test_root_of_trust_runtime_can_forge_reauth_inputs(locked, superuser_db):
    """mainai_app has UPDATE users / INSERT refresh_tokens. If it can set the Founder password hash and mint a
    'current session', the password re-authentication that P1-2/3 rely on is forgeable from direct SQL."""
    key, _ = locked
    s = runtime_session()
    forged = "f" + uuid.uuid4().hex
    try:
        ctx(s, F)
        st1, d1 = attempt(s, "UPDATE users SET password_hash=:h WHERE id=:o", {"h": hash_password("attacker-chosen-pw"), "o": F})
        ctx(s, F)
        st2, d2 = attempt(s, "INSERT INTO refresh_tokens (id,user_id,family_id,token_hash,access_jti,csrf_token,created_at,expires_at) "
                             "VALUES (gen_random_uuid(),:o,gen_random_uuid(),:t,:j,:c,now()+interval '1 second',now()+interval '1 hour')",
                          {"o": F, "t": uuid.uuid4().hex * 2, "j": forged, "c": uuid.uuid4().hex * 2})
        M("R:runtime rewrites Founder password hash", st1 == "DENIED", f"-> {st1} {d1}")
        M("R:runtime mints a 'current' Founder session row", st2 == "DENIED", f"-> {st2} {d2}")
        chain = "not attempted"
        if st1 == "ALLOWED" and st2 == "ALLOWED":
            s.commit()
            ctx(s, F, forged)
            try:
                svc.issue_founder_step_up(s, purpose=StepUpPurpose.WORKFLOW_UNLOCK, password="attacker-chosen-pw")
                r = attempt(s, "UPDATE founder_policy_heads SET workflow_locked=false WHERE policy_key=:k", {"k": key})
                s.commit()
                chain = f"step-up issued; unlock {r[0]}"
            except Exception as exc:  # noqa: BLE001
                s.rollback(); chain = f"refused: {str(exc)[:80]}"
        M("R:end-to-end forged reauth -> step-up -> unlock", head_locked(superuser_db, key) is True, chain)
    finally:
        s.rollback(); s.close()
        # restore founder password for other tests
        superuser_db.execute(sa_text("UPDATE users SET password_hash=:h WHERE id=:o"), {"h": hash_password(FOUNDER_PASSWORD), "o": F}); superuser_db.commit()


# ============================================================ P1-4 snapshot hash + action/limit substitution
def _request(su, jti, cap="lights.control"):
    _bind_session(su, FOUNDER_USER_ID, jti)
    kid = _family_user(su, "snap")
    svc.add_family_member(su, principal_id=kid.id, relationship=FamilyRelationship.CHILD, display_name="k")
    req = svc.request_family_capability(su, principal_id=kid.id, capability_key=cap)
    su.commit()
    return kid, req


def test_p1_4_snapshot_and_substitution(superuser_db):
    _ensure_founder(superuser_db)
    jti = founder_jti(superuser_db)
    kid, req = _request(superuser_db, jti)
    shown = req.snapshot_hash
    s = runtime_session()
    try:
        for name, sql in (("capability swap", "UPDATE family_approval_requests SET capability_key='purchases.create', resource='purchases', action='create', risk_tier='high' WHERE id=:r"),
                          ("limit swap", "UPDATE family_approval_requests SET requested_limits='{\"max_amount\": 100000}' WHERE id=:r"),
                          ("hash rewrite", "UPDATE family_approval_requests SET snapshot_hash=repeat('a',64) WHERE id=:r"),
                          ("delete request", "DELETE FROM family_approval_requests WHERE id=:r")):
            for g in ({}, GUCS):
                ctx(s, F, jti, **g)
                st, d = attempt(s, sql, {"r": str(req.id)})
                s.rollback()
                M(f"4:{name}{' +GUCs' if g else ''}", st == "DENIED", f"runtime -> {st} {d}")
    finally:
        s.close()
    for name, kw in (("no hash", {"expected_snapshot_hash": ""}), ("wrong hash", {"expected_snapshot_hash": "0" * 64}),
                     ("widened limits", {"expected_snapshot_hash": shown, "limits": {"max_amount": 10**6}})):
        _bind_session(superuser_db, FOUNDER_USER_ID, jti)
        try:
            svc.decide_approval(superuser_db, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW, **kw); st = "ACCEPTED"
        except Exception as exc:  # noqa: BLE001
            st = f"refused {str(exc)[:60]}"
        superuser_db.rollback()
        M(f"4:decide {name}", st.startswith("refused"), st)


def test_p1_4_race_rewrite_vs_decide(superuser_db):
    """Runtime rewrites the request concurrently with the Founder's decision (approval/execution race)."""
    _ensure_founder(superuser_db)
    jti = founder_jti(superuser_db)
    out = []
    for trial in range(5):
        kid, req = _request(superuser_db, jti)
        rid, rhash = req.id, req.snapshot_hash  # plain values; threads must not touch the fixture's ORM instance
        bar = threading.Barrier(2)
        res = {}

        def rewrite():
            t = runtime_session()
            try:
                ctx(t, F, jti, **GUCS); bar.wait()
                t.execute(sa_text("UPDATE family_approval_requests SET requested_limits=CAST(:l AS jsonb) WHERE id=:r"), {"r": str(rid), "l": '{"max_amount": 99999}'}); t.commit(); res["rw"] = "OK"
            except Exception as exc:  # noqa: BLE001
                t.rollback(); res["rw"] = type(exc).__name__
            finally:
                t.close()

        def decide():
            t = runtime_session()
            try:
                ctx(t, F, jti); bar.wait()
                svc.decide_approval(t, request_id=rid, mode=ApprovalMode.ALWAYS_ALLOW, expected_snapshot_hash=rhash); t.commit(); res["dec"] = "OK"
            except Exception as exc:  # noqa: BLE001
                t.rollback(); res["dec"] = f"{type(exc).__name__}:{str(exc)[:50]}"
            finally:
                t.close()
        ts = [threading.Thread(target=rewrite), threading.Thread(target=decide)]
        [x.start() for x in ts]; [x.join() for x in ts]
        superuser_db.expire_all()
        lim = superuser_db.execute(sa_text("SELECT requested_limits FROM family_approval_requests WHERE id=:r"), {"r": str(rid)}).scalar_one()
        out.append((res, lim))
    bad = [o for o in out if o[0].get("dec") == "OK" and o[0].get("rw") == "OK"]
    decided = sum(1 for o in out if o[0].get("dec") == "OK")
    M("4:race rewrite vs decide", not bad and decided > 0, f"decided={decided}/5 (must be >0 for a valid race) {out}")


# ============================================================ P1-5 erasure authorization, purpose isolation, consumed reauth
def test_p1_5_erasure(superuser_db, make_verified_user):
    from app.account.reauth import create_account_erasure_reauth_receipt  # noqa: F401
    import inspect
    from app.account import reauth as ar
    print(f"\nINFO account reauth API: {[n for n, _ in inspect.getmembers(ar, inspect.isfunction) if not n.startswith('_')]}")
    # purpose isolation: a Founder step-up reauth receipt id presented to account_erasure_begin_operation
    _ensure_founder(superuser_db)
    jti = founder_jti(superuser_db)
    s = runtime_session()
    try:
        ctx(s, F, jti)
        from app.mainai_founder_sovereignty.reauth import create_founder_step_up_reauth_receipt
        rr = create_founder_step_up_reauth_receipt(s, user=s.get(User, FOUNDER_USER_ID), password=FOUNDER_PASSWORD, access_jti=jti, purpose=StepUpPurpose.WORKFLOW_UNLOCK)
        s.commit()
        ctx(s, F, jti)
        st, d = attempt(s, "SELECT account_erasure_begin_operation(:o, :r)", {"o": F, "r": str(rr.receipt_id)})
        s.rollback()
        M("5:step-up reauth used as erasure receipt", st == "DENIED", f"-> {st} {d}")
    finally:
        s.close()


# ============================================================ end-to-end governed erasure (runtime role, router-equivalent path)
def _founder_with_state(su):
    _ensure_founder(su)
    jti = founder_jti(su)
    _bind_session(su, FOUNDER_USER_ID, jti)
    key = f"e_{uuid.uuid4().hex[:5]}"
    svc.apply_founder_policy(su, actor_kind=ActorKind.FOUNDER, policy_key=key, payload={"v": 1}, reason="r")
    svc.set_workflow_lock(su, policy_key=key, locked=True)
    kid = _family_user(su, "ekid")
    svc.add_family_member(su, principal_id=kid.id, relationship=FamilyRelationship.CHILD, display_name="k")
    req = svc.request_family_capability(su, principal_id=kid.id, capability_key="lights.control")
    svc.decide_approval(su, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW, expected_snapshot_hash=req.snapshot_hash)
    su.commit()
    return jti


SOV_TABLES = ("founder_policy_versions", "founder_policy_heads", "family_members", "family_approval_requests", "family_approval_receipts",
              "family_capability_grants", "founder_instance_bindings")


def _counts(su):
    su.expire_all()
    return {t: su.execute(sa_text(f"SELECT count(*) FROM {t} WHERE owner_id=:o"), {"o": F}).scalar_one() for t in SOV_TABLES}


def _erase_as_runtime(jti, password=FOUNDER_PASSWORD, fail_at=None):
    from app.account.erasure import erase_account_data
    from app.account.reauth import create_account_erasure_reauth_receipt
    from app.db import current_user_id, current_access_jti
    t1, t2 = current_user_id.set(F), current_access_jti.set(jti)  # what app/deps.py does per request
    s = runtime_session()
    try:
        ctx(s, F, jti)
        user = s.get(User, FOUNDER_USER_ID)
        try:
            rr = create_account_erasure_reauth_receipt(s, user=user, password=password, access_jti=jti)
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            return f"FAILED {type(exc).__name__}: {str(exc)[:80]}"
        if fail_at:
            orig = s.execute

            def boom(stmt, *a, **k):
                if fail_at in str(stmt):
                    raise RuntimeError("examiner-injected failure")
                return orig(stmt, *a, **k)
            s.execute = boom
        try:
            erase_account_data(s, user, reauth_receipt_id=rr.receipt_id, reauth_access_jti=jti)
            return "ERASED"
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            return f"FAILED {type(exc).__name__}: {str(exc)[:80]}"
    finally:
        s.close()
        current_user_id.reset(t1); current_access_jti.reset(t2)


def test_e2e_founder_erasure_atomic_rollback_after_sovereignty_wipe(superuser_db):
    jti = _founder_with_state(superuser_db)
    before = _counts(superuser_db)
    out = _erase_as_runtime(jti, fail_at="account_erasure_complete_operation")
    after = _counts(superuser_db)
    user_left = superuser_db.execute(sa_text("SELECT count(*) FROM users WHERE id=:o"), {"o": F}).scalar_one()
    M("5:atomic rollback after sovereignty wipe", out.startswith("FAILED") and after == before and user_left == 1,
      f"injected failure at complete_operation -> {out}; sovereignty rows before={before} after={after}; founder row={user_left}")


def test_e2e_founder_erasure_succeeds_and_wipes_sovereignty(superuser_db):
    jti = _founder_with_state(superuser_db)
    before = _counts(superuser_db)
    out = _erase_as_runtime(jti)
    after = _counts(superuser_db)
    user_left = superuser_db.execute(sa_text("SELECT count(*) FROM users WHERE id=:o"), {"o": F}).scalar_one()
    M("5:e2e Founder erasure (runtime role)", out == "ERASED" and user_left == 0 and not any(after.values()),
      f"{out}; before={before} after={after}; founder row={user_left}")


def test_e2e_erasure_wrong_password_and_replay(superuser_db):
    jti = _founder_with_state(superuser_db)
    out = _erase_as_runtime(jti, password="wrong")
    M("5:erasure wrong password", out.startswith("FAILED"), out)


# ============================================================ root of trust through the REAL HTTP surface (no forged session row)
def test_root_of_trust_http_chain(superuser_db, client, db_session):
    """Attacker capability: arbitrary SQL as mainai_app (e.g. an injection). Everything after that is the real API."""
    founder = _ensure_founder(superuser_db)
    email = founder.email
    superuser_db.execute(sa_text("UPDATE users SET email_verified=true, is_active=true WHERE id=:o"), {"o": F})
    jti = founder_jti(superuser_db)
    _bind_session(superuser_db, FOUNDER_USER_ID, jti)
    key = f"h_{uuid.uuid4().hex[:5]}"
    svc.apply_founder_policy(superuser_db, actor_kind=ActorKind.FOUNDER, policy_key=key, payload={"v": 1}, reason="v1")
    svc.apply_founder_policy(superuser_db, actor_kind=ActorKind.FOUNDER, policy_key=key, payload={"v": 2}, reason="v2")
    superuser_db.commit()
    # 1. runtime-role SQL: overwrite the Founder's password hash
    assert db_session.execute(sa_text("select current_user")).scalar_one() == "mainai_app"
    ctx(db_session, F)
    st, d = attempt(db_session, "UPDATE users SET password_hash=:h WHERE id=:o", {"h": hash_password("attacker-pw-123!"), "o": F})
    db_session.commit()
    # 2..4: real routes only
    r1 = client.post("/api/auth/login", json={"email": email, "password": "attacker-pw-123!"})
    csrf = r1.json().get("csrf_token") if r1.status_code == 200 else None
    h = {"x-csrf-token": csrf} if csrf else {}
    r2 = client.post("/api/founder-sovereignty/step-up", json={"purpose": "policy_rollback", "password": "attacker-pw-123!"}, headers=h)
    r3 = client.post("/api/founder-sovereignty/policies/rollback", json={"policy_key": key, "reason": "attacker"}, headers=h)
    superuser_db.execute(sa_text("UPDATE users SET password_hash=:h WHERE id=:o"), {"h": hash_password(FOUNDER_PASSWORD), "o": F}); superuser_db.commit()
    M("R:HTTP chain (SQL password rewrite -> login -> step-up -> rollback)", not (st == "ALLOWED" and r3.status_code == 200),
      f"sql={st}; login={r1.status_code}; step-up={r2.status_code} {r2.text[:80]}; rollback={r3.status_code} {r3.text[:80]}")
