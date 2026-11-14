"""Bridge the two published exact-0069 database histories without relabelling either.

Founder Alpha's historical 0069 changes provider-disclosure constraints. PR #237 published a
different exact 0069 that creates MainAI STOP tables. Alembic cannot distinguish those
semantics from the version string alone. This terminal bridge fingerprints the actual schema:

* canonical Founder Alpha history: locality constraints exist, STOP tables do not;
* exact PR #237 history: both STOP tables match their published shape, locality is absent.

The latter is preserved as inert historical data, stripped of runtime-role access, and given
the missing locality transition. Ambiguous, partial, or already-combined states fail closed.
A PR #237-origin database cannot downgrade through this bridge because that would require
pretending the shared 0069 identifier has one reversible meaning.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "0074_0069_lineage_bridge"
down_revision = "0073_provider_disclosure_local"
branch_labels = None
depends_on = None

_MARKER = "_alembic_0074_0069_lineage"
_LINEAGE_CONSTRAINTS = {
    "founder_alpha_provider_disclosure": "ck_alembic_0074_origin_founder_alpha",
    "pr237_mainai_stop_controls": "ck_alembic_0074_origin_pr237",
}
_STOP_TABLES = ("mainai_stop_state", "mainai_stop_events")
_DISCLOSURE_CONSTRAINTS = {
    "ck_provider_disclosure_events_decision",
    "ck_provider_disclosure_events_sent_hash_matches_decision",
}
_STOP_COLUMNS = {
    "mainai_stop_state": {
        "id", "scope", "owner_id", "active", "reason", "sequence", "updated_at", "provenance"
    },
    "mainai_stop_events": {
        "id", "scope", "owner_id", "event_kind", "sequence", "reason", "founder_ack",
        "clear_request_id", "actor_kind", "provenance", "created_at"
    },
}
_STOP_CONSTRAINTS = {
    "mainai_stop_state": {
        "mainai_stop_state_pkey", "mainai_stop_state_owner_id_fkey", "ck_mainai_stop_scope",
        "ck_mainai_stop_owner_null",
        "ck_mainai_stop_seq", "ck_mainai_stop_prov"
    },
    "mainai_stop_events": {
        "mainai_stop_events_pkey", "ck_mainai_stop_ev_scope", "ck_mainai_stop_ev_kind",
        "ck_mainai_stop_ev_actor", "ck_mainai_stop_ev_prov"
    },
}
_STOP_INDEXES = {
    "mainai_stop_state": {
        "mainai_stop_state_pkey", "uq_mainai_stop_global", "uq_mainai_stop_owner"
    },
    "mainai_stop_events": {
        "mainai_stop_events_pkey", "uq_mainai_stop_clear_request", "ix_mainai_stop_events_owner"
    },
}


def _table_exists(bind: sa.Connection, table: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"),
            {"name": f"public.{table}"},
        ).scalar_one()
    )


def _provider_constraints(bind: sa.Connection) -> dict[str, str]:
    rows = bind.execute(
        sa.text(
            """
            SELECT conname, pg_get_constraintdef(oid)
              FROM pg_constraint
             WHERE conrelid = 'public.provider_disclosure_events'::regclass
               AND conname = ANY(:names)
            """
        ),
        {"names": list(_DISCLOSURE_CONSTRAINTS)},
    ).all()
    return {str(name): str(definition) for name, definition in rows}


def _validate_exact_pr237_shape(bind: sa.Connection) -> None:
    for table in _STOP_TABLES:
        columns = {
            str(row[0])
            for row in bind.execute(
                sa.text(
                    """
                    SELECT column_name FROM information_schema.columns
                     WHERE table_schema = 'public' AND table_name = :table
                    """
                ),
                {"table": table},
            )
        }
        if columns != _STOP_COLUMNS[table]:
            raise RuntimeError(f"{table} does not match the published PR #237 column fingerprint")
        constraints = {
            str(row[0])
            for row in bind.execute(
                sa.text(
                    """
                    SELECT conname FROM pg_constraint
                     WHERE conrelid = CAST(:table AS regclass)
                    """
                ),
                {"table": f"public.{table}"},
            )
        }
        if constraints != _STOP_CONSTRAINTS[table]:
            raise RuntimeError(f"{table} does not match the published PR #237 constraint fingerprint")
        indexes = {
            str(row[0])
            for row in bind.execute(
                sa.text(
                    """
                    SELECT indexname FROM pg_indexes
                     WHERE schemaname = 'public' AND tablename = :table
                    """
                ),
                {"table": table},
            )
        }
        if indexes != _STOP_INDEXES[table]:
            raise RuntimeError(f"{table} does not match the published PR #237 index fingerprint")


def _classify(bind: sa.Connection) -> str:
    if _table_exists(bind, _MARKER):
        raise RuntimeError("lineage marker already exists before bridge migration")
    stop_presence = tuple(_table_exists(bind, table) for table in _STOP_TABLES)
    if any(stop_presence) and not all(stop_presence):
        raise RuntimeError("partial PR #237 STOP schema is unsafe to bridge")

    constraints = _provider_constraints(bind)
    if set(constraints) != _DISCLOSURE_CONSTRAINTS:
        raise RuntimeError("provider-disclosure constraint fingerprint is incomplete")
    local_flags = {"local" in definition for definition in constraints.values()}
    if len(local_flags) != 1:
        raise RuntimeError("provider-disclosure constraints disagree about locality")
    has_local = local_flags.pop()

    if not any(stop_presence) and has_local:
        return "founder_alpha_provider_disclosure"
    if all(stop_presence) and not has_local:
        _validate_exact_pr237_shape(bind)
        return "pr237_mainai_stop_controls"
    raise RuntimeError("ambiguous exact-0069 schema effects; refusing silent combination")


def _marker_source_lineage(bind: sa.Connection) -> str:
    """Read immutable schema provenance, cross-checking mutable detail when present.

    Test and maintenance cleanup legitimately truncates application-adjacent tables.  The
    source lineage therefore lives in a migration-owned CHECK-constraint name, not solely in
    a row which can disappear.  A surviving row must agree with that schema anchor.
    """
    names = {
        str(row[0])
        for row in bind.execute(
            sa.text(
                """
                SELECT conname FROM pg_constraint
                 WHERE conrelid = CAST(:table AS regclass)
                   AND conname = ANY(:names)
                """
            ),
            {"table": f"public.{_MARKER}", "names": list(_LINEAGE_CONSTRAINTS.values())},
        )
    }
    matches = [lineage for lineage, name in _LINEAGE_CONSTRAINTS.items() if name in names]
    if len(matches) != 1:
        raise RuntimeError("lineage marker constraint is missing or ambiguous; downgrade refused")
    source_lineage = matches[0]
    row_lineage = bind.execute(
        sa.text(f"SELECT source_lineage FROM {_MARKER} WHERE singleton = 1")
    ).scalar_one_or_none()
    if row_lineage is not None and row_lineage != source_lineage:
        raise RuntimeError("lineage marker row conflicts with schema provenance; downgrade refused")
    return source_lineage


def _apply_provider_locality() -> None:
    op.execute(
        """
        ALTER TABLE provider_disclosure_events
            DROP CONSTRAINT ck_provider_disclosure_events_decision,
            ADD CONSTRAINT ck_provider_disclosure_events_decision
                CHECK (decision IN ('allowed', 'denied', 'local'));

        ALTER TABLE provider_disclosure_events
            DROP CONSTRAINT ck_provider_disclosure_events_sent_hash_matches_decision,
            ADD CONSTRAINT ck_provider_disclosure_events_sent_hash_matches_decision CHECK (
                (decision = 'denied' AND sent_content_hash IS NULL)
                OR (decision IN ('allowed', 'local') AND sent_content_hash IS NOT NULL)
            );
        """
    )


def _revoke_dormant_stop_authority() -> None:
    op.execute("REVOKE ALL ON TABLE mainai_stop_state, mainai_stop_events FROM PUBLIC")
    op.execute(
        """
        DO $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mainai_app') THEN
                REVOKE ALL ON TABLE mainai_stop_state, mainai_stop_events FROM mainai_app;
            END IF;
        END
        $guard$;
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    source_lineage = _classify(bind)
    if source_lineage == "pr237_mainai_stop_controls":
        _apply_provider_locality()
        _revoke_dormant_stop_authority()

    op.create_table(
        _MARKER,
        sa.Column("singleton", sa.Integer(), primary_key=True),
        sa.Column("source_lineage", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("applied_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("singleton = 1", name="ck_alembic_0074_singleton"),
        sa.CheckConstraint(
            "source_lineage IN ('founder_alpha_provider_disclosure', 'pr237_mainai_stop_controls')",
            name="ck_alembic_0074_source_lineage",
        ),
        sa.CheckConstraint(
            f"source_lineage = '{source_lineage}'",
            name=_LINEAGE_CONSTRAINTS[source_lineage],
        ),
    )
    detail = json.dumps(
        {
            "bridge": "explicit_exact_0069_schema_fingerprint",
            "source_lineage": source_lineage,
            "stop_tables_preserved_inert": source_lineage == "pr237_mainai_stop_controls",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    bind.execute(
        sa.text(
            f"INSERT INTO {_MARKER} (singleton, source_lineage, detail) "
            "VALUES (1, :source_lineage, :detail)"
        ),
        {"source_lineage": source_lineage, "detail": detail},
    )
    op.execute(f"REVOKE ALL ON TABLE {_MARKER} FROM PUBLIC")
    op.execute(
        f"""
        DO $guard$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mainai_app') THEN
                REVOKE ALL ON TABLE {_MARKER} FROM mainai_app;
            END IF;
        END
        $guard$;
        """
    )
    if source_lineage == "pr237_mainai_stop_controls":
        bind.execute(
            sa.text(
                """
                INSERT INTO audit_log (
                    id, user_id, action, entity_type, entity_id, ip_address, detail, created_at
                ) VALUES (
                    gen_random_uuid(), NULL, 'migration_bridge_0069_pr237',
                    'alembic_lineage', '0069', NULL, :detail, now()
                )
                """
            ),
            {"detail": detail},
        )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, _MARKER):
        raise RuntimeError("lineage marker is missing; downgrade cannot prove source history")
    source_lineage = _marker_source_lineage(bind)
    if source_lineage == "pr237_mainai_stop_controls":
        raise RuntimeError(
            "cannot downgrade bridged PR #237 history: exact revision 0069 has incompatible semantics"
        )
    if source_lineage != "founder_alpha_provider_disclosure":
        raise RuntimeError("unknown 0069 source lineage; downgrade refused")
    op.drop_table(_MARKER)
