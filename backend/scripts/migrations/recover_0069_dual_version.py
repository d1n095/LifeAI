"""Fail-closed repair for the known Founder Alpha dual-0069 Alembic state.

Mutation removes only the redundant ancestor row, under a serializable transaction, after
proving the exact published canonical schema. PR #237 STOP objects, altered column shapes,
same-named but different constraints, unvalidated constraints and partial provenance are all
refused. No schema is stamped or relabelled by this utility.
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
STOP_TABLES = ("mainai_stop_state", "mainai_stop_events")
REQUIRED_DISCLOSURE_CONSTRAINTS = frozenset(
    {
        "ck_provider_disclosure_events_decision",
        "ck_provider_disclosure_events_sent_hash_matches_decision",
    }
)
EXPECTED_PROVENANCE_SHAPE = (
    ("embedding_model", "text", False, None),
    ("embedding_native_dim", "integer", False, None),
)


class UnsafeRecoveryState(RuntimeError):
    """The database cannot be repaired without additional human evidence."""


@dataclass(frozen=True)
class RecoveryState:
    versions: tuple[str, ...]
    provenance_columns: tuple[str, ...]
    provenance_shape: tuple[tuple[object, ...], ...]
    disclosure_constraints: tuple[tuple[str, str, bool, str], ...]
    disclosure_constraints_exact: bool
    stop_tables_present: tuple[str, ...]
    backup_table_present: bool
    partial_provenance_rows: int

    @property
    def is_dual_version(self) -> bool:
        return set(self.versions) == {ANCESTOR, DESCENDANT}

    @property
    def is_recovered(self) -> bool:
        return self.versions == (DESCENDANT,)


def _constraint_rows(cursor, relation: str) -> tuple[tuple[str, str, bool, str], ...]:
    cursor.execute(
        """
        SELECT conname, contype, convalidated, pg_get_constraintdef(oid, true)
          FROM pg_constraint
         WHERE conrelid = to_regclass(%s)
           AND conname = ANY(%s)
         ORDER BY conname
        """,
        (relation, list(REQUIRED_DISCLOSURE_CONSTRAINTS)),
    )
    return tuple((str(a), str(b), bool(c), str(d)) for a, b, c, d in cursor.fetchall())


def _expected_disclosure_constraints(cursor) -> tuple[tuple[str, str, bool, str], ...]:
    cursor.execute("DROP TABLE IF EXISTS pg_temp._expected_recovery_disclosure_0069")
    cursor.execute(
        """
        CREATE TEMP TABLE _expected_recovery_disclosure_0069 (
            decision varchar(16) NOT NULL,
            sent_content_hash varchar(64),
            CONSTRAINT ck_provider_disclosure_events_decision
                CHECK (decision IN ('allowed', 'denied', 'local')),
            CONSTRAINT ck_provider_disclosure_events_sent_hash_matches_decision CHECK (
                (decision = 'denied' AND sent_content_hash IS NULL)
                OR (decision IN ('allowed', 'local') AND sent_content_hash IS NOT NULL)
            )
        ) ON COMMIT DROP
        """
    )
    return _constraint_rows(cursor, "pg_temp._expected_recovery_disclosure_0069")


def inspect_state(conn: Connection) -> RecoveryState:
    with conn.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
        versions = tuple(row[0] for row in cursor.fetchall())
        cursor.execute(
            """
            SELECT a.attname,
                   format_type(a.atttypid, a.atttypmod),
                   a.attnotnull,
                   pg_get_expr(d.adbin, d.adrelid, true)
              FROM pg_attribute a
              LEFT JOIN pg_attrdef d
                ON d.adrelid = a.attrelid AND d.adnum = a.attnum
             WHERE a.attrelid = 'public.document_chunks'::regclass
               AND a.attname IN ('embedding_model', 'embedding_native_dim')
               AND a.attnum > 0
               AND NOT a.attisdropped
             ORDER BY a.attnum
            """
        )
        provenance_shape = tuple(tuple(row) for row in cursor.fetchall())
        provenance_columns = tuple(str(row[0]) for row in provenance_shape)

        disclosure_constraints = _constraint_rows(cursor, "public.provider_disclosure_events")
        expected_constraints = _expected_disclosure_constraints(cursor)

        cursor.execute(
            """
            SELECT name
              FROM unnest(%s::text[]) AS candidate(name)
             WHERE to_regclass('public.' || name) IS NOT NULL
             ORDER BY name
            """,
            (list(STOP_TABLES),),
        )
        stop_tables_present = tuple(str(row[0]) for row in cursor.fetchall())
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
        provenance_shape=provenance_shape,
        disclosure_constraints=disclosure_constraints,
        disclosure_constraints_exact=disclosure_constraints == expected_constraints,
        stop_tables_present=stop_tables_present,
        backup_table_present=backup_table_present,
        partial_provenance_rows=partial_provenance_rows,
    )


def validate_state(state: RecoveryState) -> str:
    if not (state.is_dual_version or state.is_recovered):
        raise UnsafeRecoveryState(
            "unsupported Alembic state; expected the known dual-version state or its repaired descendant-only state"
        )
    if state.stop_tables_present:
        raise UnsafeRecoveryState(
            "conflicting PR #237 STOP history is present; refusing to collapse exact-0069 revisions"
        )
    if state.provenance_shape != EXPECTED_PROVENANCE_SHAPE:
        raise UnsafeRecoveryState(
            "embedding provenance columns do not match the published type/nullability/default fingerprint"
        )
    if state.backup_table_present:
        raise UnsafeRecoveryState("migration-owned provenance backup exists; downgrade/recovery state is ambiguous")
    if state.partial_provenance_rows:
        raise UnsafeRecoveryState("partial embedding provenance rows exist; model and native dimension must be paired")
    if not state.disclosure_constraints_exact:
        raise UnsafeRecoveryState(
            "provider-disclosure constraints do not match the complete validated canonical 0069 definitions"
        )
    return "already_recovered" if state.is_recovered else "repairable"


def recover(conn: Connection, *, apply: bool) -> dict[str, object]:
    conn.set_session(isolation_level="SERIALIZABLE", readonly=False, autocommit=False)
    try:
        with conn.cursor() as cursor:
            cursor.execute("LOCK TABLE alembic_version IN ACCESS EXCLUSIVE MODE")
            cursor.execute("LOCK TABLE document_chunks, provider_disclosure_events IN SHARE MODE")
        before = inspect_state(conn)
        classification = validate_state(before)
        if not apply or classification == "already_recovered":
            conn.rollback()
            return {"status": classification, "applied": False, "state": asdict(before)}

        receipt_id = uuid.uuid4()
        after_versions = (DESCENDANT,)
        detail = json.dumps(
            {
                "recovery": "collapse_redundant_0069_ancestor",
                "before_versions": before.versions,
                "after_versions": after_versions,
                "provenance_shape": before.provenance_shape,
                "partial_provenance_rows": before.partial_provenance_rows,
                "backup_table_present": before.backup_table_present,
                "stop_tables_present": before.stop_tables_present,
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
