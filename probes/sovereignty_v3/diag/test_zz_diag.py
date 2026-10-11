from sqlalchemy import text
from app.founder import FOUNDER_USER_ID
from tests.backend.mainai.test_founder_sovereignty import _ensure_founder


def test_diag(db_session, superuser_db):
    _ensure_founder(superuser_db); superuser_db.commit()
    q = lambda s: db_session.execute(text(s)).all()
    print("\nDIAG current_user/session_user:", q("select current_user, session_user"))
    print("DIAG grants:", superuser_db.execute(text("select string_agg(privilege_type,',') from information_schema.role_table_grants where grantee='mainai_app' and table_name='founder_step_up_receipts'")).scalar())
    print("DIAG triggers:", superuser_db.execute(text("select tgname, tgenabled from pg_trigger where tgrelid='founder_step_up_receipts'::regclass and not tgisinternal")).all())
    db_session.execute(text("SELECT set_config('app.current_user_id', :u, true)"), {"u": str(FOUNDER_USER_ID)})
    db_session.execute(text("SELECT set_config('app.current_access_jti', 'forged-jti-value', true)"))
    db_session.execute(text("SELECT set_config('app.founder_step_up_issue', 'on', true)"))
    print("DIAG erasure_authorized:", q("select founder_sovereignty_erasure_authorized()"))
    try:
        with db_session.begin_nested():
            db_session.execute(text("INSERT INTO founder_step_up_receipts (owner_id, purpose, session_jti, expires_at) VALUES (:f,'workflow_unlock','forged-jti-value', now()+interval '15 minutes')"), {"f": str(FOUNDER_USER_ID)})
            db_session.flush()
        print("DIAG INSERT: SUCCEEDED")
    except Exception as e:
        print("DIAG INSERT raised:", str(e).splitlines()[0][:150])
