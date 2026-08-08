"""Coverage for the schema-wide privilege floor: `mainai_app` must hold NONE of
TRUNCATE/REFERENCES/TRIGGER on ANY table in schema public
(`_NEVER_GRANTED_TABLE_PRIVS` in backend/scripts/s1a_privilege_policy.py).

Origin — PR #42's independent security review, deferred there as explicitly non-blocking:
migration 0031 gave `messages` owner-scoped RLS, but `mainai_app` still held TRUNCATE on it,
and **Postgres RLS does not apply to TRUNCATE**. TRUNCATE is a whole-table operation; no
`USING`/`WITH CHECK` clause is ever evaluated, so a single `TRUNCATE messages` would have
wiped every owner's rows with RLS fully enabled and FORCEd and no policy violated. The same
review established the finding was project-wide (`conversations`, `documents`,
`document_chunks`, …), a consequence of `GRANT ALL PRIVILEGES ON ALL TABLES`, not of anything
specific to `messages`.

Written in the same style as tests/backend/test_rls_policy_registry.py: assertions are made
against Postgres's OWN live catalog state (`has_table_privilege`, `pg_default_acl`), never
against the text of a GRANT statement — a migration or bootstrap script "containing the right
SQL" is not the same claim as "the database actually ended up in the right state", which is
precisely how the original blanket grant survived unnoticed. `has_table_privilege` rather than
`information_schema.role_table_grants` for the reason app/rls.py's `_effective_table_privileges`
documents: the information_schema view shows only DIRECT grants, so a privilege held
indirectly through role membership would be invisible there while still being usable.
"""

import ast
import importlib.util
from pathlib import Path

import psycopg2
import psycopg2.errors
import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db import migration_engine

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
POLICY_PATH = BACKEND_ROOT / "scripts" / "s1a_privilege_policy.py"

# The four tables PR #42's review named explicitly. The floor covers every table in the
# schema, but these are the ones whose user data made the finding worth acting on, so they get
# their own named, non-parameterised-away coverage.
USER_DATA_TABLES = ["messages", "conversations", "documents", "document_chunks"]


def _load_policy():
    """The policy lives outside the `app` package on purpose (it must run standalone, pre-boot,
    before `app` is importable — see backend/docker-entrypoint.sh), so it is loaded by path,
    the same way tests/backend/test_ensure_app_role.py loads ensure_app_role.py."""
    spec = importlib.util.spec_from_file_location("s1a_privilege_policy", POLICY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _all_public_tables(conn):
    return list(
        conn.execute(
            text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        ).scalars()
    )


@pytest.fixture
def restore_privileges():
    """Restores the uniform DML floor backend/tests/conftest.py's `_test_database` fixture
    grants, after a test has deliberately mutated `mainai_app`'s privileges.

    Necessary because `apply_privilege_policy()` legitimately narrows the S1A tables FURTHER
    than the session-wide baseline (e.g. `memory_source_units` down to SELECT+INSERT, and
    `storage_deletion_tasks` to nothing at all). That is production-correct, but it is not the
    state the rest of this pytest session was set up with, and privileges are database state
    that outlives the per-test transaction — unlike rows, `_clean_tables` does not reset them.
    Without this, a test here would silently change the privilege environment every test that
    happens to run after it executes in."""
    yield
    settings = get_settings()
    admin = psycopg2.connect(settings.database_url)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mainai_app")
    admin.close()


# --------------------------------------------------------------------------------------
# 1. The floor, as it actually is right now, measured
# --------------------------------------------------------------------------------------


def test_no_table_in_public_grants_the_runtime_role_truncate_references_or_trigger():
    """The headline invariant, over EVERY table — not just the four the review named. Reads
    Postgres's effective privilege answer per (table, privilege) pair and reports every
    violation at once rather than failing on the first, so a regression that re-widens several
    tables is diagnosable from one run."""
    policy = _load_policy()
    violations = []
    with migration_engine.connect() as conn:
        tables = _all_public_tables(conn)
        assert tables, "no tables found in schema public — the test database was not migrated"
        for table in tables:
            for priv in policy._NEVER_GRANTED_TABLE_PRIVS:
                held = conn.execute(
                    text("SELECT has_table_privilege('mainai_app', :t, :p)"),
                    {"t": f'public."{table}"', "p": priv},
                ).scalar()
                if held:
                    violations.append(f"{table}.{priv}")
    assert violations == [], (
        "mainai_app holds privileges it must never hold: "
        f"{violations}. RLS does not apply to TRUNCATE, and REFERENCES/TRIGGER are DDL-only "
        "privileges no runtime code path uses."
    )


@pytest.mark.parametrize("table", USER_DATA_TABLES)
def test_named_user_data_tables_keep_exactly_the_four_dml_privileges(table):
    """The complement of the test above: the reduction must not have overshot. Each table PR
    #42's review named still needs all four DML privileges, and every one is exercised by a
    real code path — SELECT/INSERT in app/routers/chat.py (messages, conversations) and
    app/rag/vector_store.py (document_chunks), UPDATE in
    app/rag/message_sequence_backfill.py's `UPDATE messages m SET sequence_number = ...`,
    DELETE in app/rag/account_erasure.py's per-owner erasure. Those stay row-scoped under RLS,
    which is exactly what TRUNCATE would have bypassed."""
    with migration_engine.connect() as conn:
        held = {
            priv
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE")
            if conn.execute(
                text("SELECT has_table_privilege('mainai_app', :t, :p)"),
                {"t": f'public."{table}"', "p": priv},
            ).scalar()
        }
    assert held == {"SELECT", "INSERT", "UPDATE", "DELETE"}, (
        f"{table}: mainai_app should still hold exactly the four DML privileges, has {sorted(held)}"
    )


# --------------------------------------------------------------------------------------
# 2. Mutation tests — the privilege is genuinely DENIED, not merely absent from a catalog
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("table", USER_DATA_TABLES)
def test_truncate_is_actually_rejected_when_attempted_as_the_real_runtime_role(table):
    """The proof that matters: connect as `mainai_app` — the real credential the application
    serves traffic with (APP_DATABASE_URL), not a mock and not the admin role — and issue the
    statement the review was worried about. Postgres must reject it.

    A catalog assertion alone would not close this: `has_table_privilege` returning false and
    the server actually refusing the statement are two different claims, and it is the second
    one the security property depends on."""
    settings = get_settings()
    conn = psycopg2.connect(settings.app_database_url)
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute(f"TRUNCATE TABLE {table}")
    finally:
        conn.rollback()
        conn.close()


def test_runtime_role_can_still_read_the_same_tables_it_cannot_truncate():
    """Guards against the reduction having gone too far in the crudest possible way: the same
    role, the same connection style, the same tables — an ordinary SELECT must still succeed.
    (Row visibility is RLS's job and is covered in tests/security/test_rls_isolation.py; this
    asserts only that the SELECT privilege itself survived.)"""
    settings = get_settings()
    conn = psycopg2.connect(settings.app_database_url)
    try:
        with conn.cursor() as cur:
            for table in USER_DATA_TABLES:
                cur.execute(f"SELECT count(*) FROM {table}")
                assert cur.fetchone()[0] is not None
    finally:
        conn.rollback()
        conn.close()


def test_runtime_role_can_still_insert_update_and_delete_its_own_rows(db_session, make_verified_user):
    """End-to-end DML through the restricted role with RLS engaged, using the same
    `SET LOCAL app.current_user_id` pattern tests/security/test_rls_isolation.py establishes —
    so this exercises INSERT, UPDATE and DELETE on `conversations`/`messages` as an owner,
    not merely the presence of a privilege bit. This is the path that must NOT have been
    broken by removing TRUNCATE."""
    from app.models.conversation import Conversation, Message

    user, _ = make_verified_user()
    db_session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})

    conversation = Conversation(title="privilege floor probe", user_id=user.id)
    db_session.add(conversation)
    db_session.flush()

    message = Message(conversation_id=conversation.id, role="user", content="hello")
    db_session.add(message)
    db_session.flush()

    message.content = "edited"  # UPDATE
    db_session.flush()

    # Deleted child-first with an explicit flush between the two: no SQLAlchemy
    # `relationship()` links these models, so the unit of work has no dependency edge to order
    # them by and would otherwise emit the parent DELETE first, tripping
    # `messages_conversation_id_fkey`. That is a property of the test's own hand-built object
    # graph, not of the privilege change under test.
    db_session.delete(message)  # DELETE on messages
    db_session.flush()
    db_session.delete(conversation)  # DELETE on conversations
    db_session.commit()


# --------------------------------------------------------------------------------------
# 3. The fix is not cosmetic — it repairs an ALREADY-WIDE database, and stays repaired
# --------------------------------------------------------------------------------------


def test_policy_narrows_a_legacy_database_that_was_already_granted_all_privileges(restore_privileges):
    """Every production database that has booted even once already has the wide `arwdDxt` ACL
    durably committed, and a GRANT never removes privileges — so changing the bootstrap
    scripts' GRANT alone would fix new databases and leave every existing one exactly as
    vulnerable. This reproduces that legacy state deliberately (re-issuing the historical
    `GRANT ALL PRIVILEGES ON ALL TABLES`) and asserts the boot-time policy actually REVOKEs
    it back down."""
    policy = _load_policy()
    settings = get_settings()

    admin = psycopg2.connect(settings.database_url)
    admin.autocommit = False
    try:
        with admin.cursor() as cur:
            cur.execute("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO mainai_app")
            admin.commit()

            # Sanity-check the precondition, so a green result can never come from the legacy
            # state having failed to apply in the first place.
            cur.execute("SELECT has_table_privilege('mainai_app', 'public.messages', 'TRUNCATE')")
            assert cur.fetchone()[0] is True, "precondition failed: legacy wide grant did not apply"

            policy.acquire_privilege_boot_lock(cur)
            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()
            errors = policy.apply_privilege_policy(
                cur, expected_owner=expected_owner, require_complete=False
            )
            assert errors == [], f"policy reported errors: {errors}"
            admin.commit()

            for table in USER_DATA_TABLES:
                for priv in policy._NEVER_GRANTED_TABLE_PRIVS:
                    cur.execute("SELECT has_table_privilege('mainai_app', %s, %s)", (f'public."{table}"', priv))
                    assert cur.fetchone()[0] is False, f"{table}.{priv} survived the policy run"
    finally:
        admin.rollback()
        admin.close()


def test_new_tables_do_not_silently_re_acquire_the_removed_privileges(restore_privileges):
    """The half of this fix that is easiest to get wrong, and the reason `_default_acl_privileges()`
    exists. `ALTER DEFAULT PRIVILEGES ... GRANT` is ADDITIVE: re-issuing the historical
    `GRANT ALL PRIVILEGES ON TABLES` as a narrower four-privilege GRANT leaves the stored ACL
    entry at the full `arwdDxt`, so the very next migration that adds a table would hand
    mainai_app TRUNCATE on it again and quietly undo the floor.

    Verified here the only way that actually proves it: set up the legacy default-privilege
    state, run the policy, then CREATE a genuinely new table as the admin/migration role
    (exactly what a future Alembic migration does) and measure what mainai_app really got."""
    policy = _load_policy()
    settings = get_settings()

    admin = psycopg2.connect(settings.database_url)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT current_user")
            (expected_owner,) = cur.fetchone()
            cur.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE "{expected_owner}" IN SCHEMA public '
                "GRANT ALL PRIVILEGES ON TABLES TO mainai_app"
            )

        admin.autocommit = False
        with admin.cursor() as cur:
            policy.acquire_privilege_boot_lock(cur)
            errors = policy.apply_privilege_policy(
                cur, expected_owner=expected_owner, require_complete=False
            )
            assert errors == [], f"policy reported errors: {errors}"
        admin.commit()
        admin.autocommit = True

        with admin.cursor() as cur:
            cur.execute("CREATE TABLE privilege_floor_probe (id integer)")
            try:
                for priv in policy._NEVER_GRANTED_TABLE_PRIVS:
                    cur.execute("SELECT has_table_privilege('mainai_app', 'privilege_floor_probe', %s)", (priv,))
                    assert cur.fetchone()[0] is False, (
                        f"a newly created table still grants mainai_app {priv} automatically — "
                        "the default-privilege ACL was not actually narrowed"
                    )
                # ...and the four it SHOULD still inherit are genuinely still inherited, so
                # this assertion can't pass by the default grant having been wiped entirely.
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cur.execute("SELECT has_table_privilege('mainai_app', 'privilege_floor_probe', %s)", (priv,))
                    assert cur.fetchone()[0] is True, f"new tables no longer inherit {priv}"
            finally:
                cur.execute("DROP TABLE privilege_floor_probe")
    finally:
        admin.close()


# --------------------------------------------------------------------------------------
# 4. The bootstrap scripts themselves must not reintroduce the wide grant
# --------------------------------------------------------------------------------------


def test_no_bootstrap_path_grants_all_privileges_on_tables():
    """A REVOKE in a boot-time policy is worthless if some other boot-time script re-applies
    `GRANT ALL PRIVILEGES ON ALL TABLES` afterwards — this is exactly the "applied once, undone
    by the next restart" class of bug documented in apply_runtime_privileges.py's docstring and
    the Pass 12 boot-persistence incident. These are every place in the repo that provisions
    mainai_app's table privileges; a future edit that reintroduces a table-wide `ALL` in any of
    them fails here rather than silently re-widening production on the next deploy.

    Scoped to TABLES deliberately: `ALL PRIVILEGES ON ALL SEQUENCES` is still correct and still
    present — a sequence's privilege set (USAGE/SELECT/UPDATE) has no TRUNCATE/REFERENCES/
    TRIGGER in it and is genuinely needed for `nextval()`."""
    python_sources = [
        BACKEND_ROOT / "scripts" / "ensure_app_role.py",
        BACKEND_ROOT / "tests" / "conftest.py",
    ]
    # .github/workflows/ci.yml provisions mainai_app itself for the two Playwright E2E jobs
    # (they build their own database rather than reusing the pytest fixture), so it is a real
    # provisioning site and belongs in this guard — a wide grant left there would mean E2E
    # exercises a privilege shape production does not have, which is precisely the gap that
    # lets a TRUNCATE-dependent regression pass CI.
    shell_sources = [
        BACKEND_ROOT / "db-init" / "01-app-role.sh",
        BACKEND_ROOT.parent / ".github" / "workflows" / "ci.yml",
    ]
    patterns = ("ALL PRIVILEGES ON ALL TABLES", "ALL PRIVILEGES ON TABLES")

    offenders = []

    # Python: matched against the AST's string literals with docstrings excluded, rather than
    # raw lines. These modules legitimately DISCUSS the old blanket grant at length in their
    # docstrings (that history is why the current code looks the way it does and is worth
    # keeping); a line-based scan cannot tell that prose apart from a live SQL string, and
    # would force the explanation to be deleted to keep the test green.
    for path in python_sources:
        assert path.exists(), f"expected bootstrap source missing: {path}"
        tree = ast.parse(path.read_text())
        docstring_nodes = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                    if isinstance(body[0].value.value, str):
                        docstring_nodes.add(id(body[0].value))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstring_nodes:
                continue
            if any(p in node.value for p in patterns):
                offenders.append(f"{path.name}:{node.lineno}: {node.value.strip()[:90]}")

    # Shell: no AST available, so ordinary `--`/`#` comment lines are skipped instead.
    for path in shell_sources:
        assert path.exists(), f"expected bootstrap source missing: {path}"
        for lineno, line in enumerate(path.read_text().splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("--"):
                continue
            if any(p in line for p in patterns):
                offenders.append(f"{path.name}:{lineno}: {stripped}")

    assert offenders == [], (
        "a table-wide ALL PRIVILEGES grant to mainai_app was reintroduced: " + "; ".join(offenders)
    )
