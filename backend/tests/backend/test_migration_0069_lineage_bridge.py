"""Executable PostgreSQL coverage for the two incompatible exact-0069 histories."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2


BACKEND_ROOT = Path(__file__).resolve().parents[2]
HEAD = "0074_0069_lineage_bridge"


def _run_alembic(database_url: str, *args: str, check: bool = True):
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        text=True,
        capture_output=True,
        check=check,
    )


@contextmanager
def _disposable_database(prefix: str):
    configured = urlparse(os.environ["DATABASE_URL"])
    name = f"{prefix}_{uuid.uuid4().hex}"
    admin_url = urlunparse(configured._replace(path="/postgres"))
    database_url = urlunparse(configured._replace(path=f"/{name}"))
    admin = psycopg2.connect(admin_url)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE DATABASE "{name}"')
        yield database_url
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()",
                (name,),
            )
            cursor.execute(f'DROP DATABASE IF EXISTS "{name}"')
        admin.close()


def _install_exact_pr237_0069(database_url: str) -> tuple[uuid.UUID, uuid.UUID]:
    _run_alembic(database_url, "upgrade", "0068")
    owner_id = uuid.uuid4()
    event_id = uuid.uuid4()
    with psycopg2.connect(database_url) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE mainai_stop_state (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    scope varchar(16) NOT NULL,
                    owner_id uuid REFERENCES users(id) ON DELETE CASCADE,
                    active boolean NOT NULL DEFAULT false,
                    reason text NOT NULL DEFAULT '',
                    sequence bigint NOT NULL DEFAULT 0,
                    updated_at timestamp NOT NULL DEFAULT now(),
                    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
                    CONSTRAINT ck_mainai_stop_scope CHECK (scope IN ('owner', 'global')),
                    CONSTRAINT ck_mainai_stop_owner_null CHECK (
                        (scope = 'global' AND owner_id IS NULL)
                        OR (scope = 'owner' AND owner_id IS NOT NULL)
                    ),
                    CONSTRAINT ck_mainai_stop_seq CHECK (sequence >= 0),
                    CONSTRAINT ck_mainai_stop_prov CHECK (jsonb_typeof(provenance) = 'object')
                );
                CREATE UNIQUE INDEX uq_mainai_stop_global
                    ON mainai_stop_state ((scope)) WHERE scope = 'global';
                CREATE UNIQUE INDEX uq_mainai_stop_owner
                    ON mainai_stop_state (owner_id) WHERE scope = 'owner';

                CREATE TABLE mainai_stop_events (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                    scope varchar(16) NOT NULL,
                    owner_id uuid,
                    event_kind varchar(32) NOT NULL,
                    sequence bigint NOT NULL,
                    reason text NOT NULL,
                    founder_ack text,
                    clear_request_id uuid,
                    actor_kind varchar(32) NOT NULL,
                    provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
                    created_at timestamp NOT NULL DEFAULT now(),
                    CONSTRAINT ck_mainai_stop_ev_scope CHECK (scope IN ('owner', 'global')),
                    CONSTRAINT ck_mainai_stop_ev_kind CHECK (
                        event_kind IN ('activate', 'clear', 'reject_clear', 'boot_blocked')
                    ),
                    CONSTRAINT ck_mainai_stop_ev_actor CHECK (
                        actor_kind IN ('founder', 'operator', 'system')
                    ),
                    CONSTRAINT ck_mainai_stop_ev_prov CHECK (jsonb_typeof(provenance) = 'object')
                );
                CREATE UNIQUE INDEX uq_mainai_stop_clear_request
                    ON mainai_stop_events (clear_request_id)
                    WHERE clear_request_id IS NOT NULL;
                CREATE INDEX ix_mainai_stop_events_owner
                    ON mainai_stop_events (owner_id, created_at DESC);
                INSERT INTO mainai_stop_state (scope, owner_id, active, reason, sequence)
                VALUES ('global', NULL, false, '', 0);
                """
            )
            cursor.execute(
                """
                INSERT INTO users (
                    id, email, password_hash, role, is_active, created_at,
                    email_verified, sessions_valid_after
                ) VALUES (%s, %s, 'not-a-real-hash', 'founder', true, now(), true, now())
                """,
                (str(owner_id), f"pr237-{owner_id}@example.invalid"),
            )
            cursor.execute(
                """
                INSERT INTO mainai_stop_state (
                    scope, owner_id, active, reason, sequence, provenance
                ) VALUES ('owner', %s, true, 'preserve-me', 7, '{"source":"pr237"}')
                """,
                (str(owner_id),),
            )
            cursor.execute(
                """
                INSERT INTO mainai_stop_events (
                    id, scope, owner_id, event_kind, sequence, reason, actor_kind, provenance
                ) VALUES (%s, 'owner', %s, 'activate', 7, 'preserve-me', 'founder',
                          '{"source":"pr237"}')
                """,
                (str(event_id), str(owner_id)),
            )
            cursor.execute("UPDATE alembic_version SET version_num = '0069'")
    return owner_id, event_id


def test_exact_pr237_history_upgrades_without_relabelling_or_data_loss():
    with _disposable_database("lifeai_pr237_bridge") as database_url:
        owner_id, event_id = _install_exact_pr237_0069(database_url)

        _run_alembic(database_url, "upgrade", "head")
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchall() == [(HEAD,)]
                cursor.execute(
                    "SELECT active, reason, sequence, provenance FROM mainai_stop_state "
                    "WHERE owner_id = %s",
                    (str(owner_id),),
                )
                assert cursor.fetchone() == (
                    True,
                    "preserve-me",
                    7,
                    {"source": "pr237"},
                )
                cursor.execute(
                    "SELECT event_kind, reason, sequence FROM mainai_stop_events WHERE id = %s",
                    (str(event_id),),
                )
                assert cursor.fetchone() == ("activate", "preserve-me", 7)
                cursor.execute(
                    "SELECT source_lineage FROM _alembic_0074_0069_lineage"
                )
                assert cursor.fetchone() == ("pr237_mainai_stop_controls",)
                cursor.execute(
                    "SELECT count(*) FROM audit_log "
                    "WHERE action = 'migration_bridge_0069_pr237'"
                )
                assert cursor.fetchone() == (1,)
                cursor.execute(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'provider_disclosure_events'::regclass "
                    "AND conname = 'ck_provider_disclosure_events_decision'"
                )
                assert "local" in cursor.fetchone()[0]
                cursor.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'document_chunks' "
                    "AND column_name IN ('embedding_model', 'embedding_native_dim') "
                    "ORDER BY column_name"
                )
                assert cursor.fetchall() == [
                    ("embedding_model",),
                    ("embedding_native_dim",),
                ]
                cursor.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = 'mainai_app'"
                )
                if cursor.fetchone() is not None:
                    cursor.execute(
                        "SELECT has_table_privilege('mainai_app', 'mainai_stop_state', 'UPDATE'), "
                        "has_table_privilege('mainai_app', 'mainai_stop_events', 'DELETE')"
                    )
                    assert cursor.fetchone() == (False, False)

        refused = _run_alembic(database_url, "downgrade", "0073_provider_disclosure_local", check=False)
        assert refused.returncode != 0
        assert "cannot downgrade bridged PR #237 history" in (refused.stderr + refused.stdout)
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchall() == [(HEAD,)]
                cursor.execute("SELECT reason FROM mainai_stop_state WHERE owner_id = %s", (str(owner_id),))
                assert cursor.fetchone() == ("preserve-me",)


def test_ambiguous_combined_0069_effects_fail_closed():
    with _disposable_database("lifeai_ambiguous_0069") as database_url:
        _run_alembic(database_url, "upgrade", "0069")
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE mainai_stop_state (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                        scope varchar(16) NOT NULL,
                        owner_id uuid REFERENCES users(id) ON DELETE CASCADE,
                        active boolean NOT NULL DEFAULT false,
                        reason text NOT NULL DEFAULT '',
                        sequence bigint NOT NULL DEFAULT 0,
                        updated_at timestamp NOT NULL DEFAULT now(),
                        provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
                        CONSTRAINT ck_mainai_stop_scope CHECK (scope IN ('owner', 'global')),
                        CONSTRAINT ck_mainai_stop_owner_null CHECK (
                            (scope = 'global' AND owner_id IS NULL)
                            OR (scope = 'owner' AND owner_id IS NOT NULL)
                        ),
                        CONSTRAINT ck_mainai_stop_seq CHECK (sequence >= 0),
                        CONSTRAINT ck_mainai_stop_prov CHECK (jsonb_typeof(provenance) = 'object')
                    );
                    CREATE UNIQUE INDEX uq_mainai_stop_global
                        ON mainai_stop_state ((scope)) WHERE scope = 'global';
                    CREATE UNIQUE INDEX uq_mainai_stop_owner
                        ON mainai_stop_state (owner_id) WHERE scope = 'owner';
                    CREATE TABLE mainai_stop_events (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                        scope varchar(16) NOT NULL,
                        owner_id uuid,
                        event_kind varchar(32) NOT NULL,
                        sequence bigint NOT NULL,
                        reason text NOT NULL,
                        founder_ack text,
                        clear_request_id uuid,
                        actor_kind varchar(32) NOT NULL,
                        provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
                        created_at timestamp NOT NULL DEFAULT now(),
                        CONSTRAINT ck_mainai_stop_ev_scope CHECK (scope IN ('owner', 'global')),
                        CONSTRAINT ck_mainai_stop_ev_kind CHECK (
                            event_kind IN ('activate', 'clear', 'reject_clear', 'boot_blocked')
                        ),
                        CONSTRAINT ck_mainai_stop_ev_actor CHECK (
                            actor_kind IN ('founder', 'operator', 'system')
                        ),
                        CONSTRAINT ck_mainai_stop_ev_prov CHECK (jsonb_typeof(provenance) = 'object')
                    );
                    CREATE UNIQUE INDEX uq_mainai_stop_clear_request
                        ON mainai_stop_events (clear_request_id)
                        WHERE clear_request_id IS NOT NULL;
                    CREATE INDEX ix_mainai_stop_events_owner
                        ON mainai_stop_events (owner_id, created_at DESC);
                    """
                )

        result = _run_alembic(database_url, "upgrade", "head", check=False)
        assert result.returncode != 0
        assert "ambiguous exact-0069 schema effects" in (result.stderr + result.stdout)
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchone()[0] == "0073_provider_disclosure_local"
                cursor.execute("SELECT to_regclass('_alembic_0074_0069_lineage')")
                assert cursor.fetchone() == (None,)
