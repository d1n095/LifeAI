"""Regression for a database created by the standalone #326 migration."""

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import inspect, text

from app.db import migration_engine
from app.models.provider_disclosure import ProviderDisclosureEvent

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _alembic(*args):
    subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_ROOT,
        env={**os.environ},
        check=True,
    )


def test_standalone_0069_with_local_data_upgrades_without_skipping_embedding(superuser_db, make_verified_user):
    user, _ = make_verified_user()
    event = ProviderDisclosureEvent(
        owner_id=user.id,
        provider="ollama",
        model="synthetic",
        purpose="migration-test",
        requested_by="migration-test",
        decision="local",
        reason="synthetic",
        attempted_content_hash="a",
        sent_content_hash="b",
    )
    superuser_db.add(event)
    superuser_db.commit()
    event_id = event.id
    superuser_db.close()
    migration_engine.dispose()

    try:
        _alembic("downgrade", "0069")
        migration_engine.dispose()
        with migration_engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == ["0069"]
            columns = {column["name"] for column in inspect(conn).get_columns("document_chunks")}
            assert "embedding_model" not in columns
            assert "embedding_native_dim" not in columns
            assert conn.execute(
                text("SELECT decision FROM provider_disclosure_events WHERE id = :id"), {"id": event_id}
            ).scalar_one() == "local"

        _alembic("upgrade", "head")
        migration_engine.dispose()
        with migration_engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == [
                "0073_provider_disclosure_local"
            ]
            columns = {column["name"] for column in inspect(conn).get_columns("document_chunks")}
            assert {"embedding_model", "embedding_native_dim"} <= columns
            assert conn.execute(
                text("SELECT decision, attempted_content_hash, sent_content_hash "
                     "FROM provider_disclosure_events WHERE id = :id"),
                {"id": event_id},
            ).one() == ("local", "a", "b")
    finally:
        _alembic("upgrade", "head")
        migration_engine.dispose()
