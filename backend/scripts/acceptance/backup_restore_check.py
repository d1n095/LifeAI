#!/usr/bin/env python3
"""Isolated PostgreSQL backup/restore acceptance for Founder Alpha.

This deliberately exercises the database path that the VPS backup script does NOT cover:
pg_dump -> checksum -> pg_restore into a NEW database -> runtime grants -> RLS -> start the
real API on the restored data -> login, read documents/conversations, and complete one chat.

Safety:
  * the source and restore database names must differ;
  * the restore database name must contain "restore";
  * the restore database is dropped/recreated, never the source;
  * no production URL is accepted unless it is explicitly named as a restore target.

The source database must already be migrated and contain at least one document, chunk and
conversation. Run the founder acceptance suite first if it is empty:

    cd backend
    DATABASE_URL=... APP_DATABASE_URL=... pytest tests/founder_acceptance -q

Then:

    python scripts/acceptance/backup_restore_check.py \
      --source-url postgresql://postgres@127.0.0.1:55432/lifeos_e2e \
      --restore-url postgresql://postgres@127.0.0.1:55432/lifeos_restore_check \
      --restore-app-url postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_restore_check \
      --pg-bin /tmp/lifeai-testenv/root/usr/lib/postgresql/16/bin \
      --redis-url redis://127.0.0.1:56379/12

Exit code 0 means every step passed; any failed assertion exits non-zero and keeps the
restore database for diagnosis unless --cleanup-on-failure is passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

TABLES = (
    "users",
    "conversations",
    "messages",
    "documents",
    "document_chunks",
    "knowledge_import_jobs",
    "memory_source_units",
)
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


def _run(cmd: list[str], *, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    print("+", " ".join(str(part) for part in cmd))
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, check=True)


def _db_name(url: str) -> str:
    name = make_url(url).database
    if not name:
        raise SystemExit(f"database URL has no database name: {url}")
    return name


def _safe_databases(source_url: str, restore_url: str) -> None:
    source = _db_name(source_url)
    restore = _db_name(restore_url)
    if source == restore:
        raise SystemExit("refusing to restore over the source database")
    if "restore" not in restore:
        raise SystemExit("restore database name must contain 'restore' (safety guard)")
    if source.lower() in {"postgres", "template0", "template1"}:
        raise SystemExit(f"refusing to use system database {source!r} as source")


def _counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        return {table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()) for table in TABLES}


def _pg_env(url: str) -> dict[str, str]:
    parsed = make_url(url)
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return env


def _pg_admin_args(url: str) -> list[str]:
    parsed = make_url(url)
    args = ["-h", parsed.host or "127.0.0.1", "-p", str(parsed.port or 5432), "-U", parsed.username or "postgres"]
    return args


def _apply_privileges(restore_url: str, restore_app_url: str, temp: Path) -> None:
    """Mirror the documented boot order on the restored database.

    pg_restore is run with --no-acl so it never depends on the source's role ownership. That
    means the app role starts with no grants on the restored tables, so run the same
    ensure_app_role -> apply_runtime_privileges sequence the real entrypoint uses before the
    API startup policies narrow the grants again.
    """
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": restore_url,
            "APP_DATABASE_URL": restore_app_url,
            "PYTHONPATH": ".",
            "MAINAI_APP_PASSWORD": make_url(restore_app_url).password or "",
            "RENDER_ENV_FILE": str(temp / "render-env.sh"),
        }
    )
    _run([sys.executable, "scripts/security/ensure_app_role.py"], cwd=BACKEND_ROOT, env=env)
    _run([sys.executable, "scripts/security/apply_runtime_privileges.py"], cwd=BACKEND_ROOT, env=env)


def _ensure_other_user(engine, founder_email: str) -> bool:
    """Create one non-founder principal if the isolated source has only the founder.

    RLS isolation needs a second real user id to query as; a random UUID would only prove
    "no rows for an unknown id", not that two actual owners are separated. This is opt-in
    (--seed-other-user) because it mutates the isolated source database.
    """
    from sqlalchemy.orm import Session

    from app.models.user import User, UserRole
    from app.security import hash_password

    with Session(engine) as db:
        existing = db.query(User).filter(User.email != founder_email).first()
        if existing is not None:
            return False
        db.add(
            User(
                id=uuid.uuid4(),
                email=f"backup-other-{uuid.uuid4().hex[:10]}@example.invalid",
                password_hash=hash_password("BackupOtherPassword123!"),
                role=UserRole.member,
                email_verified=True,
            )
        )
        db.commit()
        return True


def _verify_rls(source_engine, restore_app_engine) -> None:
    with source_engine.connect() as conn:
        owner = conn.execute(text("SELECT uploaded_by FROM documents LIMIT 1")).scalar()
        other = conn.execute(text("SELECT id FROM users WHERE id <> :owner LIMIT 1"), {"owner": owner}).scalar()
    if owner is None or other is None:
        raise AssertionError("RLS check needs at least one document owner and one other user")

    with restore_app_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner)})
        own_rows = conn.execute(text("SELECT uploaded_by FROM documents")).scalars().all()
        assert own_rows, "owner must see the restored document through the restricted app role"
        assert all(str(row) == str(owner) for row in own_rows), "RLS leaked another owner's documents"

    with restore_app_engine.begin() as conn:
        conn.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(other)})
        other_rows = conn.execute(text("SELECT uploaded_by FROM documents")).scalars().all()
        assert not other_rows, "RLS returned the founder's documents to another owner"

    with restore_app_engine.connect() as conn:
        privileges = conn.execute(
            text(
                "SELECT has_table_privilege('mainai_app','alembic_version','TRUNCATE'),"
                " has_table_privilege('mainai_app','memory_source_units','TRUNCATE')"
            )
        ).one()
    assert tuple(privileges) == (False, False), f"restricted role has TRUNCATE: {tuple(privileges)}"


def _wait_for_health(client: httpx.Client, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            response = client.get("/api/health")
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:
            last_error = exc
        time.sleep(1)
    raise AssertionError(f"restored API did not become healthy: {last_error!r}")


def _start_and_verify_api(args: argparse.Namespace, source_counts: dict[str, int], temp: Path) -> dict:
    port = args.api_port
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": args.restore_url,
            "APP_DATABASE_URL": args.restore_app_url,
            "REDIS_URL": args.redis_url,
            "SECRET_KEY": args.secret_key,
            "FOUNDER_EMAIL": args.founder_email,
            "FOUNDER_PASSWORD": args.founder_password,
            "E2E_FOUNDER_PASSWORD": args.founder_password,
            "E2E_DATABASE_URL": args.restore_url,
            "E2E_EMAIL_LOG_PATH": str(temp / "emails.jsonl"),
            "E2E_BACKEND_PORT": str(port),
            "STORAGE_ROOT": str(temp / "storage"),
            "FRONTEND_ORIGINS": f"http://127.0.0.1:{port}",
            "PUBLIC_APP_URL": f"http://127.0.0.1:{port}",
            "ENABLE_SCHEDULED_CLEANUP": "false",
            # The local acceptance API is plain HTTP; httpx (unlike the browser E2E) will not
            # send a Secure cookie over http. This only affects the isolated check.
            "COOKIE_SECURE": "false",
            "OPENAI_API_KEY": "fake-key-for-backup-restore-check",
            "RATE_LIMIT_LOGIN_PER_MINUTE": "1000",
            "RATE_LIMIT_REGISTER_PER_MINUTE": "1000",
            "RATE_LIMIT_FORGOT_PASSWORD_PER_MINUTE": "1000",
            "RATE_LIMIT_RESET_PASSWORD_PER_MINUTE": "1000",
            "RATE_LIMIT_VERIFY_EMAIL_PER_MINUTE": "1000",
            "PYTHONPATH": ".",
        }
    )
    log = (temp / "backend.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "scripts/ci/run_e2e_backend.py"],
        cwd=BACKEND_ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120.0) as client:
            _wait_for_health(client)
            login = client.post(
                "/api/auth/login",
                json={"email": args.founder_email, "password": args.founder_password},
            )
            assert login.status_code == 200, f"restored founder login failed: {login.status_code} {login.text[:300]}"
            csrf = login.json()["csrf_token"]

            documents = client.get("/api/documents")
            conversations = client.get("/api/conversations")
            status = client.get("/api/mainai/project-status")
            for name, response in (("documents", documents), ("conversations", conversations), ("project-status", status)):
                assert response.status_code == 200, f"restored {name} failed: {response.status_code} {response.text[:300]}"

            chat = client.post(
                "/api/chat",
                json={"message": "Vad finns i mitt bibliotek efter återställningen?"},
                headers={"X-CSRF-Token": csrf},
            )
            assert chat.status_code == 200, f"restored chat failed: {chat.status_code} {chat.text[:300]}"
            body = chat.json()
            assert body.get("reply"), "restored API returned an empty chat reply"
            return {
                "login": login.status_code,
                "documents": documents.status_code,
                "conversations": conversations.status_code,
                "project_status": status.status_code,
                "chat": chat.status_code,
                "reply_chars": len(body["reply"]),
                "source_documents": source_counts["documents"],
                "source_conversations": source_counts["conversations"],
            }
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--restore-url", required=True)
    parser.add_argument("--restore-app-url", required=True)
    parser.add_argument("--pg-bin", default=os.environ.get("PG_BIN", ""))
    parser.add_argument("--redis-url", default=os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"))
    parser.add_argument("--secret-key", default="backup-restore-local-secret")
    parser.add_argument("--founder-email", default="founder@lifeos.local")
    parser.add_argument("--founder-password", default="TestFounderPassword123!")
    parser.add_argument("--api-port", type=int, default=8012)
    parser.add_argument("--seed-other-user", action="store_true")
    parser.add_argument("--cleanup-on-failure", action="store_true")
    parser.add_argument("--out", default="", help="write a machine-readable PASS summary here")
    args = parser.parse_args()

    _safe_databases(args.source_url, args.restore_url)
    pg_bin = Path(args.pg_bin) if args.pg_bin else Path(shutil.which("pg_dump") or "")
    if not (pg_bin / "pg_dump").exists():
        raise SystemExit("pg_dump not found; pass --pg-bin or set PG_BIN")
    restore_db = _db_name(args.restore_url)

    source_engine = create_engine(args.source_url)
    restore_engine = create_engine(args.restore_url)
    restore_app_engine = create_engine(args.restore_app_url)
    temp = Path(tempfile.mkdtemp(prefix="lifeai-backup-restore-"))
    success = False
    try:
        source_counts = _counts(source_engine)
        if args.seed_other_user and _ensure_other_user(source_engine, args.founder_email):
            source_counts = _counts(source_engine)
            print("seeded one non-founder principal for the RLS check")
        print("source counts:", json.dumps(source_counts, sort_keys=True))
        for required in ("users", "documents", "document_chunks", "conversations"):
            assert source_counts[required] > 0, (
                f"source has no {required}; seed it through the real API first "
                "(do not use pytest: tests/conftest.py drops the DATABASE_URL database)"
            )

        dump = temp / "lifeos.dump"
        # Keep ACLs: a real restore must preserve the source's function grants/revokes
        # (notably PUBLIC must not regain EXECUTE on trigger functions). Only ownership is
        # normalized, because the restore database is created by the local admin role.
        _run(
            [str(pg_bin / "pg_dump"), "--format=custom", "--no-owner", "--file", str(dump), args.source_url],
            env=_pg_env(args.source_url),
        )
        digest = hashlib.sha256(dump.read_bytes()).hexdigest()
        checksum = temp / "lifeos.dump.sha256"
        checksum.write_text(f"{digest}  {dump.name}\n", encoding="utf-8")
        _run(["sha256sum", "-c", checksum.name], cwd=temp)
        print(f"backup checksum verified: {digest}")

        admin_args = _pg_admin_args(args.restore_url)
        _run([str(pg_bin / "dropdb"), *admin_args, "--if-exists", restore_db], env=_pg_env(args.restore_url))
        _run([str(pg_bin / "createdb"), *admin_args, restore_db], env=_pg_env(args.restore_url))
        _run(
            [str(pg_bin / "pg_restore"), "--no-owner", "--dbname", args.restore_url, str(dump)],
            env=_pg_env(args.restore_url),
        )
        _apply_privileges(args.restore_url, args.restore_app_url, temp)

        restored_counts = _counts(restore_engine)
        print("restored counts:", json.dumps(restored_counts, sort_keys=True))
        assert restored_counts == source_counts, f"row counts differ: {source_counts} != {restored_counts}"
        _verify_rls(source_engine, restore_app_engine)
        print("RLS and runtime privileges verified")

        api_result = _start_and_verify_api(args, source_counts, temp)
        print("restored API verified:", json.dumps(api_result, sort_keys=True))
        success = True
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "source_database": _db_name(args.source_url),
                        "restore_database": restore_db,
                        "source_counts": source_counts,
                        "restored_counts": restored_counts,
                        "checksum_sha256": digest,
                        "rls_verified": True,
                        "api": api_result,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"wrote {out}")
        print("PASS: isolated PostgreSQL backup/restore round trip")
        return 0
    finally:
        source_engine.dispose()
        restore_engine.dispose()
        restore_app_engine.dispose()
        if success or args.cleanup_on_failure:
            try:
                admin_args = _pg_admin_args(args.restore_url)
                _run([str(pg_bin / "dropdb"), *admin_args, "--if-exists", restore_db], env=_pg_env(args.restore_url))
            except Exception as exc:  # cleanup must not hide the real result
                print(f"warning: could not drop {restore_db}: {exc}", file=sys.stderr)
        else:
            print(f"kept restore database {restore_db} and artifacts in {temp} for diagnosis", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
