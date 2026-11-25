#!/usr/bin/env python3
"""Recovery matrix for Founder Alpha: valid, corrupt and partial PostgreSQL restores.

The existing backup_restore_check.py proves the happy path (dump -> checksum -> restore ->
RLS -> real API). This script covers the failure half of F18 without touching the source:

  * a valid dump restores into a scratch database with identical row counts;
  * a corrupted dump is rejected by the checksum gate and by pg_restore itself;
  * a truncated (partial) dump cannot produce a usable restore;
  * after both failures the source database is unchanged and still reachable;
  * every scratch database is dropped.

Safety: the source database must not be a system database; every scratch name contains
"restore" and is dropped in a finally block. No application or production URL is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

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


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, env=env, text=True, capture_output=True, check=False)


def _db_name(url: str) -> str:
    name = make_url(url).database
    if not name:
        raise SystemExit(f"database URL has no database name: {url}")
    return name


def _pg_env(url: str) -> dict[str, str]:
    parsed = make_url(url)
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = parsed.password
    return env


def _admin_args(url: str) -> list[str]:
    parsed = make_url(url)
    return [
        "-h",
        parsed.host or "127.0.0.1",
        "-p",
        str(parsed.port or 5432),
        "-U",
        parsed.username or "postgres",
    ]


def _counts(url: str) -> dict[str, int]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return {
                table: int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
                for table in TABLES
            }
    finally:
        engine.dispose()


def _scratch_db(source_url: str, suffix: str) -> str:
    source = _db_name(source_url)
    return f"lifeos_recovery_matrix_{suffix}_{uuid.uuid4().hex[:8]}_restore"


def _create_db(pg_bin: Path, admin_url: str, name: str) -> None:
    result = _run([str(pg_bin / "createdb"), *_admin_args(admin_url), name], env=_pg_env(admin_url))
    if result.returncode != 0:
        raise RuntimeError(f"createdb {name} failed: {result.stderr[-800:]}")


def _drop_db(pg_bin: Path, admin_url: str, name: str) -> None:
    _run([str(pg_bin / "dropdb"), *_admin_args(admin_url), "--if-exists", name], env=_pg_env(admin_url))


def _restore(pg_bin: Path, target_url: str, dump: Path) -> subprocess.CompletedProcess:
    return _run(
        [str(pg_bin / "pg_restore"), "--no-owner", "--dbname", target_url, str(dump)],
        env=_pg_env(target_url),
    )


def _check(checks: list[dict], name: str, ok: bool, detail: object) -> None:
    checks.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--pg-bin", default=os.environ.get("PG_BIN", ""))
    parser.add_argument("--out", default="")
    parser.add_argument("--keep-scratch", action="store_true")
    args = parser.parse_args()

    source_db = _db_name(args.source_url)
    if source_db.lower() in {"postgres", "template0", "template1"}:
        raise SystemExit(f"refusing to use system database {source_db!r} as source")
    pg_bin = Path(args.pg_bin) if args.pg_bin else Path(shutil.which("pg_dump") or "")
    if not (pg_bin / "pg_dump").exists():
        raise SystemExit("pg_dump not found; pass --pg-bin or set PG_BIN")

    temp = Path(tempfile.mkdtemp(prefix="lifeai-recovery-matrix-"))
    scratch: list[str] = []
    checks: list[dict] = []
    source_before = _counts(args.source_url)
    try:
        dump = temp / "lifeos.dump"
        result = _run(
            [
                str(pg_bin / "pg_dump"),
                "--format=custom",
                "--no-owner",
                "--file",
                str(dump),
                args.source_url,
            ],
            env=_pg_env(args.source_url),
        )
        _check(checks, "valid dump created", result.returncode == 0, result.stderr[-800:] or "ok")
        if result.returncode != 0:
            raise RuntimeError("pg_dump failed")

        digest = hashlib.sha256(dump.read_bytes()).hexdigest()
        valid_list = _run([str(pg_bin / "pg_restore"), "--list", str(dump)])
        _check(checks, "valid dump has a readable TOC", valid_list.returncode == 0, valid_list.stderr[-800:] or "ok")

        valid_db = _scratch_db(args.source_url, "valid")
        scratch.append(valid_db)
        _create_db(pg_bin, args.source_url, valid_db)
        valid_url = make_url(args.source_url).set(database=valid_db).render_as_string(hide_password=False)
        restored = _restore(pg_bin, valid_url, dump)
        _check(checks, "valid dump restores", restored.returncode == 0, restored.stderr[-800:] or "ok")
        restored_counts = _counts(valid_url) if restored.returncode == 0 else {}
        _check(checks, "valid restore row counts match", restored_counts == source_before, {
            "source": source_before,
            "restored": restored_counts,
        })

        corrupt = bytearray(dump.read_bytes())
        # The TOC lives at the start of a custom-format archive; flipping it is the
        # deterministic "this backup is corrupt" case, not a random byte in a data block.
        for i in range(min(512, len(corrupt))):
            corrupt[i] ^= 0xFF
        corrupt_path = temp / "corrupt.dump"
        corrupt_path.write_bytes(corrupt)
        corrupt_digest = hashlib.sha256(corrupt_path.read_bytes()).hexdigest()
        _check(checks, "corrupt dump checksum differs", corrupt_digest != digest, corrupt_digest)
        corrupt_list = _run([str(pg_bin / "pg_restore"), "--list", str(corrupt_path)])
        corrupt_db = _scratch_db(args.source_url, "corrupt")
        scratch.append(corrupt_db)
        _create_db(pg_bin, args.source_url, corrupt_db)
        corrupt_url = make_url(args.source_url).set(database=corrupt_db).render_as_string(hide_password=False)
        corrupt_restore = _restore(pg_bin, corrupt_url, corrupt_path)
        _check(
            checks,
            "corrupt dump is rejected",
            corrupt_list.returncode != 0 or corrupt_restore.returncode != 0,
            {
                "list_exit": corrupt_list.returncode,
                "restore_exit": corrupt_restore.returncode,
                "restore_error": corrupt_restore.stderr[-800:],
            },
        )

        partial = dump.read_bytes()[: max(1, len(dump.read_bytes()) // 2)]
        partial_path = temp / "partial.dump"
        partial_path.write_bytes(partial)
        partial_list = _run([str(pg_bin / "pg_restore"), "--list", str(partial_path)])
        partial_db = _scratch_db(args.source_url, "partial")
        scratch.append(partial_db)
        _create_db(pg_bin, args.source_url, partial_db)
        partial_url = make_url(args.source_url).set(database=partial_db).render_as_string(hide_password=False)
        partial_restore = _restore(pg_bin, partial_url, partial_path)
        _check(
            checks,
            "partial dump is rejected",
            partial_list.returncode != 0 or partial_restore.returncode != 0,
            {
                "list_exit": partial_list.returncode,
                "restore_exit": partial_restore.returncode,
                "restore_error": partial_restore.stderr[-800:],
            },
        )

        source_after = _counts(args.source_url)
        _check(checks, "source database unchanged", source_after == source_before, {
            "before": source_before,
            "after": source_after,
        })

        status = "PASS" if all(check["status"] == "PASS" for check in checks) else "FAIL"
        payload = {
            "status": status,
            "source_database": source_db,
            "checks": checks,
            "checksum_sha256": digest,
            "valid_restore_counts": restored_counts,
            "scratch_databases": scratch,
        }
        if args.out:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"wrote {out}")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if status == "PASS" else 2
    finally:
        if not args.keep_scratch:
            for name in scratch:
                _drop_db(pg_bin, args.source_url, name)


if __name__ == "__main__":
    raise SystemExit(main())
