"""MainAI Dynamic Workforce Orchestration + Capability Learning -- durable Capability Mastery
Ledger. See docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md; rebased after 0078_founder_boot for founder-boot integration.

Ports the verified Coverage/Workforce tables and the verified Vision vocabulary widening needed
by `mainai_vision.gap_generator` (`implied_requirement` and related node/edge types). Existing
rows are not rewritten, but CHECK constraints on project-entity vocabulary are widened before the
new workforce tables are created. `mainai_workforce_mastery_events` is append-only, reusing the
EXISTING `intelligence_governance_deny_mutation()` trigger function (migration 0038) verbatim.
`mainai_workforce_capability_mastery` is the live, mutable "current state" row -- same "live row
+ append-only event log" split as `capability_records`/`capability_observation_events` (migration
0048), a DIFFERENT axis (task-class autonomy STAGE relative to an external teacher, not "can this
be invoked at all")."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0079_mainai_workforce_mastery"
down_revision = "0078_founder_boot"
branch_labels = None
depends_on = None

_OLD_ENTITY_TYPES = ("idea", "decision", "task_reference", "vision_statement", "open_question")
_NEW_ENTITY_TYPES = (
    "domain", "capability", "system", "subsystem", "requirement", "implied_requirement",
    "invariant", "risk", "acceptance_criterion", "verification_criterion",
)
_OLD_RELATIONSHIP_TYPES = ("relates_to", "supersedes", "contradicts", "blocks", "answers", "duplicates", "derived_from")
_NEW_RELATIONSHIP_TYPES = ("depends_on", "implies", "verifies", "satisfies", "mitigates")


def _csv(values: tuple[str, ...]) -> str:
    return ",".join(repr(v) for v in values)


def _widen_vision_vocabulary() -> None:
    all_entity_types = _OLD_ENTITY_TYPES + _NEW_ENTITY_TYPES
    all_relationship_types = _OLD_RELATIONSHIP_TYPES + _NEW_RELATIONSHIP_TYPES
    op.execute("ALTER TABLE project_entities DROP CONSTRAINT ck_project_entities_entity_type")
    op.execute(
        "ALTER TABLE project_entities ADD CONSTRAINT ck_project_entities_entity_type "
        f"CHECK (entity_type IN ({_csv(all_entity_types)}))"
    )
    op.execute("ALTER TABLE project_entity_relationships DROP CONSTRAINT ck_project_entity_relationships_type")
    op.execute(
        "ALTER TABLE project_entity_relationships ADD CONSTRAINT ck_project_entity_relationships_type "
        f"CHECK (relationship_type IN ({_csv(all_relationship_types)}))"
    )
    op.execute("ALTER TABLE interpretation_proposals DROP CONSTRAINT ck_interpretation_proposals_entity_type")
    op.execute(
        "ALTER TABLE interpretation_proposals ADD CONSTRAINT ck_interpretation_proposals_entity_type "
        f"CHECK (proposed_entity_type IN ({_csv(all_entity_types)}))"
    )


def _restore_old_vision_vocabulary() -> None:
    op.execute("ALTER TABLE interpretation_proposals DROP CONSTRAINT ck_interpretation_proposals_entity_type")
    op.execute(
        "ALTER TABLE interpretation_proposals ADD CONSTRAINT ck_interpretation_proposals_entity_type "
        f"CHECK (proposed_entity_type IN ({_csv(_OLD_ENTITY_TYPES)}))"
    )
    op.execute("ALTER TABLE project_entity_relationships DROP CONSTRAINT ck_project_entity_relationships_type")
    op.execute(
        "ALTER TABLE project_entity_relationships ADD CONSTRAINT ck_project_entity_relationships_type "
        f"CHECK (relationship_type IN ({_csv(_OLD_RELATIONSHIP_TYPES)}))"
    )
    op.execute("ALTER TABLE project_entities DROP CONSTRAINT ck_project_entities_entity_type")
    op.execute(
        "ALTER TABLE project_entities ADD CONSTRAINT ck_project_entities_entity_type "
        f"CHECK (entity_type IN ({_csv(_OLD_ENTITY_TYPES)}))"
    )


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""CREATE POLICY {table}_owner ON {table}
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)"""
    )
    op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO mainai_app")


def upgrade() -> None:
    _widen_vision_vocabulary()

    op.create_table(
        "mainai_workforce_capability_mastery",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability_key", sa.String(128), nullable=False),
        sa.Column("task_class", sa.String(128), nullable=False),
        sa.Column("external_teacher", sa.String(64), nullable=False),
        sa.Column("stage", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("observation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("local_practice_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("local_success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("examiner_pass_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("examiner_fail_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rework_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("distinct_task_diversity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("common_failure_modes", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("context_requirement", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=True),
        sa.Column("latency_seconds", sa.Numeric(10, 2), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(160), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint("stage BETWEEN 0 AND 6", name="ck_workforce_mastery_stage"),
        sa.CheckConstraint("length(btrim(capability_key)) > 0", name="ck_workforce_mastery_capability_key"),
        sa.CheckConstraint("length(btrim(task_class)) > 0", name="ck_workforce_mastery_task_class"),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_workforce_mastery_idem"),
        sa.UniqueConstraint("id", "owner_id", name="uq_workforce_mastery_id_owner"),
        sa.UniqueConstraint("owner_id", "capability_key", "task_class", "external_teacher", name="uq_workforce_mastery_identity"),
    )
    op.create_index("ix_workforce_mastery_owner_capability", "mainai_workforce_capability_mastery", ["owner_id", "capability_key"])
    _secure("mainai_workforce_capability_mastery")

    op.create_table(
        "mainai_workforce_mastery_events",
        sa.Column("id", sa.UUID(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("owner_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("capability_mastery_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(24), nullable=False),
        sa.Column("previous_stage", sa.SmallInteger(), nullable=True),
        sa.Column("new_stage", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "event_type IN ('observation','promotion','demotion','decay')",
            name="ck_workforce_mastery_events_type",
        ),
        sa.CheckConstraint("length(btrim(reason)) > 0", name="ck_workforce_mastery_events_reason"),
        sa.ForeignKeyConstraint(
            ["capability_mastery_id", "owner_id"],
            ["mainai_workforce_capability_mastery.id", "mainai_workforce_capability_mastery.owner_id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_workforce_mastery_events_owner_mastery", "mainai_workforce_mastery_events", ["owner_id", "capability_mastery_id", "recorded_at"])
    _secure("mainai_workforce_mastery_events")

    # Append-only: reuses the EXISTING trigger function from migration 0038, never redefined.
    op.execute(
        """CREATE TRIGGER trg_mainai_workforce_mastery_events_deny_mutation
        BEFORE UPDATE OR DELETE ON mainai_workforce_mastery_events
        FOR EACH ROW EXECUTE FUNCTION intelligence_governance_deny_mutation()"""
    )
    op.execute("REVOKE UPDATE, DELETE ON mainai_workforce_mastery_events FROM mainai_app")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_mainai_workforce_mastery_events_deny_mutation ON mainai_workforce_mastery_events")
    op.drop_table("mainai_workforce_mastery_events")
    op.drop_table("mainai_workforce_capability_mastery")
    _restore_old_vision_vocabulary()
