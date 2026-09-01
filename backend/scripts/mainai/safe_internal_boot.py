#!/usr/bin/env python3
"""Boot MainAI in SAFE INTERNAL mode from the current checkout.

Usage (from backend/):

  python scripts/mainai/safe_internal_boot.py --json /tmp/mainai-boot-receipt.json

Defaults to local Postgres on localhost:5433 database ``lifeos_safe_internal``
(created + migrated on first run). Override with DATABASE_URL / LIFEAI_BOOT_DATABASE_NAME.

Provider call count must remain 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import psycopg2

_BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BACKEND))

_BOOT_DB = os.environ.get("LIFEAI_BOOT_DATABASE_NAME", "lifeos_safe_internal")
os.environ.setdefault("DATABASE_URL", f"postgresql://lifeos@localhost:5433/{_BOOT_DB}")
os.environ.setdefault(
    "APP_DATABASE_URL", f"postgresql://mainai_app:mainai_app_pw@localhost:5433/{_BOOT_DB}"
)
os.environ.setdefault("SECRET_KEY", "safe-internal-boot-local-only")
os.environ.setdefault("FOUNDER_EMAIL", "founder@lifeos.local")
os.environ.setdefault("FOUNDER_PASSWORD", "TestFounderPassword123!")
os.environ.setdefault("ENABLE_SCHEDULED_CLEANUP", "false")
os.environ.setdefault("OPENAI_API_KEY", "disabled-for-safe-internal")


def _server_dsn(database_url: str, dbname: str = "postgres") -> str:
    parsed = urlparse(database_url)
    auth = parsed.username if not parsed.password else f"{parsed.username}:{parsed.password}"
    return f"postgresql://{auth}@{parsed.hostname}:{parsed.port or 5432}/{dbname}"


def ensure_boot_database() -> None:
    """Create boot DB if missing, alembic upgrade head, apply RLS/privileges."""
    database_url = os.environ["DATABASE_URL"]
    app_url = os.environ["APP_DATABASE_URL"]
    db_name = urlparse(database_url).path.lstrip("/")
    if not re.fullmatch(r"[A-Za-z0-9_]+", db_name):
        raise SystemExit(f"refusing non-identifier boot DB name: {db_name!r}")
    app_role = urlparse(app_url).username or "mainai_app"
    app_pw = urlparse(app_url).password or "mainai_app_pw"

    admin = psycopg2.connect(_server_dsn(database_url))
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
        if cur.fetchone() is None:
            cur.execute(f'CREATE DATABASE "{db_name}"')
            print(f"created_database={db_name}")
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_role,))
        if cur.fetchone() is None:
            cur.execute(f"CREATE ROLE {app_role} LOGIN PASSWORD %s", (app_pw,))
    admin.close()

    db = psycopg2.connect(database_url)
    db.autocommit = True
    with db.cursor() as cur:
        cur.execute(f"GRANT USAGE ON SCHEMA public TO {app_role}")
        cur.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {app_role}"
        )
        cur.execute(f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {app_role}")
        cur.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_role}"
        )
        cur.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL PRIVILEGES ON SEQUENCES TO {app_role}"
        )
    db.close()

    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(_BACKEND),
        check=True,
        env={**os.environ},
    )

    # Import only after DATABASE_URL is set and DB migrated.
    from app.config import get_settings

    get_settings.cache_clear()
    from app.db import migration_engine
    from app.rls import (
        apply_mainai_execution_privileges,
        apply_mainai_job_runtime_privileges,
        apply_rls,
    )

    apply_rls(migration_engine)
    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)
    print(f"boot_database_ready={db_name}")


def main() -> int:
    parser = argparse.ArgumentParser(description="MainAI safe-internal boot")
    parser.add_argument("--owner-email", default=None)
    parser.add_argument("--task", default=None, help="Override founder task text")
    parser.add_argument("--json", dest="json_out", default=None, help="Write receipt JSON")
    parser.add_argument("--session-id", default=None)
    parser.add_argument(
        "--skip-db-ensure",
        action="store_true",
        help="Assume DATABASE_URL already migrated (advanced)",
    )
    args = parser.parse_args()

    if not args.skip_db_ensure:
        ensure_boot_database()

    from sqlalchemy.orm import sessionmaker

    from app.config import get_settings
    from app.db import migration_engine
    from app.mainai_executive.internal_start import (
        DEFAULT_FOUNDER_TASK,
        run_first_real_internal_boot,
    )

    get_settings.cache_clear()
    Session = sessionmaker(bind=migration_engine)
    db = Session()
    try:
        report = run_first_real_internal_boot(
            db,
            owner_email=args.owner_email,
            founder_task=args.task or DEFAULT_FOUNDER_TASK,
            session_id=args.session_id,
        )
        payload = report.as_dict()
        if args.json_out:
            Path(args.json_out).write_text(json.dumps(payload, indent=2, default=str))
            print(f"receipt_written={args.json_out}")
        print("=== MAINAI SAFE-INTERNAL BOOT ===")
        print(f"readiness={report.readiness_level}")
        print(f"session_id={report.session_id}")
        print(f"first_task_ok={report.first_task_ok} phase={report.first_task_phase}")
        print(
            f"local_attempt_used={report.local_attempt_used} school_used={report.school_used}"
        )
        print(f"provider_call_count={report.provider_call_count}")
        print(
            f"shutdown_ok={report.shutdown_ok} restart_ok={report.restart_ok} resume_ok={report.resume_ok}"
        )
        print(f"offline_ok={report.offline_ok}")
        print(f"PROVIDER_ENABLED={report.status_surface.get('PROVIDER_ENABLED')}")
        ok = (
            report.first_task_ok
            and report.shutdown_ok
            and report.restart_ok
            and report.resume_ok
            and report.provider_call_count == 0
            and report.local_attempt_used
            and report.school_used
        )
        print(f"BOOT_SUCCESS={ok}")
        return 0 if ok else 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
