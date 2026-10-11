from sqlalchemy import text


def pytest_collection_modifyitems(items):
    mods = {it.module for it in items if it.module.__name__.endswith("test_founder_sovereignty")}
    for m in mods:
        _patch(m)


def _patch(m):
    orig = m._attack

    def wrapped(db, sql, params=None):
        if sql.strip().startswith("UPDATE founder_policy_heads"):
            vis = db.execute(text("SELECT policy_key, workflow_locked FROM founder_policy_heads")).all()
            uid = db.execute(text("select current_setting('app.current_user_id', true), current_user")).one()
            try:
                with db.begin_nested():
                    inside = db.execute(text("SELECT count(*), current_setting('app.current_user_id', true) FROM founder_policy_heads WHERE policy_key='runtime_lock'")).one()
                    r = db.execute(text(sql), params or {})
                    out = f"inside_savepoint={tuple(inside)} rowcount={r.rowcount}"
                    raise RuntimeError("diag")
            except RuntimeError:
                pass
            except Exception as e:  # noqa: BLE001
                out = "RAISED " + str(e).splitlines()[0][:90]
            print(f"\nDIAG ctx={tuple(uid)} visible_heads={vis} -> {out}")
        return orig(db, sql, params)
    m._attack = wrapped
