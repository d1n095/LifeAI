"""Historical provider-disclosure locality revision — compatibility revision 0069.

PR #326 shipped this exact revision identifier and schema transition before the composed
Founder Alpha lineage renamed the same transition to 0073. Databases that recorded ``0069``
must continue from that authoritative history; treating the value as an abbreviation of
``0069_embedding_space_identity`` silently skipped the embedding-provenance migration.

Restoring the historical revision as a real ancestor makes both paths identical:

* fresh install: 0068 -> 0069 -> 0069_embedding_space_identity -> ... -> 0073;
* historical #326 database: recorded 0069 -> 0069_embedding_space_identity -> ... -> 0073.

0073 remains as a no-op compatibility marker because databases already at the frozen #352
head record that identifier. The locality constraint itself is owned here, at its original
revision, so downgrade semantics also remain historically truthful.
"""

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
    # Existing local rows intentionally block this downgrade rather than being deleted or
    # relabelled. That is the historical #326 behavior and preserves audit truth.
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
