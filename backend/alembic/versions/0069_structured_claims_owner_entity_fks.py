"""Stage K follow-up — owner-anchored FKs for structured_claims entity refs.

Owner A must not store related_entity_id / contradicts_entity_id pointing at Owner B.
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE structured_claims
            ADD CONSTRAINT fk_structured_claims_related_entity_owner
            FOREIGN KEY (related_entity_id, owner_id)
            REFERENCES project_entities (id, owner_id)
            ON DELETE SET NULL;

        ALTER TABLE structured_claims
            ADD CONSTRAINT fk_structured_claims_contradicts_entity_owner
            FOREIGN KEY (contradicts_entity_id, owner_id)
            REFERENCES project_entities (id, owner_id)
            ON DELETE SET NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE structured_claims
            DROP CONSTRAINT IF EXISTS fk_structured_claims_contradicts_entity_owner;
        ALTER TABLE structured_claims
            DROP CONSTRAINT IF EXISTS fk_structured_claims_related_entity_owner;
        """
    )
