#!/usr/bin/env python3
"""Safe-internal DB backup → mutate → restore proof (local disposable DB only).

Demonstrates:
  backup made → state changed → restore performed → canonical memory recovered.
Never touches production. Defaults to lifeos_safe_internal_bak_proof on localhost:5433.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psycopg2

_BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BACKEND))

_PROOF_DB = os.environ.get("LIFEAI_BACKUP_PROOF_DB", "lifeos_safe_internal_bak_proof")


def _server_dsn(database_url: str, dbname: str = "postgres") -> str:
    parsed = urlparse(database_url)
    auth = parsed.username if not parsed.password else f"{parsed.username}:{parsed.password}"
    return f"postgresql://{auth}@{parsed.hostname}:{parsed.port or 5432}/{dbname}"


def _ensure_db(admin_url: str, db_name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_]+", db_name):
        raise SystemExit(f"refusing non-identifier DB name: {db_name!r}")
    admin = psycopg2.connect(_server_dsn(admin_url))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{db_name}"')
    admin.close()


def _drop_db(admin_url: str, db_name: str) -> None:
    admin = psycopg2.connect(_server_dsn(admin_url))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
            (db_name,),
        )
        cur.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    admin.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup/restore proof for safe-internal DB")
    parser.add_argument("--json", dest="json_out", default="/tmp/mainai-backup-restore-proof.json")
    parser.add_argument("--keep-db", action="store_true")
    args = parser.parse_args()

    admin_url = os.environ.get("DATABASE_URL", "postgresql://lifeos@localhost:5433/postgres")
    # Point env at proof DB for alembic + app.
    proof_url = _server_dsn(admin_url, _PROOF_DB)
    os.environ["DATABASE_URL"] = proof_url
    os.environ.setdefault(
        "APP_DATABASE_URL",
        f"postgresql://mainai_app:mainai_app_pw@localhost:5433/{_PROOF_DB}",
    )
    os.environ.setdefault("SECRET_KEY", "safe-internal-boot-local-only")
    os.environ.setdefault("FOUNDER_EMAIL", "founder@lifeos.local")
    os.environ.setdefault("FOUNDER_PASSWORD", "TestFounderPassword123!")
    os.environ.setdefault("ENABLE_SCHEDULED_CLEANUP", "false")
    os.environ.setdefault("OPENAI_API_KEY", "disabled-for-safe-internal")

    work = Path("/tmp/mainai-backup-proof")
    work.mkdir(parents=True, exist_ok=True)
    dump_path = work / f"proof-{uuid.uuid4().hex[:8]}.dump"

    # Recreate clean proof DB
    _drop_db(admin_url, _PROOF_DB)
    _ensure_db(admin_url, _PROOF_DB)

    os.environ["LIFEAI_BOOT_DATABASE_NAME"] = _PROOF_DB

    from app.config import get_settings

    get_settings.cache_clear()
    import importlib.util

    boot_path = _BACKEND / "scripts" / "mainai" / "safe_internal_boot.py"
    spec = importlib.util.spec_from_file_location("safe_internal_boot", boot_path)
    boot_mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(boot_mod)
    boot_mod.ensure_boot_database()

    from sqlalchemy.orm import sessionmaker

    get_settings.cache_clear()
    from app.db import migration_engine
    from app.founder_memory import list_current_founder_memory, list_founder_memory
    from app.mainai_executive.existing_state import seed_rich_safe_internal_state
    from sqlalchemy import text

    Session = sessionmaker(bind=migration_engine)
    db = Session()
    try:
        seeded = seed_rich_safe_internal_state(
            db, owner_email=f"bak-{uuid.uuid4().hex[:8]}@local.internal"
        )
        db.commit()
        owner_id = uuid.UUID(seeded["owner_id"])
        before_current = [
            (str(n.id), n.content, n.status)
            for n in list_current_founder_memory(db, owner_id=owner_id)
        ]
        before_superseded = [
            (str(n.id), n.content, n.status)
            for n in list_founder_memory(db, owner_id=owner_id, status="superseded")
        ]
        canonical_correction = seeded["current_correction_id"]
        old_note_id = seeded["old_note_id"]
    finally:
        db.close()

    # BACKUP
    subprocess.run(
        ["pg_dump", "-Fc", "-f", str(dump_path), proof_url],
        check=True,
    )

    # MUTATE — poison state after backup
    db = Session()
    try:
        db.execute(
            text(
                "UPDATE founder_memory_notes SET content = 'POISONED_AFTER_BACKUP', status = 'active' "
                "WHERE id = :id"
            ),
            {"id": canonical_correction},
        )
        db.execute(
            text(
                "UPDATE founder_memory_notes SET status = 'active' "
                "WHERE id = :id"
            ),
            {"id": old_note_id},
        )
        db.commit()
        poisoned = list(
            db.execute(
                text("SELECT content FROM founder_memory_notes WHERE id = :id"),
                {"id": canonical_correction},
            ).scalars()
        )
        assert poisoned and poisoned[0] == "POISONED_AFTER_BACKUP"
    finally:
        db.close()

    # RESTORE into same DB (terminate sessions, drop+create, pg_restore)
    migration_engine.dispose()
    _drop_db(admin_url, _PROOF_DB)
    _ensure_db(admin_url, _PROOF_DB)
    subprocess.run(
        ["pg_restore", "--no-owner", "--dbname", proof_url, str(dump_path)],
        check=True,
    )

    get_settings.cache_clear()
    from app.db import migration_engine as eng2

    eng2.dispose()
    Session2 = sessionmaker(bind=eng2)
    db = Session2()
    try:
        after_current = [
            (str(n.id), n.content, n.status)
            for n in list_current_founder_memory(db, owner_id=owner_id)
        ]
        after_superseded = [
            (str(n.id), n.content, n.status)
            for n in list_founder_memory(db, owner_id=owner_id, status="superseded")
        ]
        corr = db.execute(
            text("SELECT content, status FROM founder_memory_notes WHERE id = :id"),
            {"id": canonical_correction},
        ).one()
        recovered = corr[0] != "POISONED_AFTER_BACKUP" and "CURRENT correction" in (corr[0] or "")
        ok = (
            recovered
            and before_current == after_current
            and before_superseded == after_superseded
            and dump_path.exists()
            and dump_path.stat().st_size > 0
        )
    finally:
        db.close()

    receipt = {
        "db": _PROOF_DB,
        "dump_path": str(dump_path),
        "dump_bytes": dump_path.stat().st_size if dump_path.exists() else 0,
        "owner_id": str(owner_id),
        "canonical_correction_id": canonical_correction,
        "before_current_count": len(before_current),
        "after_current_count": len(after_current),
        "recovered_correction": recovered,
        "BACKUP_RESTORE_OK": ok,
    }
    Path(args.json_out).write_text(json.dumps(receipt, indent=2, default=str))
    print(f"receipt_written={args.json_out}")
    print(f"BACKUP_RESTORE_OK={ok}")

    if not args.keep_db:
        _drop_db(admin_url, _PROOF_DB)

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
