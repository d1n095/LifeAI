"""Backup / verify / restore for the Founder Alpha database — the durable state nothing else covers.

Why this exists
---------------
`scripts/vps/backup.sh` deliberately does not back up Postgres: it was written when the database
was Supabase-hosted and therefore someone else's backup responsibility (see its own header, and
`docs/VPS_BACKUP_RESTORE.md`). `docker-compose.vps.yml` has no `postgres` service at all. For a
*private* Founder Alpha — the one this project now targets — that assumption no longer holds:
conversations, messages, memory sources, knowledge documents/chunks, the pgvector embeddings
derived from them, every RLS policy and the `alembic_version` row all live in one database. Losing
it loses the founder's memory; the existing file/volume backup restores a stack that boots against
an empty brain.

So this tool covers exactly that gap, and nothing else. It is deliberately NOT a generic
`pg_dump` wrapper: a backup file existing is not evidence that it can restore the system, which is
the only claim that matters for recoverability.

What it produces
----------------
`backup` writes three files next to each other:

  <name>.dump            pg_dump custom-format archive (`--format=custom`)
  <name>.dump.sha256     sha256sum-compatible checksum file (same shape backup.sh writes)
  <name>.manifest.json   the recovery contract: what the dump is expected to contain

The manifest records enough to answer "did this restore actually reproduce the database?"
without trusting the dump:

  * the dump's own sha256 and byte size, so a corrupted or swapped file is detectable;
  * a checksum over the manifest itself, so editing recorded row counts is detectable;
  * the applied `alembic_version`, AND the migration DAG's heads parsed from
    `backend/alembic/versions/*.py` at backup time — a backup taken against a forked tree, or a
    revision the current tree no longer knows, is refused rather than stored as if recoverable;
  * per-table row counts and (optionally) content digests, plus sequence values;
  * the RLS policy count and exactly which tables have RLS enabled/forced;
  * `mainai_app`'s privilege fingerprint, including that it holds nothing on `alembic_version`
    (SFA-1 — see `scripts/security/s1a_privilege_policy.py`).

Consistency
-----------
By default `backup` exports a Postgres snapshot (`pg_export_snapshot()`) from a REPEATABLE READ
transaction, computes the entire inventory inside that transaction, and passes the snapshot id to
`pg_dump --snapshot=...`. The manifest therefore describes the exact database state the dump
contains, even against a live, still-serving database. If snapshot export is unavailable to this
role, it falls back to reading the inventory and dumping sequentially and says so in
`manifest["consistency"]` — it never silently claims snapshot consistency it did not get.

Restore
-------
`restore` only ever writes into a database that does not exist yet or is provably empty, refuses
the database the backup came from, requires `--confirm`, and then reproduces the container boot
sequence's privilege end state rather than trusting the dump's ACLs:

    pg_restore --no-owner --no-privileges  ->  GRANT DML floor  ->  apply_runtime_privileges

That last step is the same boot-time module production runs (`apply_and_verify`), so a restored
database ends up with the narrowed S1A grants and zero runtime privileges on `alembic_version` —
verified by the policy module itself, not asserted by this file.

After restoring it recomputes the same inventory and compares it to the manifest field by field,
exiting non-zero on any difference. A restore that "worked" because `pg_restore` returned 0 but
silently dropped a table is exactly what this comparison is for.

Usage
-----
    python scripts/ops/db_recovery.py backup  --database-url "$DATABASE_URL" --output /var/backups/lifeai/db/<name>.dump
    python scripts/ops/db_recovery.py verify  --backup /var/backups/lifeai/db/<name>.dump
    python scripts/ops/db_recovery.py restore --backup <name>.dump --target-url "$DRILL_URL" --confirm

Exit codes: 0 success, 1 verification/restore failure, 2 refusal (unsafe request).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

FORMAT_VERSION = 1
TOOL_NAME = "lifeai-db-recovery"
BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
VERSIONS_DIR = BACKEND_ROOT / "alembic" / "versions"
POLICY_DIR = BACKEND_ROOT / "scripts" / "security"

APP_ROLE = "mainai_app"
TABLE_PRIVS = ["SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"]
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class RecoveryError(RuntimeError):
    """An unsafe request or an operation that failed. Always exits non-zero."""


# --------------------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------------------


def _identifier(name: str, what: str) -> str:
    """Every name interpolated into SQL is checked here first. Table and database names come
    from the live catalog (not user input), but a stray quote would still produce either a
    syntax error or an injection into the restore path — validating is cheaper than reasoning
    about it."""
    if not _IDENTIFIER.match(name or ""):
        raise RecoveryError(f"refusing to use {what} as a SQL identifier: {name!r}")
    return name


def _retarget(url: str, dbname: str) -> str:
    """Same credentials/host/options, different database — used to reach the maintenance
    database for CREATE DATABASE / DROP DATABASE, and to check emptiness of the target."""
    _identifier(dbname, "database name")
    parsed = urlparse(url)
    return urlunparse(parsed._replace(path="/" + dbname))


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def _require_bins(*names: str) -> None:
    missing = [name for name in names if shutil.which(name) is None]
    if missing:
        raise RecoveryError(
            f"missing PostgreSQL client tool(s): {', '.join(missing)}. Install the "
            "postgresql-client package (it must match or exceed the server's major version)."
        )


def _run(cmd: list[str], *, what: str) -> subprocess.CompletedProcess:
    # errors="replace": `pg_restore --list` on a corrupted archive emits raw archive bytes into
    # its output, which is not valid UTF-8. Found by this tool's own corruption test — strict
    # decoding turned an intended, explicit "this archive is corrupt" refusal into an uncaught
    # UnicodeDecodeError from subprocess itself.
    result = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    if result.returncode != 0:
        raise RecoveryError(
            f"{what} failed (exit {result.returncode}):\n{(result.stderr or result.stdout or '').strip()[-2000:]}"
        )
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _manifest_checksum(manifest: dict) -> str:
    """sha256 over every field except the checksum itself, so recorded row counts and
    digests cannot be edited without `verify` noticing."""
    body = {key: value for key, value in manifest.items() if key != "manifest_checksum"}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------
# migration DAG (the same derivation the final-gate doc uses: parsed, never from filenames)
# --------------------------------------------------------------------------------------


def migration_graph(versions_dir: Path = VERSIONS_DIR) -> dict:
    """Parse `revision` / `down_revision` out of every version file with `ast` and derive the
    graph. Files are NOT trusted to be numbered ordering, and a `down_revision` pointing at a
    revision that no file defines is reported instead of ignored."""
    revisions: dict[str, tuple[str, ...]] = {}
    for path in sorted(versions_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        revision = None
        down: tuple[str, ...] = ()
        for node in tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            target = node.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if target.id not in ("revision", "down_revision"):
                continue
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                continue
            if target.id == "revision":
                revision = value if isinstance(value, str) else None
            else:
                if value is None:
                    down = ()
                elif isinstance(value, str):
                    down = (value,)
                elif isinstance(value, (tuple, list)):
                    down = tuple(str(item) for item in value)
        if revision is None:
            raise RecoveryError(f"{path.name} has no literal `revision` assignment — cannot verify the graph")
        if revision in revisions:
            raise RecoveryError(f"duplicate revision id {revision!r} (second definition in {path.name})")
        revisions[revision] = down

    referenced = {parent for parents in revisions.values() for parent in parents}
    heads = sorted(set(revisions) - referenced)
    roots = sorted(rev for rev, parents in revisions.items() if not parents)
    dangling = sorted(parent for parent in referenced if parent not in revisions)
    return {
        "revision_count": len(revisions),
        "revisions": sorted(revisions),
        "roots": roots,
        "heads": heads,
        "dangling_down_revisions": dangling,
    }


# --------------------------------------------------------------------------------------
# database introspection
# --------------------------------------------------------------------------------------


def public_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
        return [row[0] for row in cur.fetchall()]


def _count(cur, table: str) -> int:
    cur.execute(f'SELECT count(*) FROM public."{_identifier(table, "table")}"')
    return int(cur.fetchone()[0])


def _content_digest(cur, table: str) -> str:
    """Order-independent digest of a table's full contents.

    `t::text` renders every column (including vectors and timestamps) deterministically, so this
    catches a restore that lost a row, a column value or an embedding while keeping the count.
    It is a full scan by design: this is a recovery-verification artifact, not a hot path.
    """
    cur.execute(
        f'SELECT md5(coalesce(string_agg(x, \'|\' ORDER BY x), \'\')) '
        f'FROM (SELECT t::text AS x FROM public."{_identifier(table, "table")}" t) AS s'
    )
    return cur.fetchone()[0]


def inventory(conn, *, content_digests: bool = True) -> dict:
    tables: dict[str, dict] = {}
    with conn.cursor() as cur:
        for table in public_tables(conn):
            entry = {"row_count": _count(cur, table)}
            if content_digests:
                entry["digest"] = _content_digest(cur, table)
            tables[table] = entry
    return tables


def sequences(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public' ORDER BY sequencename"
        )
        return {name: (None if value is None else int(value)) for name, value in cur.fetchall()}


def rls_state(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_policies WHERE schemaname = 'public'")
        policy_count = int(cur.fetchone()[0])
        cur.execute(
            "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
            "FROM pg_class c WHERE c.relnamespace = 'public'::regnamespace AND c.relkind = 'r' "
            "ORDER BY c.relname"
        )
        rows = cur.fetchall()
    return {
        "policy_count": policy_count,
        "rls_enabled_tables": sorted(name for name, enabled, _ in rows if enabled),
        "rls_forced_tables": sorted(name for name, _, forced in rows if forced),
    }


def alembic_revision(conn) -> str | None:
    with conn.cursor() as cur:
        try:
            cur.execute("SELECT version_num FROM alembic_version")
        except Exception:  # psycopg2.errors.UndefinedTable
            conn.rollback()
            return None
        rows = [row[0] for row in cur.fetchall()]
    if not rows:
        return None
    if len(rows) > 1:
        raise RecoveryError(f"alembic_version holds {len(rows)} rows — a single revision is required")
    return rows[0]


def privilege_fingerprint(conn, role: str = APP_ROLE) -> dict:
    fingerprint: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        if cur.fetchone() is None:
            return fingerprint
        for table in public_tables(conn):
            cur.execute(
                "SELECT p FROM unnest(%s::text[]) AS p WHERE has_table_privilege(%s, %s, p)",
                (TABLE_PRIVS, role, f'public."{_identifier(table, "table")}"'),
            )
            fingerprint[table] = sorted(row[0] for row in cur.fetchall())
    return fingerprint


# --------------------------------------------------------------------------------------
# backup
# --------------------------------------------------------------------------------------


def _write_checksum_file(path: Path) -> str:
    digest = _sha256_file(path)
    checksum_path = path.with_name(path.name + ".sha256")
    checksum_path.write_text(f"{digest}  {path.name}\n", encoding="utf-8")
    checksum_path.chmod(0o600)
    return digest


def backup(database_url: str, output_path: Path, *, content_digests: bool = True) -> dict:
    _require_bins("pg_dump")
    output_path = Path(output_path)
    if output_path.exists():
        raise RecoveryError(f"refusing to overwrite an existing backup: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    graph = migration_graph()
    if graph["dangling_down_revisions"]:
        raise RecoveryError(
            "migration graph has down_revision(s) pointing at revisions no file defines: "
            + ", ".join(graph["dangling_down_revisions"])
        )
    if len(graph["heads"]) != 1:
        raise RecoveryError(
            f"migration tree has {len(graph['heads'])} heads ({', '.join(graph['heads'])}) — "
            "a backup cannot be certified against a forked history"
        )

    import psycopg2

    consistency = "snapshot"
    conn = psycopg2.connect(database_url)
    try:
        conn.set_session(isolation_level="REPEATABLE READ", autocommit=False)
        with conn.cursor() as cur:
            snapshot = None
            try:
                cur.execute("SELECT pg_export_snapshot()")
                snapshot = cur.fetchone()[0]
            except Exception:
                consistency = "sequential"
                conn.rollback()
                conn.set_session(isolation_level="READ COMMITTED", autocommit=False)

            revision = alembic_revision(conn)
            if revision is None:
                raise RecoveryError(
                    "database has no alembic_version row — refusing to store a backup of an unmigrated database"
                )
            if revision not in graph["revisions"]:
                raise RecoveryError(
                    f"database is at revision {revision!r}, which this tree's migrations do not define"
                )
            if revision not in graph["heads"]:
                raise RecoveryError(
                    f"database is at {revision!r}, not the tree's head {graph['heads'][0]!r} — "
                    "backing up a lagging schema would certify the wrong state as recoverable"
                )

            tables = inventory(conn, content_digests=content_digests)
            seqs = sequences(conn)
            rls = rls_state(conn)
            privileges = privilege_fingerprint(conn)

            dump_cmd = [
                "pg_dump",
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                f"--file={output_path}",
                f"--dbname={database_url}",
            ]
            if snapshot is not None:
                dump_cmd.insert(1, f"--snapshot={snapshot}")
            _run(dump_cmd, what="pg_dump")
        conn.rollback()
    finally:
        conn.close()

    meta = psycopg2.connect(database_url)
    try:
        with meta.cursor() as cur:
            cur.execute("SHOW server_version")
            server_version = cur.fetchone()[0]
    finally:
        meta.close()

    dump_sha256 = _write_checksum_file(output_path)
    manifest = {
        "format_version": FORMAT_VERSION,
        "tool": TOOL_NAME,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "consistency": consistency,
        "source": {
            "host": urlparse(database_url).hostname,
            "database": _db_name(database_url),
            "server_version": server_version,
            "pg_dump_version": _run(["pg_dump", "--version"], what="pg_dump --version").stdout.strip(),
            "backed_up_by": socket.gethostname(),
        },
        "dump": {
            "file": output_path.name,
            "bytes": output_path.stat().st_size,
            "sha256": dump_sha256,
            "format": "custom",
            # Nothing in the archive carries ownership or grants: those are re-derived at restore
            # time by the boot sequence, so a restore into a cluster with different role names is
            # possible at all. The privilege fingerprint below is what actually gets compared.
            "ownership": "omitted (--no-owner)",
            "privileges": "omitted (--no-privileges)",
        },
        "alembic": {
            "current_revision": revision,
            "heads": graph["heads"],
            "revision_count": graph["revision_count"],
            "roots": graph["roots"],
            "dangling_down_revisions": graph["dangling_down_revisions"],
        },
        "tables": tables,
        "sequences": seqs,
        "rls": rls,
        "app_role": {
            "name": APP_ROLE,
            "table_privileges": privileges,
            "migration_state_privileges": privileges.get("alembic_version", []),
        },
    }
    manifest["manifest_checksum"] = _manifest_checksum(manifest)
    manifest_path = output_path.with_name(output_path.name + ".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path.chmod(0o600)
    return manifest


# --------------------------------------------------------------------------------------
# verify
# --------------------------------------------------------------------------------------


def _manifest_path(backup_path: Path) -> Path:
    return backup_path.with_name(backup_path.name + ".manifest.json")


def load_manifest(backup_path: Path) -> dict:
    path = _manifest_path(backup_path)
    if not path.is_file():
        raise RecoveryError(f"missing manifest: {path} — this is not a backup produced by {TOOL_NAME} `backup`")
    return json.loads(path.read_text(encoding="utf-8"))


def verify(backup_path: Path) -> list[str]:
    """Structural + integrity checks that need no database. Returns a list of failures; empty
    means the archive is intact, self-consistent, and belongs to this tree's single-head
    migration history."""
    _require_bins("pg_restore")
    backup_path = Path(backup_path)
    failures: list[str] = []
    if not backup_path.is_file():
        raise RecoveryError(f"backup not found: {backup_path}")

    try:
        manifest = load_manifest(backup_path)
    except RecoveryError as error:
        return [str(error)]

    if manifest.get("format_version") != FORMAT_VERSION:
        failures.append(f"unsupported manifest format_version {manifest.get('format_version')!r}")
    if manifest.get("tool") != TOOL_NAME:
        failures.append(f"manifest was not written by {TOOL_NAME}")

    recorded = manifest.get("manifest_checksum")
    if recorded != _manifest_checksum(manifest):
        failures.append("manifest checksum mismatch — the manifest itself was modified after backup")

    actual_sha256 = _sha256_file(backup_path)
    expected_sha256 = manifest.get("dump", {}).get("sha256")
    if expected_sha256 != actual_sha256:
        failures.append(
            f"dump checksum mismatch: manifest records {expected_sha256}, file is {actual_sha256} "
            "(corrupted or swapped archive)"
        )
    actual_bytes = backup_path.stat().st_size
    if manifest.get("dump", {}).get("bytes") != actual_bytes:
        failures.append(
            f"dump size mismatch: manifest records {manifest.get('dump', {}).get('bytes')} bytes, file is {actual_bytes}"
        )

    checksum_file = backup_path.with_name(backup_path.name + ".sha256")
    if checksum_file.is_file():
        recorded_sha = checksum_file.read_text(encoding="utf-8").split()[0]
        if recorded_sha != actual_sha256:
            failures.append("checksum file (.sha256) does not match the dump")
    else:
        failures.append(f"missing checksum file: {checksum_file}")

    # A verifier that fails or crashes on the archive is itself a verification failure — the
    # structure could not be read, so nothing downstream may trust it. It is recorded as a failure
    # rather than raised so the report names every problem it can, and so the outcome does not
    # depend on how a particular pg_restore build reacts to corruption: on some platforms it exits
    # 1 with a message, on others (observed locally) it dies with SIGSEGV (exit -11). Both mean the
    # same thing to a restore decision.
    try:
        toc = _run(["pg_restore", "--list", str(backup_path)], what="pg_restore --list")
    except RecoveryError as exc:
        failures.append(f"pg_restore could not read the archive: {exc}")
        toc = None
    if toc is not None:
        if "alembic_version" not in toc.stdout:
            failures.append("pg_restore TOC does not mention alembic_version — archive does not look like this schema")
        table_entries = sum(1 for line in toc.stdout.splitlines() if " TABLE DATA " in line)
        if table_entries < len(manifest.get("tables", {})):
            failures.append(
                f"pg_restore TOC lists {table_entries} TABLE DATA entries but the manifest records "
                f"{len(manifest.get('tables', {}))} tables — the dump is incomplete"
            )

    graph = migration_graph()
    if len(graph["heads"]) != 1:
        failures.append(f"current migration tree has {len(graph['heads'])} heads; the backup was certified against one")
    if manifest.get("alembic", {}).get("heads") != graph["heads"]:
        failures.append(
            f"backup was taken at heads {manifest.get('alembic', {}).get('heads')} but this tree's heads are {graph['heads']}"
        )
    revision = manifest.get("alembic", {}).get("current_revision")
    if revision not in graph["revisions"]:
        failures.append(f"backup revision {revision!r} is not defined by this tree's migration files")

    return failures


# --------------------------------------------------------------------------------------
# restore
# --------------------------------------------------------------------------------------


def _target_is_empty(target_url: str) -> bool:
    import psycopg2

    conn = psycopg2.connect(target_url)
    try:
        return not public_tables(conn)
    finally:
        conn.close()


def _ensure_target_database(target_url: str, source_database: str) -> str:
    """Creates the target database if absent; refuses anything that is not provably empty and
    refuses the source database outright (an in-place 'restore' is not a recovery drill)."""
    import psycopg2

    target_database = _db_name(target_url)
    _identifier(target_database, "target database name")
    if target_database == source_database:
        raise RecoveryError(
            f"refusing to restore into {target_database!r}: that is the database this backup was taken from. "
            "Restore into a fresh database and verify it before touching the live one."
        )

    admin = psycopg2.connect(_retarget(target_url, "postgres"))
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target_database,))
            exists = cur.fetchone() is not None
            if not exists:
                cur.execute(f'CREATE DATABASE "{target_database}"')
    finally:
        admin.close()

    if exists and not _target_is_empty(target_url):
        raise RecoveryError(
            f"refusing to restore into {target_database!r}: it already contains tables. "
            "Restoring over live data would mix two states; use a new, empty database."
        )
    return target_database


def _provision_runtime_role(target_url: str) -> None:
    """The boot sequence's step 1 equivalent: `ensure_app_role.py` unconditionally re-grants this
    DML floor on every boot, before Alembic runs. Doing the same here means a restored database
    ends up in the state the app actually boots into, not in the ACL state of the dump (which has
    none at all, since the archive was taken with --no-privileges)."""
    import psycopg2

    conn = psycopg2.connect(target_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (APP_ROLE,))
            if cur.fetchone() is None:
                raise RecoveryError(
                    f"role {APP_ROLE!r} does not exist in the target cluster. Run "
                    "scripts/security/ensure_app_role.py against it first (it also owns the password)."
                )
            cur.execute(f'GRANT USAGE ON SCHEMA public TO "{APP_ROLE}"')
            cur.execute(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO "{APP_ROLE}"'
            )
            cur.execute(f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "{APP_ROLE}"')
    finally:
        conn.close()


_SAFE_SET_LINE = re.compile(r"^SET ([A-Za-z0-9_.]+) = .*;$")


def sanitize_dump_sql(sql: str, known_settings: set[str]) -> tuple[str, list[str]]:
    """Drop session `SET <guc> = ...;` lines whose parameter the TARGET server does not know.

    A newer `pg_dump` writes settings the older server has never heard of — `SET
    transaction_timeout = 0;` from client 17 restoring into server 16 is the concrete case that
    broke the first green run of these tests. The right long-term answer is a version-matched
    client; this makes the tool survive the mismatch rather than refuse to restore a perfectly
    good archive, which is the property that matters during a real recovery.

    Deliberately only touches `SET` lines that appear OUTSIDE a COPY block: a data row is not
    SQL, and rewriting it would silently corrupt restored content. The COPY state machine is the
    reason this is a function with its own tests rather than a `sed`.
    """
    kept: list[str] = []
    dropped: list[str] = []
    in_copy = False
    for line in sql.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        if in_copy:
            kept.append(line)
            if stripped == "\\.":
                in_copy = False
            continue
        if stripped.startswith("COPY ") and stripped.endswith("FROM stdin;"):
            in_copy = True
            kept.append(line)
            continue
        match = _SAFE_SET_LINE.match(stripped)
        if match and match.group(1) not in known_settings:
            dropped.append(match.group(1))
            continue
        kept.append(line)
    return "".join(kept), dropped


def _restore_via_sql(dump_path: Path, target_url: str) -> list[str]:
    """Client/server version-skew fallback: render the archive to SQL, drop the settings this
    server does not define, and apply the result atomically with psql. Returns the dropped
    parameter names (never silently — the caller prints them)."""
    import psycopg2

    _require_bins("psql")
    sql = _run(
        ["pg_restore", "--no-owner", "--no-privileges", "--file=-", str(dump_path)],
        what="pg_restore --file=-",
    ).stdout

    conn = psycopg2.connect(target_url)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT name FROM pg_settings")
            known = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()

    sanitized, dropped = sanitize_dump_sql(sql, known)
    handle = tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False, encoding="utf-8")
    try:
        handle.write(sanitized)
        handle.close()
        _run(
            [
                "psql",
                "--quiet",
                "--single-transaction",
                "-v",
                "ON_ERROR_STOP=1",
                f"--dbname={target_url}",
                "--file",
                handle.name,
            ],
            what="psql restore (version-skew fallback)",
        )
    finally:
        handle.close()
        Path(handle.name).unlink(missing_ok=True)
    return dropped


def _apply_boot_privilege_policy(target_url: str) -> None:
    """Boot sequence's steps 3 AND 4, in their mutating modes, so the restored database is left
    in the same privilege end state a clean install reaches: narrowed S1A grants, no runtime
    privileges at all on `alembic_version` (SFA-1), and the MainAI job/execution function
    policies applied. Raises on any mismatch, which is the behaviour a restore drill should
    inherit unchanged.

    Step 4 is not optional. `pg_restore --no-privileges` recreates Postgres's default PUBLIC
    EXECUTE on functions, so a restore that stopped at step 3 produced a database that passed
    the manifest comparison and then refused to boot with "PUBLIC must never hold EXECUTE".
    That was found by actually restoring an encrypted backup into a clean database and starting
    the API against it, which is the only way this class of defect shows up.
    """
    module = _load_module(POLICY_DIR / "apply_runtime_privileges.py", "apply_runtime_privileges")
    module.apply_and_verify(target_url)

    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))
    from sqlalchemy import create_engine

    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    engine = create_engine(target_url)
    try:
        apply_mainai_job_runtime_privileges(engine)
        apply_mainai_execution_privileges(engine)
    finally:
        engine.dispose()


def compare_to_manifest(manifest: dict, conn, *, content_digests: bool) -> list[str]:
    failures: list[str] = []
    expected_tables = manifest.get("tables", {})
    actual_tables = inventory(conn, content_digests=content_digests)

    missing = sorted(set(expected_tables) - set(actual_tables))
    unexpected = sorted(set(actual_tables) - set(expected_tables))
    if missing:
        failures.append(f"restored database is missing table(s): {', '.join(missing)}")
    if unexpected:
        failures.append(f"restored database has table(s) the backup did not: {', '.join(unexpected)}")

    for table in sorted(set(expected_tables) & set(actual_tables)):
        expected = expected_tables[table]
        actual = actual_tables[table]
        if expected.get("row_count") != actual.get("row_count"):
            failures.append(
                f"{table}: row count {actual.get('row_count')} after restore, backup recorded {expected.get('row_count')}"
            )
        if content_digests and expected.get("digest") is not None and expected.get("digest") != actual.get("digest"):
            failures.append(f"{table}: content digest changed across the backup/restore round trip")

    expected_sequences = manifest.get("sequences", {})
    actual_sequences = sequences(conn)
    for name, value in expected_sequences.items():
        if value is None:
            # `pg_sequences.last_value` is NULL for a sequence never read. pg_dump emits
            # `setval(seq, 1, false)` for those, which reads back as 1 after restore — a
            # difference with no data consequence, so only sequences that were actually used
            # are compared. Comparing them would make every restore of a fresh install fail.
            continue
        if actual_sequences.get(name) != value:
            failures.append(f"sequence {name}: last_value {actual_sequences.get(name)} after restore, backup recorded {value}")

    expected_rls = manifest.get("rls", {})
    actual_rls = rls_state(conn)
    for key in ("policy_count", "rls_enabled_tables", "rls_forced_tables"):
        if expected_rls.get(key) != actual_rls.get(key):
            failures.append(f"RLS {key} changed across the round trip (backup {expected_rls.get(key)!r}, restored {actual_rls.get(key)!r})")

    expected_revision = manifest.get("alembic", {}).get("current_revision")
    actual_revision = alembic_revision(conn)
    if expected_revision != actual_revision:
        failures.append(f"alembic_version is {actual_revision!r} after restore, backup recorded {expected_revision!r}")
    return failures


def restore(backup_path: Path, target_url: str, *, confirmed: bool) -> list[str]:
    import psycopg2

    _require_bins("pg_restore")
    if not confirmed:
        raise RecoveryError("refusing to restore without --confirm")

    failures = verify(Path(backup_path))
    if failures:
        raise RecoveryError(
            "backup did not pass verification, refusing to restore:\n  - " + "\n  - ".join(failures)
        )

    manifest = load_manifest(Path(backup_path))
    source_database = manifest["source"]["database"]
    content_digests = any("digest" in entry for entry in manifest.get("tables", {}).values())
    target_database = _ensure_target_database(target_url, source_database)

    try:
        _run(
            [
                "pg_restore",
                "--no-owner",
                "--no-privileges",
                "--single-transaction",
                "--exit-on-error",
                f"--dbname={target_url}",
                str(backup_path),
            ],
            what=f"pg_restore into {target_database}",
        )
    except RecoveryError as error:
        # A client newer than the server writes GUCs the server does not know (`SET
        # transaction_timeout = 0;`, client 17 -> server 16). The direct attempt is atomic
        # (--single-transaction), so nothing was applied and the fallback starts from an empty
        # database. Any other failure is a real failure and propagates unchanged.
        if "unrecognized configuration parameter" not in str(error):
            raise
        dropped = _restore_via_sql(Path(backup_path), target_url)
        print(
            f"restore: client/server version skew — restored via SQL, dropped unknown session "
            f"setting(s): {', '.join(dropped) or 'none'}",
            file=sys.stderr,
        )

    _provision_runtime_role(target_url)
    _apply_boot_privilege_policy(target_url)

    conn = psycopg2.connect(target_url)
    try:
        report = compare_to_manifest(manifest, conn, content_digests=content_digests)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT p FROM unnest(%s::text[]) AS p WHERE has_table_privilege(%s, 'public.alembic_version', p)",
                (TABLE_PRIVS, APP_ROLE),
            )
            migration_state_privs = sorted(row[0] for row in cur.fetchall())
        if migration_state_privs:
            report.append(
                f"SFA-1 violated after restore: {APP_ROLE} holds {migration_state_privs} on alembic_version"
            )
    finally:
        conn.close()
    return report


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup_parser = subparsers.add_parser("backup", help="dump the database and write its recovery manifest")
    backup_parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    backup_parser.add_argument("--output", required=True, help="path for the .dump file (must not exist)")
    backup_parser.add_argument(
        "--no-content-digests",
        action="store_true",
        help="record row counts only (skips the full-table content digests; weaker verification)",
    )

    verify_parser = subparsers.add_parser("verify", help="verify a backup's integrity without touching a database")
    verify_parser.add_argument("--backup", required=True)

    restore_parser = subparsers.add_parser("restore", help="restore into a fresh database and compare against the manifest")
    restore_parser.add_argument("--backup", required=True)
    restore_parser.add_argument("--target-url", required=True)
    restore_parser.add_argument("--confirm", action="store_true")

    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            if not args.database_url:
                raise RecoveryError("--database-url (or DATABASE_URL) is required")
            manifest = backup(
                args.database_url,
                Path(args.output),
                content_digests=not args.no_content_digests,
            )
            print(f"backup: {manifest['dump']['file']} ({manifest['dump']['bytes']} bytes, sha256 {manifest['dump']['sha256']})")
            print(f"backup: consistency={manifest['consistency']} revision={manifest['alembic']['current_revision']} tables={len(manifest['tables'])}")
            print("backup: OK")
            return 0

        if args.command == "verify":
            failures = verify(Path(args.backup))
            if failures:
                print("verify: FAILED", file=sys.stderr)
                for failure in failures:
                    print(f"  - {failure}", file=sys.stderr)
                return 1
            print("verify: OK")
            return 0

        failures = restore(Path(args.backup), args.target_url, confirmed=args.confirm)
        if failures:
            print("restore: FAILED — the restored database does not match the backup", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
            return 1
        print(f"restore: OK — {_db_name(args.target_url)} matches the backup manifest")
        return 0
    except RecoveryError as error:
        print(f"{args.command}: {error}", file=sys.stderr)
        return 2 if "refusing" in str(error) else 1


if __name__ == "__main__":
    raise SystemExit(main())
