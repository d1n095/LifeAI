"""Allow the `local` disclosure decision on provider_disclosure_events — migration 0069.

0062 created the egress disclosure ledger with decision CHECK-constrained to ('allowed',
'denied'). A call to an ON-HOST local model runner (Ollama on loopback / in the same
deployment — see app/providers/ollama_provider.py's is_external_egress) is still fully gated
(incomplete-request and NEVER_EGRESS denies, plus secret redaction, all apply) but never left
the host, so recording it as an external-AI `allowed` disclosure is untrue. This adds a third
truthful decision value, `local`, so a local-first ingestion produces zero external `allowed`
rows — a checkable no-egress invariant.

`local` behaves like `allowed` for the sent-hash invariant (content was processed, so
sent_content_hash IS NOT NULL); only `denied` has a NULL sent hash. The append-only trigger,
RLS policy and redaction_categories constraint from 0062 are untouched.
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
    # Restores the original two-value constraints. Any pre-existing `local` rows would violate
    # the stricter decision CHECK — an intentional downgrade guard, not a silent data change.
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
