"""Bridge the two published exact-0069 database histories without relabelling either.

The shared revision string is not authority.  This migration compares the live PostgreSQL
catalog with temporary tables built from the two published schemas.  Column types, nullability,
defaults, complete CHECK/FK definitions, validation state, indexes, predicates, uniqueness and
validity must all match.  Unknown, partial, combined or merely same-named objects fail closed.
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


def _table_exists(bind: sa.Connection, table: str) -> bool:
    return bool(
        bind.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"),
            {"name": f"public.{table}"},
        ).scalar_one()
    )


def _column_fingerprint(bind: sa.Connection, relation: str) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in bind.execute(
            sa.text(
                """
                SELECT a.attname,
                       format_type(a.atttypid, a.atttypmod),
                       a.attnotnull,
                       pg_get_expr(d.adbin, d.adrelid, true)
                  FROM pg_attribute a
                  LEFT JOIN pg_attrdef d
                    ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                 WHERE a.attrelid = to_regclass(:relation)
                   AND a.attnum > 0
                   AND NOT a.attisdropped
                 ORDER BY a.attnum
                """
            ),
            {"relation": relation},
        ).all()
    )


def _constraint_fingerprint(
    bind: sa.Connection,
    relation: str,
    *,
    names: set[str] | None = None,
) -> dict[str, tuple[str, bool, str]]:
    rows = bind.execute(
        sa.text(
            """
            SELECT conname, contype, convalidated, pg_get_constraintdef(oid, true)
              FROM pg_constraint
             WHERE conrelid = to_regclass(:relation)
               AND (:all_names OR conname = ANY(:names))
             ORDER BY conname
            """
        ),
        {
            "relation": relation,
            "all_names": names is None,
            "names": sorted(names or {"__all__"}),
        },
    ).all()
    return {
        str(name): (str(kind), bool(validated), str(definition))
        for name, kind, validated, definition in rows
    }


def _index_fingerprint(bind: sa.Connection, relation: str) -> dict[str, tuple[object, ...]]:
    rows = bind.execute(
        sa.text(
            """
            SELECT index_class.relname,
                   idx.indisunique,
                   idx.indisprimary,
                   idx.indisvalid,
                   idx.indisready,
                   access_method.amname,
                   regexp_replace(
                       pg_get_indexdef(idx.indexrelid),
                       ' ON (ONLY )?[^ ]+ ',
                       ' ON \\1<table> '
                   ) AS normalized_definition
              FROM pg_index idx
              JOIN pg_class index_class ON index_class.oid = idx.indexrelid
              JOIN pg_class table_class ON table_class.oid = idx.indrelid
              JOIN pg_am access_method ON access_method.oid = index_class.relam
             WHERE table_class.oid = to_regclass(:relation)
             ORDER BY index_class.relname
            """
        ),
        {"relation": relation},
    ).all()
    return {
        str(name): (
            bool(unique),
            bool(primary),
            bool(valid),
            bool(ready),
            str(method),
            str(definition),
        )
        for name, unique, primary, valid, ready, method, definition in rows
    }


def _create_expected_disclosure_tables(bind: sa.Connection) -> None:
    bind.execute(
        sa.text(
            """
            CREATE TEMP TABLE _expected_disclosure_pre_0074 (
                decision varchar(16) NOT NULL,
                sent_content_hash varchar(64),
                CONSTRAINT ck_provider_disclosure_events_decision
                    CHECK (decision IN ('allowed', 'denied')),
                CONSTRAINT ck_provider_disclosure_events_sent_hash_matches_decision CHECK (
                    (decision = 'denied' AND sent_content_hash IS NULL)
                    OR (decision = 'allowed' AND sent_content_hash IS NOT NULL)
                )
            ) ON COMMIT DROP;
            CREATE TEMP TABLE _expected_disclosure_local_0074 (
                decision varchar(16) NOT NULL,
                sent_content_hash varchar(64),
                CONSTRAINT ck_provider_disclosure_events_decision
                    CHECK (decision IN ('allowed', 'denied', 'local')),
                CONSTRAINT ck_provider_disclosure_events_sent_hash_matches_decision CHECK (
                    (decision = 'denied' AND sent_content_hash IS NULL)
                    OR (decision IN ('allowed', 'local') AND sent_content_hash IS NOT NULL)
                )
            ) ON COMMIT DROP;
            """
        )
    )


def _provider_constraint_state(bind: sa.Connection) -> str:
    _create_expected_disclosure_tables(bind)
    actual = _constraint_fingerprint(
        bind,
        "public.provider_disclosure_events",
        names=_DISCLOSURE_CONSTRAINTS,
    )
    if set(actual) != _DISCLOSURE_CONSTRAINTS:
        raise RuntimeError("provider-disclosure constraint fingerprint is incomplete")
    pre = _constraint_fingerprint(
        bind,
        "pg_temp._expected_disclosure_pre_0074",
        names=_DISCLOSURE_CONSTRAINTS,
    )
    local = _constraint_fingerprint(
        bind,
        "pg_temp._expected_disclosure_local_0074",
        names=_DISCLOSURE_CONSTRAINTS,
    )
    if actual == pre:
        return "pre_locality"
    if actual == local:
        return "locality"
    raise RuntimeError("provider-disclosure constraints do not match a published exact-0069 state")


def _create_expected_pr237_tables(bind: sa.Connection) -> None:
    bind.execute(
        sa.text(
            """
            CREATE TEMP TABLE _expected_mainai_stop_state_0074 (
                id uuid CONSTRAINT mainai_stop_state_pkey PRIMARY KEY DEFAULT gen_random_uuid(),
                scope varchar(16) NOT NULL,
                owner_id uuid,
                active boolean NOT NULL DEFAULT false,
                reason text NOT NULL DEFAULT '',
                sequence bigint NOT NULL DEFAULT 0,
                updated_at timestamp NOT NULL DEFAULT now(),
                provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
                CONSTRAINT ck_mainai_stop_scope CHECK (scope IN ('owner', 'global')),
                CONSTRAINT ck_mainai_stop_owner_null CHECK (
                    (scope = 'global' AND owner_id IS NULL)
                    OR (scope = 'owner' AND owner_id IS NOT NULL)
                ),
                CONSTRAINT ck_mainai_stop_seq CHECK (sequence >= 0),
                CONSTRAINT ck_mainai_stop_prov_object
                    CHECK (jsonb_typeof(provenance) = 'object')
            ) ON COMMIT DROP;
            CREATE UNIQUE INDEX uq_mainai_stop_global
                ON _expected_mainai_stop_state_0074 ((scope)) WHERE scope = 'global';
            CREATE UNIQUE INDEX uq_mainai_stop_owner
                ON _expected_mainai_stop_state_0074 (owner_id) WHERE scope = 'owner';

            CREATE TEMP TABLE _expected_mainai_stop_events_0074 (
                id uuid CONSTRAINT mainai_stop_events_pkey PRIMARY KEY DEFAULT gen_random_uuid(),
                scope varchar(16) NOT NULL,
                owner_id uuid,
                event_kind varchar(32) NOT NULL,
                sequence bigint NOT NULL,
                reason text NOT NULL,
                founder_ack text,
                clear_request_id uuid,
                actor_kind varchar(32) NOT NULL,
                provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at timestamp NOT NULL DEFAULT now(),
                CONSTRAINT ck_mainai_stop_ev_scope CHECK (scope IN ('owner', 'global')),
                CONSTRAINT ck_mainai_stop_ev_kind CHECK (
                    event_kind IN ('activate', 'clear', 'reject_clear', 'boot_blocked')
                ),
                CONSTRAINT ck_mainai_stop_ev_actor CHECK (
                    actor_kind IN ('founder', 'operator', 'system')
                ),
                CONSTRAINT ck_mainai_stop_ev_prov
                    CHECK (jsonb_typeof(provenance) = 'object')
            ) ON COMMIT DROP;
            CREATE UNIQUE INDEX uq_mainai_stop_clear_request
                ON _expected_mainai_stop_events_0074 (clear_request_id)
                WHERE clear_request_id IS NOT NULL;
            CREATE INDEX ix_mainai_stop_events_owner
                ON _expected_mainai_stop_events_0074 (owner_id, created_at DESC);
            """
        )
    )


def _validate_exact_pr237_shape(bind: sa.Connection) -> None:
    _create_expected_pr237_tables(bind)
    expected_relations = {
        "mainai_stop_state": "pg_temp._expected_mainai_stop_state_0074",
        "mainai_stop_events": "pg_temp._expected_mainai_stop_events_0074",
    }
    for table, expected in expected_relations.items():
        actual_relation = f"public.{table}"
        if _column_fingerprint(bind, actual_relation) != _column_fingerprint(bind, expected):
            raise RuntimeError(f"{table} does not match the published PR #237 column fingerprint")
        expected_constraints = _constraint_fingerprint(bind, expected)
        if table == "mainai_stop_state":
            # PostgreSQL does not permit a temporary table to reference a permanent table.
            # The published FK is therefore added to the expected catalog fingerprint here;
            # every other constraint is generated by PostgreSQL from the published DDL above.
            expected_constraints["mainai_stop_state_owner_id_fkey"] = (
                "f",
                True,
                "FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE CASCADE",
            )
        if _constraint_fingerprint(bind, actual_relation) != expected_constraints:
            raise RuntimeError(f"{table} does not match the published PR #237 constraint fingerprint")
        if _index_fingerprint(bind, actual_relation) != _index_fingerprint(bind, expected):
            raise RuntimeError(f"{table} does not match the published PR #237 index fingerprint")


def _classify(bind: sa.Connection) -> str:
    if _table_exists(bind, _MARKER):
        raise RuntimeError("lineage marker already exists before bridge migration")
    stop_presence = tuple(_table_exists(bind, table) for table in _STOP_TABLES)
    if any(stop_presence) and not all(stop_presence):
        raise RuntimeError("partial PR #237 STOP schema is unsafe to bridge")

    disclosure_state = _provider_constraint_state(bind)
    if not any(stop_presence) and disclosure_state == "locality":
        return "founder_alpha_provider_disclosure"
    if all(stop_presence) and disclosure_state == "pre_locality":
        _validate_exact_pr237_shape(bind)
        return "pr237_mainai_stop_controls"
    raise RuntimeError("ambiguous exact-0069 schema effects; refusing silent combination")


def _marker_source_lineage(bind: sa.Connection) -> str:
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
            "bridge": "exact_catalog_fingerprint",
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
