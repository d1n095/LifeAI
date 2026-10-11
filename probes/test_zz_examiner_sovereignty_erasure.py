"""Examiner-only: does Founder-owned sovereignty state block Founder account erasure?"""
from app.db import SessionLocal
from app.founder import FOUNDER_USER_ID
from app.mainai_founder_sovereignty.service import add_family_member, apply_founder_policy, decide_approval, request_family_capability
from app.mainai_founder_sovereignty.types import ActorKind, ApprovalMode, FamilyRelationship
from app.models.user import User, UserRole
from app.security import hash_password
from tests.backend.mainai.test_founder_sovereignty import _family_user
from tests.backend.test_account_erasure import (  # noqa: F401
    _narrow_privileges_before_this_module, _set_rls_user, erase_account_data,
)


def _fresh_founder(session):
    u = session.get(User, FOUNDER_USER_ID)
    if u is None:
        u = User(id=FOUNDER_USER_ID, email="f-erasure@lifeos.local", password_hash=hash_password("Sup3rS3cret!"), role=UserRole.founder, email_verified=True)
        session.add(u)
    else:
        u.password_hash = hash_password("Sup3rS3cret!")
    session.commit()
    return u


def _attempt(label, seed):
    session = SessionLocal()
    try:
        founder = _fresh_founder(session)
        _set_rls_user(session, FOUNDER_USER_ID)
        seed(session)
        session.commit()
        _set_rls_user(session, FOUNDER_USER_ID)
        try:
            erase_account_data(session, founder)
            ok, err = session.get(User, FOUNDER_USER_ID) is None, ""
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{type(exc).__name__}: {str(exc).splitlines()[0][:150]}"
        print(f"\n{'HOLDS' if ok else 'GAP  '} Founder account erasure succeeds {label} {err}")
    finally:
        session.rollback()
        session.close()


def test_founder_erasure_with_and_without_sovereignty_state():
    _attempt("[CONTROL: no sovereignty state]", lambda s: None)
    _attempt("[after one Founder policy version]", lambda s: apply_founder_policy(
        s, actor_id=FOUNDER_USER_ID, actor_kind=ActorKind.FOUNDER, policy_key="speech", payload={"x": 1}, reason="r"))

    def family(s):
        kid = _family_user(s, "erasekid")
        add_family_member(s, actor_id=FOUNDER_USER_ID, principal_id=kid.id, relationship=FamilyRelationship.CHILD, display_name="k")
        req = request_family_capability(s, principal_id=kid.id, capability_key="lights.control")
        decide_approval(s, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALWAYS_ALLOW)
    _attempt("[after one family approval]", family)
