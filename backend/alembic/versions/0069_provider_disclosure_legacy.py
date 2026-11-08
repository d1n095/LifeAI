"""Preserve the historical standalone #326 revision and its migration history."""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
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
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE provider_disclosure_events
            DROP CONSTRAINT ck_provider_disclosure_events_sent_hash_matches_decision,
            ADD CONSTRAINT ck_provider_disclosure_events_sent_hash_matches_decision CHECK (
                (decision = 'denied' AND sent_content_hash IS NULL)
                OR (decision = 'allowed' AND sent_content_hash IS NOT NULL)
            );

        ALTER TABLE provider_disclosure_events
            DROP CONSTRAINT ck_provider_disclosure_events_decision,
            ADD CONSTRAINT ck_provider_disclosure_events_decision
                CHECK (decision IN ('allowed', 'denied'));
    """)
