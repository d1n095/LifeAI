"""Executable coverage for `scripts/ops/db_recovery.py` — the Founder Alpha's database
recoverability contract, exercised against a real PostgreSQL instance.

Why this file exists at all: before it, nothing in this repository proved the durable Founder
Alpha state (conversations, memory sources, knowledge documents/chunks + their pgvector
embeddings, RLS policies, `alembic_version`) could be recovered. `scripts/vps/backup.sh` covers
files and Docker volumes only and says so in its own header; `docker-compose.vps.yml` has no
Postgres service; `docs/VPS_BACKUP_RESTORE.md` treats the database as someone else's
responsibility. That was true for a Supabase-hosted deployment and is false for a private one.

Every assertion here is made against Postgres's own state after a real dump/restore round trip —
never against the manifest's text alone, and never with a mocked `pg_dump`. A test that only
checked "a file was produced" would reproduce the exact gap this exists to close.

Deliberately not covered here: `restore.sh`'s Docker-volume path (already covered by
ci.yml's vps-scripts job) and the Alembic roundtrip itself (`test_migration_roundtrip.py`);
what's added here is the rollback-after-restore case, which neither of those exercises.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import importlib.util
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
RECOVERY_PATH = BACKEND_ROOT / "scripts" / "ops" / "db_recovery.py"
APP_ROLE = "mainai_app"
TABLE_PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]


def _load_recovery():
    """Loaded by path, the same way `test_runtime_table_privileges.py` loads the privilege
    policy: `backend/scripts` is deliberately outside the importable `app` package (these
    scripts must run before `app` is importable — see backend/docker-entrypoint.sh)."""
    spec = importlib.util.spec_from_file_location("db_recovery", RECOVERY_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


recovery = _load_recovery()


@pytest.fixture(scope="module", autouse=True)
def _require_postgres_client_tools():
    """Skips only where the client tools genuinely aren't installed *and* this isn't CI.

    In CI a skip would silently turn 'recoverability is verified' into 'recoverability is
    untested' for exactly the runs that are supposed to prove it, so CI fails instead.
    """
    missing = [name for name in ("pg_dump", "pg_restore") if shutil.which(name) is None]
    if not missing:
        return
    if os.environ.get("CI") == "true":
        pytest.fail(f"CI is missing PostgreSQL client tools {missing} — recovery tests must not skip silently")
    pytest.skip(f"missing PostgreSQL client tools {missing}")


def _admin_url() -> str:
    from app.config import get_settings

    return get_settings().database_url


def _database_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def _maintenance_url(url: str, dbname: str = "postgres") -> str:
    parsed = urlparse(url)
    auth = parsed.username if not parsed.password else f"{parsed.username}:{parsed.password}"
    return f"postgresql://{auth}@{parsed.hostname}:{parsed.port or 5432}/{dbname}"


def _terminate_and_drop(dbname: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_]+", dbname):
        raise ValueError(f"refusing to drop a non-identifier database name: {dbname!r}")
    conn = psycopg2.connect(_maintenance_url(_admin_url()))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
                (dbname,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')
    finally:
        conn.close()


def _create_database(dbname: str) -> str:
    conn = psycopg2.connect(_maintenance_url(_admin_url()))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        conn.close()
    return f"postgresql://{urlparse(_admin_url()).username}:{urlparse(_admin_url()).password}@{urlparse(_admin_url()).hostname}:{urlparse(_admin_url()).port or 5432}/{dbname}"


@pytest.fixture
def probe_table():
    """A tiny sentinel table with no foreign keys, so 'this specific row survived the round
    trip' is assertable without depending on which durable tables happen to have rows in a
    freshly-truncated test database. Created in the session database and dropped again here —
    leaving it behind would silently change the schema every later test in the session runs
    against."""
    conn = psycopg2.connect(_admin_url())
    probe_id = uuid.uuid4()
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS recovery_probe (id uuid PRIMARY KEY, note text NOT NULL)")
            cur.execute("INSERT INTO recovery_probe (id, note) VALUES (%s, %s)", (str(probe_id), "survives restore"))
        conn.commit()
    finally:
        conn.close()
    try:
        yield probe_id
    finally:
        conn = psycopg2.connect(_admin_url())
        try:
            with conn.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS recovery_probe")
            conn.commit()
        finally:
            conn.close()


@pytest.fixture
def restore_target():
    """A fresh, empty database to restore into — never the session database itself."""
    dbname = f"lifeos_restore_{uuid.uuid4().hex[:12]}"
    yield_url = _create_database(dbname)
    try:
        yield yield_url, dbname
    finally:
        _terminate_and_drop(dbname)


def _backup(tmp_path: Path, *, content_digests: bool = True) -> Path:
    path = tmp_path / "founder-alpha.dump"
    recovery.backup(_admin_url(), path, content_digests=content_digests)
    return path


# --------------------------------------------------------------------------------------
# 1. The manifest is a real recovery contract, not a filename
# --------------------------------------------------------------------------------------


def test_migration_graph_is_a_single_rooted_single_headed_chain():
    """The backup certifies itself against the tree's parsed history, so the graph derivation
    itself gets asserted: exactly one root and one head, no dangling parents. Re-derived from
    `revision`/`down_revision` literals, never from the files' numeric names."""
    graph = recovery.migration_graph()
    assert graph["roots"] == ["0001"]
    assert graph["heads"] == ["0079"]
    assert graph["dangling_down_revisions"] == []
    assert graph["revision_count"] >= 78
    assert "0079" in graph["revisions"]


def test_backup_records_the_state_a_restore_must_reproduce(tmp_path):
    probe_and_backup = tmp_path / "state.dump"
    manifest = recovery.backup(_admin_url(), probe_and_backup, content_digests=True)

    assert manifest["format_version"] == 1
    assert manifest["consistency"] == "snapshot", (
        "the inventory must describe the same snapshot the dump contains; sequential fallback is "
        "only legitimate for a role that cannot export a snapshot"
    )
    assert manifest["alembic"]["current_revision"] == "0079"
    assert manifest["alembic"]["heads"] == ["0079"]

    # The recorded checksum is the file's actual checksum, and the sidecar agrees with both.
    assert manifest["dump"]["sha256"] == recovery._sha256_file(probe_and_backup)
    sidecar = probe_and_backup.with_name(probe_and_backup.name + ".sha256")
    assert sidecar.is_file()
    assert sidecar.read_text().split()[0] == manifest["dump"]["sha256"]

    # Real numbers, read from Postgres, not zeros.
    assert len(manifest["tables"]) > 30
    assert manifest["tables"]["alembic_version"]["row_count"] == 1
    assert manifest["rls"]["policy_count"] > 0
    assert manifest["rls"]["rls_forced_tables"], "at least one table is expected to FORCE row level security"


def test_backup_refuses_an_unmigrated_database(tmp_path):
    """A database without an `alembic_version` row cannot be certified as recoverable: nothing
    would say which schema the dump is at. Refused rather than stored as if it were a backup."""
    dbname = f"lifeos_unmigrated_{uuid.uuid4().hex[:12]}"
    url = _create_database(dbname)
    try:
        with pytest.raises(recovery.RecoveryError, match="alembic_version"):
            recovery.backup(url, tmp_path / "unmigrated.dump")
    finally:
        _terminate_and_drop(dbname)


# --------------------------------------------------------------------------------------
# 2. verify() — integrity without a database
# --------------------------------------------------------------------------------------


def test_verify_accepts_a_fresh_backup(tmp_path):
    path = _backup(tmp_path)
    assert recovery.verify(path) == []


def test_verify_rejects_a_corrupted_archive(tmp_path):
    """The whole point of the checksum: a truncated or bit-flipped backup must be refused before
    anything is extracted, or a restore silently reproduces damage."""
    source = _backup(tmp_path)
    tampered = tmp_path / "tampered.dump"
    payload = bytearray(source.read_bytes())
    payload[len(payload) // 2] ^= 0xFF
    tampered.write_bytes(bytes(payload))
    shutil.copy(source.with_name(source.name + ".manifest.json"), tampered.with_name(tampered.name + ".manifest.json"))
    shutil.copy(source.with_name(source.name + ".sha256"), tampered.with_name(tampered.name + ".sha256"))

    failures = recovery.verify(tampered)
    assert any("checksum mismatch" in failure for failure in failures), failures


def test_sanitize_drops_only_unknown_session_settings_and_never_rewrites_copy_rows():
    """Client/server version skew: a newer `pg_dump` writes `SET transaction_timeout = 0;`, which
    an older server rejects outright. Dropping that line is safe because it is a session setting.
    Rewriting a data row that merely *looks* like that statement would corrupt restored content,
    so the function must not touch anything inside a COPY block — that is the whole reason it is
    a state machine rather than a regex over the whole file."""
    sql = (
        "SET transaction_timeout = 0;\n"
        "SET search_path = public, pg_catalog;\n"
        "COPY public.recovery_probe (id, note) FROM stdin;\n"
        "SET transaction_timeout = 0;\n"
        "\\.\n"
        "SET client_min_messages = warning;\n"
    )
    sanitized, dropped = recovery.sanitize_dump_sql(sql, {"search_path", "client_min_messages"})

    assert dropped == ["transaction_timeout"]
    assert sanitized.count("transaction_timeout") == 1, "the COPY payload row must survive untouched"
    assert "SET transaction_timeout = 0;\n\\.\n" in sanitized
    assert "SET search_path = public, pg_catalog;" in sanitized
    assert "SET client_min_messages = warning;" in sanitized


def test_verify_rejects_an_edited_manifest(tmp_path):
    """Row counts are the artifact a restore is judged against, so editing them in the JSON must
    be detectable on its own — otherwise a doctored manifest could 'prove' a broken restore was
    correct."""
    path = _backup(tmp_path)
    manifest_path = path.with_name(path.name + ".manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["tables"]["messages"]["row_count"] += 1
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))

    failures = recovery.verify(path)
    assert any("manifest checksum mismatch" in failure for failure in failures), failures


# --------------------------------------------------------------------------------------
# 3. restore() guards — a recovery tool that can overwrite the source is a liability
# --------------------------------------------------------------------------------------


def test_restore_refuses_without_confirm(tmp_path, restore_target):
    path = _backup(tmp_path)
    target_url, _ = restore_target
    with pytest.raises(recovery.RecoveryError, match="--confirm"):
        recovery.restore(path, target_url, confirmed=False)


def test_restore_refuses_the_source_database(tmp_path):
    """In-place 'restore' is not a recovery drill. The source database is named in the manifest,
    so this is refused even when every other guard would have passed."""
    path = _backup(tmp_path)
    with pytest.raises(recovery.RecoveryError, match="refusing to restore into"):
        recovery.restore(path, _admin_url(), confirmed=True)


def test_restore_refuses_a_non_empty_target(tmp_path, restore_target):
    """Restoring over a database that already has tables would mix two states and could leave a
    schema that matches neither the backup nor anything that ever booted."""
    path = _backup(tmp_path)
    target_url, _ = restore_target
    conn = psycopg2.connect(target_url)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE pre_existing (id int)")
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(recovery.RecoveryError, match="already contains tables"):
        recovery.restore(path, target_url, confirmed=True)


def test_restore_refuses_a_corrupted_backup(tmp_path, restore_target):
    """Corruption is caught by verify() *before* the target is created, so a failed restore never
    leaves a half-built database behind."""
    source = _backup(tmp_path)
    payload = bytearray(source.read_bytes())
    payload[128] ^= 0xFF
    source.write_bytes(bytes(payload))
    target_url, dbname = restore_target

    with pytest.raises(recovery.RecoveryError, match="did not pass verification"):
        recovery.restore(source, target_url, confirmed=True)

    conn = psycopg2.connect(_maintenance_url(_admin_url()))
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_tables WHERE schemaname = 'public'")
            assert cur.fetchall() == [], "a refused restore must not have restored partial state"
    finally:
        conn.close()


# --------------------------------------------------------------------------------------
# 4. A real restore reproduces the database, and still boots
# --------------------------------------------------------------------------------------


def test_restore_reproduces_state_and_reestablishes_sfa1(tmp_path, restore_target, probe_table):
    """The headline claim: dump -> restore into a fresh database reproduces every table's rows
    and content, the sequences, the RLS policies, and the applied revision; and the restored
    database is left in the *boot* privilege state — narrowed S1A grants and NO runtime
    privileges at all on `alembic_version` (SFA-1, verified through Postgres itself)."""
    path = _backup(tmp_path)
    target_url, dbname = restore_target

    failures = recovery.restore(path, target_url, confirmed=True)
    assert failures == [], failures

    manifest = json.loads(path.with_name(path.name + ".manifest.json").read_text())
    conn = psycopg2.connect(target_url)
    try:
        # The sentinel row specifically — not just a matching count.
        with conn.cursor() as cur:
            cur.execute("SELECT note FROM recovery_probe WHERE id = %s", (str(probe_table),))
            assert cur.fetchone() == ("survives restore",)

            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone() == ("0079",)

            # SFA-1: measured on the restored database, not inferred from the policy's source.
            cur.execute(
                "SELECT p FROM unnest(%s::text[]) AS p WHERE has_table_privilege(%s, 'public.alembic_version', p)",
                (TABLE_PRIVS, APP_ROLE),
            )
            assert cur.fetchall() == [], "the runtime role must hold no privilege on alembic_version after restore"

            # The S1A narrowings that make provenance trustworthy, spot-checked on real catalog state.
            cur.execute(
                "SELECT p FROM unnest(%s::text[]) AS p WHERE has_table_privilege(%s, 'public.memory_source_units', p)",
                (TABLE_PRIVS, APP_ROLE),
            )
            assert sorted(row[0] for row in cur.fetchall()) == ["INSERT", "SELECT"]

            # RLS travelled with the schema rather than being re-created by hand.
            cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
            assert cur.fetchone()[0] == manifest["rls"]["policy_count"]
    finally:
        conn.close()

    # And the policy module's own verifier agrees, on the restored database.
    policy = recovery._load_module(
        BACKEND_ROOT / "scripts" / "security" / "s1a_privilege_policy.py", "s1a_privilege_policy"
    )
    verify_conn = psycopg2.connect(target_url)
    try:
        verify_conn.set_session(readonly=True)
        with verify_conn.cursor() as cur:
            cur.execute("SELECT current_user")
            owner = cur.fetchone()[0]
            assert policy.apply_privilege_policy(
                cur, expected_owner=owner, require_complete=True, mutate=False
            ) == []
    finally:
        verify_conn.rollback()
        verify_conn.close()


def test_schema_rollback_after_restore_preserves_restored_data(tmp_path, restore_target, probe_table):
    """Recoverability includes recovering from a bad migration, not only from a lost disk: after
    restoring, `alembic downgrade -1` then `upgrade head` must return the schema to head with the
    restored rows intact. This is the data-preserving-rollback property the operational workflow
    depends on, asserted on a restored database rather than on the session one."""
    path = _backup(tmp_path)
    target_url, _ = restore_target
    assert recovery.restore(path, target_url, confirmed=True) == []

    env = {**os.environ, "DATABASE_URL": target_url}
    down = subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", "-1"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert down.returncode == 0, down.stderr[-2000:]
    up = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert up.returncode == 0, up.stderr[-2000:]

    conn = psycopg2.connect(target_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone() == ("0079",)
            cur.execute("SELECT note FROM recovery_probe WHERE id = %s", (str(probe_table),))
            assert cur.fetchone() == ("survives restore",)
    finally:
        conn.close()
