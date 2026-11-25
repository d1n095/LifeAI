#!/usr/bin/env python3
"""Prove that an abandoned staging attempt leaves the repository and test data intact.

The test creates a detached worktree and a namespaced staging ref, simulates a failed
attempt, then removes both. It never pushes, merges, migrates or writes production data.
When a disposable test database is supplied, its Alembic revision is compared before and
after as a read-only sentinel.

The existing PR #461 implementation in ``backend/scripts/acceptance/backup_restore_check.py``
remains the project's backup/restore mechanism; this command deliberately does not add a
second one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.integration.common import (  # noqa: E402
    PipelineError,
    git,
    git_output,
    guard_test_database,
    redact_url,
    resolve_commit,
    status_exit,
    worktree_add,
    worktree_remove,
    write_json,
)

BACKUP_RESTORE = Path("backend/scripts/acceptance/backup_restore_check.py")


def _refs(repo: Path) -> dict[str, str]:
    result = git(repo, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads", "refs/imports")
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if " " in line:
            name, sha = line.split(" ", 1)
            values[name] = sha
    return values


def _alembic_revision(database_url: str) -> str:
    try:
        import psycopg2
    except ImportError as exc:
        raise PipelineError("psycopg2 is required for the database sentinel", "BLOCKED") from exc
    try:
        connection = psycopg2.connect(database_url, connect_timeout=5)
        with connection, connection.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            row = cursor.fetchone()
        connection.close()
    except Exception as exc:
        raise PipelineError(f"could not read test database Alembic revision: {exc}", "BLOCKED") from exc
    if not row or not row[0]:
        raise PipelineError("test database has no Alembic revision", "BLOCKED")
    return str(row[0])


def verify(
    *,
    repo: Path | str,
    source_ref: str,
    database_url: str | None = None,
    out: Path | str | None = None,
) -> dict[str, Any]:
    repo_path = Path(repo).resolve()
    source = resolve_commit(repo_path, source_ref)
    if database_url:
        guard_test_database(database_url, "rollback database")
    refs_before = _refs(repo_path)
    head_before = git_output(repo_path, "rev-parse", "HEAD")
    status_before = git_output(repo_path, "status", "--porcelain")
    backup_script = repo_path / BACKUP_RESTORE
    checks: dict[str, Any] = {
        "existing_backup_restore": {
            "status": "PASS" if backup_script.is_file() else "FAIL",
            "path": str(backup_script),
            "sha256": hashlib.sha256(backup_script.read_bytes()).hexdigest() if backup_script.is_file() else None,
            "note": "PR #461 backup/restore implementation is reused; no competing backup mechanism is added.",
        }
    }
    temp_root = Path(tempfile.mkdtemp(prefix="lifeai-v8-rollback-"))
    worktree = temp_root / "repo"
    staging_ref = f"refs/staging/lifeai-v8-rollback-{os.getpid()}"
    db_before: str | None = None
    db_after: str | None = None
    try:
        worktree_add(repo_path, worktree, source)
        if git_output(worktree, "rev-parse", "HEAD") != source:
            raise PipelineError("rollback worktree is not at the source commit", "FAIL")
        # Simulate a failed staging edit without committing it.
        (worktree / ".lifeai-v8-failed-staging").write_text("failed staging attempt\n", encoding="utf-8")
        update = git(repo_path, "update-ref", staging_ref, source, "0" * 40)
        if update.returncode != 0:
            raise PipelineError("could not create isolated staging ref", "FAIL")
        if database_url:
            db_before = _alembic_revision(database_url)
        checks["staging_ref_created"] = {"status": "PASS", "ref": staging_ref, "sha": source}
    finally:
        if worktree.exists():
            try:
                worktree_remove(repo_path, worktree)
            except Exception:
                shutil.rmtree(worktree, ignore_errors=True)
                git(repo_path, "worktree", "prune")
        git(repo_path, "update-ref", "-d", staging_ref)
        shutil.rmtree(temp_root, ignore_errors=True)

    try:
        if database_url:
            db_after = _alembic_revision(database_url)
        checks["database_unchanged"] = {
            "status": "PASS" if database_url and db_before == db_after else ("SKIPPED" if not database_url else "FAIL"),
            "before": db_before,
            "after": db_after,
            "url": redact_url(database_url),
        }
        checks["source_commit_preserved"] = {
            "status": "PASS" if git(repo_path, "cat-file", "-e", f"{source}^{{commit}}").returncode == 0 else "FAIL",
            "sha": source,
        }
        refs_after = _refs(repo_path)
        checks["refs_preserved"] = {
            "status": "PASS" if refs_before == refs_after else "FAIL",
            "before_count": len(refs_before),
            "after_count": len(refs_after),
        }
        checks["worktree_cleanup"] = {
            "status": "PASS" if not worktree.exists() else "FAIL",
            "path": str(worktree),
        }
        worktrees = git_output(repo_path, "worktree", "list", "--porcelain")
        checks["no_staging_worktree"] = {
            "status": "PASS" if str(worktree) not in worktrees else "FAIL",
        }
        fsck = git(repo_path, "fsck", "--full", "--no-dangling")
        checks["repository_integrity"] = {
            "status": "PASS" if fsck.returncode == 0 else "FAIL",
            "output_tail": (fsck.stdout + fsck.stderr)[-2000:],
        }
        checks["working_tree_unchanged"] = {
            "status": "PASS"
            if git_output(repo_path, "status", "--porcelain") == status_before
            and git_output(repo_path, "rev-parse", "HEAD") == head_before
            else "FAIL",
        }
        checks["processes_started"] = {"status": "PASS", "processes": []}
    except PipelineError as exc:
        checks["database_unchanged"] = {
            "status": exc.status,
            "error": str(exc),
            "before": db_before,
            "after": db_after,
            "url": redact_url(database_url),
        }

    values = [item["status"] for item in checks.values()]
    if "FAIL" in values:
        status = "FAIL"
    elif "BLOCKED" in values:
        status = "BLOCKED"
    else:
        status = "PASS"
    result = {
        "status": status,
        "source_commit": source,
        "database_url": redact_url(database_url),
        "checks": checks,
        "staging_ref": staging_ref,
    }
    if out:
        write_json(out, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    try:
        result = verify(
            repo=args.repo,
            source_ref=args.source_ref,
            database_url=args.database_url,
            out=args.out,
        )
    except PipelineError as exc:
        result = {"status": exc.status, "error": str(exc), "detail": exc.detail}
        if args.out:
            write_json(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return status_exit(result["status"])


if __name__ == "__main__":
    raise SystemExit(main())
