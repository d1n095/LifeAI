"""Fail-closed repair for the known Founder Alpha dual-0069 Alembic state.

A historical database can contain both ``0069`` and
``0069_embedding_space_identity`` in ``alembic_version``.  In the corrected
serial graph the former is the direct ancestor of the latter, so retaining
both rows is invalid.  This utility removes only the redundant ancestor row,
and only after proving that both schema effects and embedding provenance are
internally consistent.

Analysis is the default.  Mutation requires ``--apply`` and runs under a
serializable transaction with an exclusive lock on ``alembic_version``.  An
append-only ``audit_log`` receipt is committed atomically with the repair.
Unknown, partial, conflicting (including PR #237's different exact ``0069``),
or otherwise ambiguous states are refused without mutation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass

import psycopg2
from psycopg2.extensions import connection as Connection

ANCESTOR = "0069"
DESCENDANT = "0069_embedding_space_identity"
BACKUP_TABLE = "_alembic_0069_embedding_space_identity_backup"
REQUIRED_DISCLOSURE_CONSTRAINTS = frozenset(
    {
        "ck_provider_disclosure_events_decision",
        "ck_provider_disclosure_events_sent_hash_matches_decision",
    }
)


class UnsafeRecoveryState(RuntimeError):
    """The database cannot be repaired without additional human evidence."""


@dataclass(frozen=True)
class RecoveryState:
    versions: tuple[str, ...]
    provenance_columns: tuple[str, ...]
    disclosure_constraints: tuple[tuple[str, str], ...]
    backup_table_present: bool
    partial_provenance_rows: int

    @property
    def is_dual_version(self) -> bool:
        return set(self.versions) == {ANCESTOR, DESCENDANT}

    @property
    def is_recovered(self) -> bool:
        return self.versions == (DESCENDANT,)


def inspect_state(conn: Connection) -> RecoveryState:
    with conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
        versions = tuple(row[0] for row in cursor.fetchall())
        cursor.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'document_chunks'
               AND column_name IN ('embedding_model', 'embedding_native_dim')
             ORDER BY column_name
            """
        )
        provenance_columns = tuple(row[0] for row in cursor.fetchall())
        cursor.execute(
            """
            SELECT conname, pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'public.provider_disclosure_events'::regclass
               AND conname = ANY(%s)
             ORDER BY conname
            """,
            (list(REQUIRED_DISCLOSURE_CONSTRAINTS),),
        )
        disclosure_constraints = tuple((row[0], row[1]) for row in cursor.fetchall())
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (f"public.{BACKUP_TABLE}",))
        backup_table_present = bool(cursor.fetchone()[0])
        partial_provenance_rows = 0
        if provenance_columns == ("embedding_model", "embedding_native_dim"):
            cursor.execute(
                """
                SELECT count(*)
                  FROM document_chunks
                 WHERE (embedding_model IS NULL) <> (embedding_native_dim IS NULL)
                """
            )
            partial_provenance_rows = int(cursor.fetchone()[0])
    return RecoveryState(
        versions=versions,
        provenance_columns=provenance_columns,
        disclosure_constraints=disclosure_constraints,
        backup_table_present=backup_table_present,
        partial_provenance_rows=partial_provenance_rows,
    )


def validate_state(state: RecoveryState) -> str:
    if not (state.is_dual_version or state.is_recovered):
        raise UnsafeRecoveryState(
            "unsupported Alembic state; expected the known dual-version state or its repaired descendant-only state"
        )
    if state.provenance_columns != ("embedding_model", "embedding_native_dim"):
        raise UnsafeRecoveryState("embedding provenance columns are missing or incomplete")
    if state.backup_table_present:
        raise UnsafeRecoveryState("migration-owned provenance backup exists; downgrade/recovery state is ambiguous")
    if state.partial_provenance_rows:
        raise UnsafeRecoveryState("partial embedding provenance rows exist; model and native dimension must be paired")
    constraints = {name: definition for name, definition in state.disclosure_constraints}
    if set(constraints) != REQUIRED_DISCLOSURE_CONSTRAINTS:
        raise UnsafeRecoveryState("provider-disclosure constraint set is incomplete")
    if any("local" not in definition for definition in constraints.values()):
        raise UnsafeRecoveryState("exact revision 0069 does not have the expected provider-disclosure semantics")
    return "already_recovered" if state.is_recovered else "repairable"


def recover(conn: Connection, *, apply: bool) -> dict[str, object]:
    conn.set_session(isolation_level="SERIALIZABLE", readonly=not apply, autocommit=False)
    try:
        with conn.cursor() as cursor:
            cursor.execute("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE")
        before = inspect_state(conn)
        classification = validate_state(before)
        if not apply or classification == "already_recovered":
            conn.rollback()
            return {
                "status": classification,
                "applied": False,
                "state": asdict(before),
            }

        receipt_id = uuid.uuid4()
        after_versions = (DESCENDANT,)
        detail = json.dumps(
            {
                "recovery": "collapse_redundant_0069_ancestor",
                "before_versions": before.versions,
                "after_versions": after_versions,
                "provenance_columns": before.provenance_columns,
                "partial_provenance_rows": before.partial_provenance_rows,
                "backup_table_present": before.backup_table_present,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM alembic_version WHERE version_num = %s", (ANCESTOR,))
            if cursor.rowcount != 1:
                raise UnsafeRecoveryState("locked ancestor revision changed before repair")
            cursor.execute(
                """
                INSERT INTO audit_log (
                    id, user_id, action, entity_type, entity_id, ip_address, detail, created_at
                ) VALUES (%s, NULL, %s, %s, %s, NULL, %s, now())
                """,
                (
                    str(receipt_id),
                    "migration_recovery_0069_dual_version",
                    "alembic_version",
                    f"{ANCESTOR}+{DESCENDANT}",
                    detail,
                ),
            )
        after = inspect_state(conn)
        if not after.is_recovered:
            raise UnsafeRecoveryState("post-repair Alembic state is not the expected descendant-only state")
        validate_state(after)
        conn.commit()
        return {
            "status": "repaired",
            "applied": True,
            "receipt_id": str(receipt_id),
            "before": asdict(before),
            "after": asdict(after),
        }
    except Exception:
        conn.rollback()
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--apply", action="store_true", help="apply the verified repair; default is analysis only")
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("--database-url or DATABASE_URL is required")
    try:
        with psycopg2.connect(args.database_url) as conn:
            result = recover(conn, apply=args.apply)
    except UnsafeRecoveryState as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
