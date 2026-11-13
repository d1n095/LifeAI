"""PostgreSQL regressions for exact embedding provenance and dual-0069 recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2

BACKEND_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_SCRIPT = BACKEND_ROOT / "scripts" / "migrations" / "recover_0069_dual_version.py"
ANCESTOR = "0069"
DESCENDANT = "0069_embedding_space_identity"


def _run_alembic(database_url: str, *args: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        check=True,
    )


def _run_recovery(database_url: str, *args: str, expected_returncode: int = 0) -> dict:
    result = subprocess.run(
        [sys.executable, str(RECOVERY_SCRIPT), "--database-url", database_url, *args],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == expected_returncode, result.stderr or result.stdout
    return json.loads(result.stdout)


@contextmanager
def _disposable_database(prefix: str):
    configured = urlparse(os.environ["DATABASE_URL"])
    database_name = f"{prefix}_{uuid.uuid4().hex}"
    admin_url = urlunparse(configured._replace(path="/postgres"))
    database_url = urlunparse(configured._replace(path=f"/{database_name}"))
    admin = psycopg2.connect(admin_url)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE "{database_name}"')
        yield database_url
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (database_name,),
            )
            cursor.execute(f'DROP DATABASE IF EXISTS "{database_name}"')
        admin.close()


def _seed_chunks(database_url: str, *, partial: bool = False) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    owner_id = uuid.uuid4()
    document_id = uuid.uuid4()
    known_chunk_id = uuid.uuid4()
    legacy_chunk_id = uuid.uuid4()
    zero_vector = "[" + ",".join(["0"] * 1536) + "]"
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, role, is_active, created_at,
                    email_verified, sessions_valid_after
                ) VALUES (%s, %s, %s, 'founder', true, now(), true, now())
                """,
                (str(owner_id), f"migration-{owner_id}@example.invalid", "not-a-real-hash"),
            )
            cursor.execute(
                """
                INSERT INTO documents (
                    id, uploaded_by, title, source, status, chunk_count, created_at, updated_at
                ) VALUES (%s, %s, 'migration provenance fixture', 'upload', 'indexed', 2, now(), now())
                """,
                (str(document_id), str(owner_id)),
            )
            cursor.execute(
                """
                INSERT INTO document_chunks (
                    id, document_id, owner_id, chunk_index, text, embedding, created_at,
                    embedding_model, embedding_native_dim
                ) VALUES (%s, %s, %s, 0, 'known provenance', %s::vector, now(), %s, %s)
                """,
                (
                    str(known_chunk_id),
                    str(document_id),
                    str(owner_id),
                    zero_vector,
                    "nomic-embed-text:v1.5",
                    None if partial else 768,
                ),
            )
            cursor.execute(
                """
                INSERT INTO document_chunks (
                    id, document_id, owner_id, chunk_index, text, embedding, created_at,
                    embedding_model, embedding_native_dim
                ) VALUES (%s, %s, %s, 1, 'legacy unknown provenance', %s::vector, now(), NULL, NULL)
                """,
                (str(legacy_chunk_id), str(document_id), str(owner_id), zero_vector),
            )
    return known_chunk_id, legacy_chunk_id, owner_id


def _make_dual_version(database_url: str) -> None:
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO alembic_version (version_num) VALUES (%s)", (ANCESTOR,))


def test_non_null_and_null_provenance_values_survive_downgrade_reupgrade_exactly():
    with _disposable_database("lifeai_provenance_roundtrip") as database_url:
        _run_alembic(database_url, "upgrade", "head")
        known_id, legacy_id, _ = _seed_chunks(database_url)

        _run_alembic(database_url, "downgrade", ANCESTOR)
        with psycopg2.connect(database_url) as downgraded:
            with downgraded.cursor() as cursor:
                cursor.execute(
                    "SELECT document_chunk_id, embedding_model, embedding_native_dim "
                    "FROM _alembic_0069_embedding_space_identity_backup ORDER BY document_chunk_id"
                )
                assert cursor.fetchall() == [(known_id, "nomic-embed-text:v1.5", 768)]
                cursor.execute(
                    """
                    SELECT count(*) FROM information_schema.columns
                     WHERE table_schema = 'public' AND table_name = 'document_chunks'
                       AND column_name IN ('embedding_model', 'embedding_native_dim')
                    """
                )
                assert cursor.fetchone() == (0,)

        _run_alembic(database_url, "upgrade", "head")
        with psycopg2.connect(database_url) as upgraded:
            with upgraded.cursor() as cursor:
                cursor.execute(
                    "SELECT id, embedding_model, embedding_native_dim FROM document_chunks "
                    "WHERE id IN (%s, %s) ORDER BY id",
                    (str(known_id), str(legacy_id)),
                )
                assert sorted(cursor.fetchall()) == sorted(
                    [
                        (known_id, "nomic-embed-text:v1.5", 768),
                        (legacy_id, None, None),
                    ]
                )
                cursor.execute("SELECT to_regclass('_alembic_0069_embedding_space_identity_backup')")
                assert cursor.fetchone() == (None,)


def test_dual_version_recovery_is_dry_run_then_atomic_audited_and_idempotent():
    with _disposable_database("lifeai_dual_0069_recovery") as database_url:
        _run_alembic(database_url, "upgrade", DESCENDANT)
        known_id, legacy_id, _ = _seed_chunks(database_url)
        _make_dual_version(database_url)

        analysis = _run_recovery(database_url)
        assert analysis["status"] == "repairable"
        assert analysis["applied"] is False

        repaired = _run_recovery(database_url, "--apply")
        assert repaired["status"] == "repaired"
        assert repaired["applied"] is True
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchall() == [(DESCENDANT,)]
                cursor.execute(
                    "SELECT id, embedding_model, embedding_native_dim FROM document_chunks "
                    "WHERE id IN (%s, %s) ORDER BY id",
                    (str(known_id), str(legacy_id)),
                )
                assert sorted(cursor.fetchall()) == sorted(
                    [
                        (known_id, "nomic-embed-text:v1.5", 768),
                        (legacy_id, None, None),
                    ]
                )
                cursor.execute(
                    "SELECT detail FROM audit_log "
                    "WHERE action = 'migration_recovery_0069_dual_version'"
                )
                receipts = cursor.fetchall()
                assert len(receipts) == 1
                detail = json.loads(receipts[0][0])
                assert detail["before_versions"] == [ANCESTOR, DESCENDANT]
                assert detail["after_versions"] == [DESCENDANT]

        replay = _run_recovery(database_url, "--apply")
        assert replay["status"] == "already_recovered"
        assert replay["applied"] is False
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM audit_log "
                    "WHERE action = 'migration_recovery_0069_dual_version'"
                )
                assert cursor.fetchone() == (1,)


def test_recovery_refuses_partial_provenance_without_mutating_versions_or_audit():
    with _disposable_database("lifeai_dual_0069_partial") as database_url:
        _run_alembic(database_url, "upgrade", DESCENDANT)
        _seed_chunks(database_url, partial=True)
        _make_dual_version(database_url)

        refused = _run_recovery(database_url, "--apply", expected_returncode=2)
        assert refused["status"] == "refused"
        assert "partial embedding provenance" in refused["reason"]
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
                assert cursor.fetchall() == [(ANCESTOR,), (DESCENDANT,)]
                cursor.execute(
                    "SELECT count(*) FROM audit_log "
                    "WHERE action = 'migration_recovery_0069_dual_version'"
                )
                assert cursor.fetchone() == (0,)


def test_recovery_refuses_pr237_exact_0069_semantics_as_unsupported():
    with _disposable_database("lifeai_pr237_0069_collision") as database_url:
        _run_alembic(database_url, "upgrade", "0068")
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE mainai_stop_state (
                        scope text PRIMARY KEY,
                        active boolean NOT NULL,
                        sequence bigint NOT NULL
                    )
                    """
                )
                cursor.execute("UPDATE alembic_version SET version_num = %s", (ANCESTOR,))

        refused = _run_recovery(database_url, "--apply", expected_returncode=2)
        assert refused["status"] == "refused"
        assert "unsupported Alembic state" in refused["reason"]
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchall() == [(ANCESTOR,)]
                cursor.execute("SELECT to_regclass('mainai_stop_state')")
                assert cursor.fetchone()[0] == "mainai_stop_state"
