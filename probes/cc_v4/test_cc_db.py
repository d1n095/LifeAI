"""Examiner-only DB probes, Continuous Conversation P1-fix-4 (runtime role = mainai_app via target conftest)."""
from __future__ import annotations

import threading
import uuid

from sqlalchemy import text as T

from app.db import SessionLocal

DENY = ("InsufficientPrivilege", "permission denied", "RaiseException", "CheckViolation", "UniqueViolation", "ForeignKeyViolation", "row-level security")


def M(pid, ok, ev):
    print(f"\nMATRIX|{pid}|{'HOLDS' if ok else 'GAP'}|{ev}")


def ctx(db, uid):
    db.execute(T("SELECT set_config('app.current_user_id', :u, true)"), {"u": str(uid)})


def attempt(db, sql, p=None):
    try:
        with db.begin_nested():
            r = db.execute(T(sql), p or {})
            out = r.fetchall() if r.returns_rows else r.rowcount
        if not r.returns_rows and not out:
            return "PROBE_ERROR", "0 rows"
        return "ALLOWED", str(out)[:90]
    except Exception as e:  # noqa: BLE001
        m = str(e).splitlines()[0][:130]
        return ("DENIED" if any(d in m for d in DENY) else "PROBE_ERROR"), m


def seed(su, make_verified_user, n_msgs=4):
    u, _ = make_verified_user()
    c = uuid.uuid4()
    su.execute(T("SELECT set_config('app.current_user_id', :u, true)"), {"u": str(u.id)})
    su.execute(T("INSERT INTO conversations (id,user_id,title,created_at,updated_at) VALUES (:c,:u,'t',now(),now())"), {"c": str(c), "u": str(u.id)})
    msgs = []
    for i in range(n_msgs):
        m = uuid.uuid4()
        su.execute(T("INSERT INTO messages (id,conversation_id,role,content,created_at) VALUES (:m,:c,'user',:t,now())"), {"m": str(m), "c": str(c), "t": f"m{i}"})
        msgs.append(m)
    su.commit()
    return u.id, c, msgs


REC = "SELECT record_founder_conversation_decision(:o,:c,:m,:k,'stmt',:v,'{}'::jsonb,now())"


def rec(db, o, c, m, k="deploy_target", v="x"):
    return db.execute(T(REC), {"o": str(o), "c": str(c), "m": str(m), "k": k, "v": v}).scalar_one()


def active(su, o, c, k="deploy_target"):
    su.expire_all()
    return su.execute(T("SELECT count(*) FROM founder_conversation_decisions WHERE owner_id=:o AND conversation_id=:c AND decision_key=:k AND status='active'"),
                      {"o": str(o), "c": str(c), "k": k}).scalar_one()


def test_p1_4_supersession_and_direct_bypass(db_session, superuser_db, make_verified_user):
    o, c, ms = seed(superuser_db, make_verified_user)
    assert db_session.execute(T("select current_user")).scalar_one() == "mainai_app"
    ctx(db_session, o)
    d1 = rec(db_session, o, c, ms[0], v="v1"); d2 = rec(db_session, o, c, ms[1], v="v2"); db_session.commit()
    row1 = superuser_db.execute(T("SELECT status, superseded, superseded_by FROM founder_conversation_decisions WHERE id=:i"), {"i": str(d1)}).one()
    M("4:supersession chain", tuple(row1) == ("historical", True, d2) and active(superuser_db, o, c) == 1, f"old={tuple(row1)} new={d2} active={active(superuser_db, o, c)}")
    ctx(db_session, o)
    again = rec(db_session, o, c, ms[1], v="v2-replay"); db_session.commit()
    M("4:idempotent replay same message", again == d2 and active(superuser_db, o, c) == 1, f"replay returned {'same id' if again == d2 else again}")
    # direct runtime writes that bypass the function
    for name, sql, p in (
        ("second active row via raw INSERT", "INSERT INTO founder_conversation_decisions (id,owner_id,conversation_id,message_id,topic,decision_key,statement,value,identifiers,superseded,status,effective_at,created_at) "
         "VALUES (gen_random_uuid(),:o,:c,:m,'deploy_target','deploy_target','s','evil','{}'::jsonb,false,'active',now(),now())", {"o": str(o), "c": str(c), "m": str(ms[2])}),
        ("rewrite ACTIVE decision value in place", "UPDATE founder_conversation_decisions SET value='rewritten', statement='rewritten' WHERE id=:i", {"i": str(d2)}),
        ("rewrite HISTORICAL decision in place", "UPDATE founder_conversation_decisions SET value='history-rewritten' WHERE id=:i", {"i": str(d1)}),
        ("un-supersede history (historical->active, active->historical)", "UPDATE founder_conversation_decisions SET status=CASE WHEN id=:a THEN 'historical' ELSE 'active' END, superseded=(id=:a) WHERE id IN (:a,:b)", {"a": str(d2), "b": str(d1)}),
        ("inconsistent: active + superseded=true + superseded_by=self", "UPDATE founder_conversation_decisions SET superseded=true, superseded_by=id WHERE id=:i", {"i": str(d2)}),
        ("empty decision_key escapes uniqueness", "INSERT INTO founder_conversation_decisions (id,owner_id,conversation_id,message_id,topic,decision_key,statement,value,identifiers,superseded,status,effective_at,created_at) "
         "VALUES (gen_random_uuid(),:o,:c,:m,'deploy_target','','s','evil','{}'::jsonb,false,'active',now(),now())", {"o": str(o), "c": str(c), "m": str(ms[3])}),
        ("delete decision history", "DELETE FROM founder_conversation_decisions WHERE id=:i", {"i": str(d1)}),
    ):
        ctx(db_session, o)
        st, d = attempt(db_session, sql, p)
        db_session.rollback()
        M(f"4:runtime {name}", st == "DENIED", f"-> {st} {d}")


def test_p1_4_concurrent_writers(superuser_db, make_verified_user):
    o, c, ms = seed(superuser_db, make_verified_user, n_msgs=16)
    results = []
    for trial in range(4):
        bar = threading.Barrier(4)
        out = []

        def go(m):
            s = SessionLocal()
            try:
                ctx(s, o); bar.wait()
                out.append(str(rec(s, o, c, m, v=f"t{trial}"))); s.commit()
            except Exception as e:  # noqa: BLE001
                s.rollback(); out.append(type(e).__name__)
            finally:
                s.close()
        ts = [threading.Thread(target=go, args=(ms[trial * 4 + i],)) for i in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        n_active = active(superuser_db, o, c)
        broken = superuser_db.execute(T("SELECT count(*) FROM founder_conversation_decisions WHERE owner_id=:o AND status='historical' AND superseded_by IS NULL"), {"o": str(o)}).scalar_one()
        results.append((n_active, broken, sorted(set(x if len(x) < 30 else 'id' for x in out))))
    ok = all(a == 1 for a, _, _ in results)
    M("4:concurrent writers keep exactly one active", ok, f"per trial (active, historical_without_successor, outcomes): {results}")
    M("4:concurrent writers keep a complete supersession chain", all(b == 0 for _, b, _ in results), f"{results}")


def test_p1_4_definer_cross_owner(db_session, superuser_db, make_verified_user):
    victim, vc, vms = seed(superuser_db, make_verified_user)
    attacker, _, _ = seed(superuser_db, make_verified_user)
    ctx(db_session, victim); vd = rec(db_session, victim, vc, vms[0], v="victim-choice"); db_session.commit()
    ctx(db_session, attacker)  # runtime session bound to ANOTHER owner
    st, d = attempt(db_session, REC, {"o": str(victim), "c": str(vc), "m": str(vms[1]), "k": "deploy_target", "v": "attacker-choice"})
    db_session.commit()
    row = superuser_db.execute(T("SELECT status FROM founder_conversation_decisions WHERE id=:i"), {"i": str(vd)}).scalar_one()
    M("4:definer fn rejects owner != session user", st == "DENIED", f"attacker-session call for victim -> {st} {d}; victim's decision now {row}")
    ctx(db_session, attacker)
    st2, d2 = attempt(db_session, REC.replace(":o", "NULL"), {"c": str(vc), "m": str(vms[2]), "k": "k2", "v": "x"})
    db_session.rollback()
    M("4:definer fn with no session user", True if st2 != "ALLOWED" else False, f"-> {st2} {d2}")
