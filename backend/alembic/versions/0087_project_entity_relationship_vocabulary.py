"""Restore the union of Concept Reconciliation and Vision relationship types.

Revision ID: 0087_relationship_vocabulary
Revises: 0086_account_erasure_reauth
Create Date: 2026-09-23
"""

from alembic import op

revision = "0087_relationship_vocabulary"
down_revision = "0086_account_erasure_reauth"
branch_labels = None
depends_on = None

_PREVIOUS_TYPES = (
    "relates_to",
    "supersedes",
    "contradicts",
    "blocks",
    "answers",
    "duplicates",
    "derived_from",
    "depends_on",
    "implies",
    "verifies",
    "satisfies",
    "mitigates",
)

_RESTORED_CONCEPT_TYPES = (
    "same",
    "partial_overlap",
    "related",
    "extends",
    "alternative",
    "reuses",
)

_ALL_TYPES = (
    "same",
    "partial_overlap",
    "related",
    "depends_on",
    "contradicts",
    "supersedes",
    "extends",
    "alternative",
    "reuses",
    "relates_to",
    "blocks",
    "answers",
    "duplicates",
    "derived_from",
    "implies",
    "verifies",
    "satisfies",
    "mitigates",
)


def _csv(values: tuple[str, ...]) -> str:
    return ",".join(repr(value) for value in values)


def upgrade() -> None:
    op.execute("ALTER TABLE project_entity_relationships DROP CONSTRAINT ck_project_entity_relationships_type")
    op.execute(
        "ALTER TABLE project_entity_relationships ADD CONSTRAINT ck_project_entity_relationships_type "
        f"CHECK (relationship_type IN ({_csv(_ALL_TYPES)}))"
    )


def downgrade() -> None:
    # Returning to 0086 is only truthful when every row fits 0086's narrower vocabulary.
    # Refuse before touching the current constraint when restored Concept rows exist; deleting
    # or remapping those rows would corrupt user-authored semantic relationships.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (
            SELECT 1
            FROM project_entity_relationships
            WHERE relationship_type IN ('same','partial_overlap','related','extends','alternative','reuses')
          ) THEN
            RAISE EXCEPTION
              'cannot downgrade to 0086: project entity relationships use restored Concept Reconciliation values';
          END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE project_entity_relationships DROP CONSTRAINT ck_project_entity_relationships_type")
    op.execute(
        "ALTER TABLE project_entity_relationships ADD CONSTRAINT ck_project_entity_relationships_type "
        f"CHECK (relationship_type IN ({_csv(_PREVIOUS_TYPES)}))"
    )
