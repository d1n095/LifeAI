"""Examiner-only follow-ups: correct concurrency harness; binding squat under real RLS."""
import threading
from sqlalchemy import text as sa_text
from sqlalchemy.orm import sessionmaker
from app.db import SessionLocal, migration_engine
from app.founder import FOUNDER_USER_ID
from app.mainai_founder_sovereignty.service import authorize_capability, bind_founder_instance, decide_approval, request_family_capability, add_family_member
from app.mainai_founder_sovereignty.types import ApprovalMode, AuthorizationVerdict, FamilyRelationship
from tests.backend.mainai.test_founder_sovereignty import _ensure_founder, _family_user


def test_concurrency(superuser_db):
    _ensure_founder(superuser_db)
    kid = _family_user(superuser_db, "racer2")
    add_family_member(superuser_db, actor_id=FOUNDER_USER_ID, principal_id=kid.id, relationship=FamilyRelationship.CHILD, display_name="k")
    req = request_family_capability(superuser_db, principal_id=kid.id, capability_key="family_calendar.create_event")
    decide_approval(superuser_db, actor_id=FOUNDER_USER_ID, request_id=req.id, mode=ApprovalMode.ALLOW_ONCE)
    kid_id = kid.id
    superuser_db.commit()
    for trial in range(5):
        superuser_db.execute(sa_text("UPDATE family_capability_grants SET remaining_uses=1 WHERE principal_id=:p"), {"p": str(kid_id)})
        superuser_db.commit()
        S, barrier, results = sessionmaker(bind=migration_engine), threading.Barrier(2), []

        def use():
            s = S()
            try:
                barrier.wait()
                v = authorize_capability(s, principal_id=kid_id, capability_key="family_calendar.create_event", consume=True).verdict
                s.commit()
                results.append(v.value)
            except Exception as exc:  # noqa: BLE001
                results.append(f"EXC:{type(exc).__name__}")
            finally:
                s.close()
        ts = [threading.Thread(target=use) for _ in range(2)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        print(f"\n{'HOLDS' if results.count('allowed') <= 1 else 'GAP  '} trial {trial}: ALLOW_ONCE concurrent consumers -> {results}")


def test_squat_under_rls(superuser_db):
    _ensure_founder(superuser_db)
    superuser_db.execute(sa_text("DELETE FROM founder_instance_bindings"))
    squatter = _family_user(superuser_db, "squat2")
    sq_id = squatter.id
    superuser_db.commit()
    app = SessionLocal()
    try:
        app.execute(sa_text("SELECT set_config('app.current_user_id', :o, true)"), {"o": str(sq_id)})
        app.execute(sa_text("INSERT INTO founder_instance_bindings (owner_id, founder_user_id) VALUES (:o, :f)"), {"o": str(sq_id), "f": str(FOUNDER_USER_ID)})
        app.commit()
        app.execute(sa_text("SELECT set_config('app.current_user_id', :o, true)"), {"o": str(FOUNDER_USER_ID)})
        try:
            b = bind_founder_instance(app, actor_id=FOUNDER_USER_ID)
            app.commit()
            res = f"bound, owner={b.owner_id}"
        except Exception as exc:  # noqa: BLE001
            app.rollback()
            res = f"FAILED {type(exc).__name__}: {str(exc).splitlines()[0][:100]}"
        owner = superuser_db.execute(sa_text("SELECT owner_id FROM founder_instance_bindings")).scalars().all()
        print(f"\n{'HOLDS' if res.startswith('bound') else 'GAP  '} real Founder (mainai_app, Founder RLS ctx) binds after squat -> {res}; binding owners now {owner} (squatter={sq_id})")
    finally:
        app.close()
        superuser_db.execute(sa_text("DELETE FROM founder_instance_bindings"))
        superuser_db.commit()
