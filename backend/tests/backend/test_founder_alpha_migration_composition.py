"""Executable acceptance for the proposed #413 + #412 Founder Alpha composition.

This is verification code, not a migration. Every database is disposable and every source
candidate is bound in the adjacent machine-readable manifest.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import psycopg2
from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.backend.test_migration_0069_lineage_bridge import (
    BACKEND_ROOT,
    HEAD,
    _disposable_database,
    _install_exact_pr237_0069,
    _run_alembic,
)


REPO_ROOT = BACKEND_ROOT.parent
MANIFEST_PATH = REPO_ROOT / "docs" / "founder_alpha_migration_composition_verification.json"


def _script_directory() -> ScriptDirectory:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_composition_manifest_binds_exact_sources_and_one_migration_head():
    manifest = json.loads(MANIFEST_PATH.read_text())
    assert manifest["first_cycle_go"] is False
    assert manifest["sources"] == [
        {
            "pr": 413,
            "sha": "23022662a78abb141792ea7439b715d670cdc640",
            "role": "import_parser_and_lease_lineage",
            "migration_files": [],
        },
        {
            "pr": 412,
            "sha": "bff25dbc398094f453be00479db8027e33b8c3bd",
            "role": "exact_0069_authority_and_lineage",
            "migration_files": ["backend/alembic/versions/0074_0069_lineage_bridge.py"],
        },
    ]
    assert {entry["pr"] for entry in manifest["excluded_exact_0069_collisions"]} == {403, 408, 411}

    script = _script_directory()
    assert script.get_heads() == [HEAD]
    exact_0069 = [revision for revision in script.walk_revisions() if revision.revision == "0069"]
    assert len(exact_0069) == 1
    assert Path(exact_0069[0].path).name == "0069_provider_disclosure_local_decision.py"


def test_existing_canonical_0073_schema_upgrades_replays_privileges_and_round_trips():
    with _disposable_database("lifeai_composed_canonical_0073") as database_url:
        _run_alembic(database_url, "upgrade", "0073_provider_disclosure_local")
        owner_id = uuid.uuid4()
        event_id = uuid.uuid4()
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO users (
                        id, email, password_hash, role, is_active, created_at,
                        email_verified, sessions_valid_after
                    ) VALUES (%s, %s, 'not-a-real-hash', 'founder', true, now(), true, now())
                    """,
                    (str(owner_id), f"composition-{owner_id}@example.invalid"),
                )
                cursor.execute(
                    """
                    INSERT INTO provider_disclosure_events (
                        id, owner_id, provider, model, purpose, requested_by, decision,
                        reason, redaction_categories, attempted_content_hash, sent_content_hash
                    ) VALUES (%s, %s, 'ollama', 'synthetic', 'composition', 'test', 'local',
                              'preserve', '[]'::jsonb, %s, %s)
                    """,
                    (str(event_id), str(owner_id), "a" * 64, "b" * 64),
                )

        _run_alembic(database_url, "upgrade", "head")
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mainai_app"
                )
        policy = subprocess.run(
            [sys.executable, str(BACKEND_ROOT / "scripts" / "security" / "apply_runtime_privileges.py")],
            cwd=BACKEND_ROOT,
            env={**os.environ, "DATABASE_URL": database_url},
            text=True,
            capture_output=True,
            check=False,
        )
        assert policy.returncode == 0, policy.stderr or policy.stdout

        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchall() == [(HEAD,)]
                cursor.execute("SELECT source_lineage FROM _alembic_0074_0069_lineage")
                assert cursor.fetchone() == ("founder_alpha_provider_disclosure",)
                cursor.execute(
                    "SELECT decision, reason FROM provider_disclosure_events WHERE id = %s",
                    (str(event_id),),
                )
                assert cursor.fetchone() == ("local", "preserve")
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    cursor.execute(
                        "SELECT has_table_privilege('mainai_app', %s, %s)",
                        ("public._alembic_0074_0069_lineage", privilege),
                    )
                    assert cursor.fetchone() == (False,)

        _run_alembic(database_url, "downgrade", "0073_provider_disclosure_local")
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT decision, reason FROM provider_disclosure_events WHERE id = %s", (str(event_id),))
                assert cursor.fetchone() == ("local", "preserve")
        _run_alembic(database_url, "upgrade", "head")


def test_genuine_pr237_database_composes_to_head_without_data_loss():
    with _disposable_database("lifeai_composed_pr237") as database_url:
        owner_id, event_id = _install_exact_pr237_0069(database_url)
        _run_alembic(database_url, "upgrade", "head")
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchall() == [(HEAD,)]
                cursor.execute("SELECT source_lineage FROM _alembic_0074_0069_lineage")
                assert cursor.fetchone() == ("pr237_mainai_stop_controls",)
                cursor.execute(
                    "SELECT active, reason, sequence FROM mainai_stop_state WHERE owner_id = %s",
                    (str(owner_id),),
                )
                assert cursor.fetchone() == (True, "preserve-me", 7)
                cursor.execute("SELECT reason FROM mainai_stop_events WHERE id = %s", (str(event_id),))
                assert cursor.fetchone() == ("preserve-me",)

        refused = _run_alembic(
            database_url,
            "downgrade",
            "0073_provider_disclosure_local",
            check=False,
        )
        assert refused.returncode != 0
        assert "cannot downgrade bridged PR #237 history" in (refused.stdout + refused.stderr)


def test_forged_history_failure_is_atomic_then_known_schema_can_recover():
    with _disposable_database("lifeai_composed_forged_recovery") as database_url:
        _run_alembic(database_url, "upgrade", "0069")
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    ALTER TABLE provider_disclosure_events
                        DROP CONSTRAINT ck_provider_disclosure_events_decision,
                        ADD CONSTRAINT ck_provider_disclosure_events_decision
                            CHECK (decision IN ('allowed', 'denied', 'local', 'forged'));
                    """
                )

        refused = _run_alembic(database_url, "upgrade", "head", check=False)
        assert refused.returncode != 0
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchall() == [("0069",)]
                cursor.execute("SELECT to_regclass('_alembic_0074_0069_lineage')")
                assert cursor.fetchone() == (None,)
                cursor.execute(
                    "SELECT count(*) FROM audit_log WHERE action = 'migration_bridge_0069_pr237'"
                )
                assert cursor.fetchone() == (0,)
                cursor.execute(
                    """
                    ALTER TABLE provider_disclosure_events
                        DROP CONSTRAINT ck_provider_disclosure_events_decision,
                        ADD CONSTRAINT ck_provider_disclosure_events_decision
                            CHECK (decision IN ('allowed', 'denied', 'local'));
                    """
                )

        _run_alembic(database_url, "upgrade", "head")
        with psycopg2.connect(database_url) as conn:
            with conn.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchall() == [(HEAD,)]

