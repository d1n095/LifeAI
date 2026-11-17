"""Advertising subsystem tables (migration 0071).

Two append-only tables backing app/ads/ — the ISOLATED advertising layer for a future
ad-funded free tier:

* ad_consent_events — per-user consent trail (grant/deny/withdraw), GDPR-conscious: the
  LATEST row per user decides current state, and the full history is retained for
  demonstrability. No third-party identifiers.
* ad_events — impressions and clicks with NO content payload: placement id, creative id,
  pseudonymous session hash, subject ids. There is deliberately NO column that could
  reference a conversation, memory, or any private context (enforced by tests), and no
  advertiser-controlled free-text fields.

NOTE FOR MIGRATION RECONCILIATION: the founder-alpha composition lanes number their
migrations 0069..0079 in a DISJOINT history. This revision (0071, down_revision=0070)
belongs to the Z.AI lane lineage and must be renumbered/re-chained by the composition
tooling when the lanes merge — it adds independent tables and conflicts with nothing.
"""

from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ad_consent_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            decision varchar(16) NOT NULL CHECK (decision IN ('granted', 'denied', 'withdrawn')),
            personalized boolean NOT NULL DEFAULT false,
            region varchar(8) NULL,
            policy_version varchar(32) NOT NULL,
            decided_at timestamp NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_ad_consent_events_user ON ad_consent_events (user_id, decided_at)")

    op.execute("""
        CREATE TABLE ad_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NULL REFERENCES users(id) ON DELETE SET NULL,
            tenant_id uuid NULL REFERENCES billing_tenants(id) ON DELETE SET NULL,
            placement varchar(64) NOT NULL,
            event varchar(16) NOT NULL CHECK (event IN ('impression', 'click')),
            creative_id varchar(128) NOT NULL,
            session_hash varchar(64) NOT NULL,
            created_at timestamp NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_ad_events_subject ON ad_events (user_id, created_at)")
    op.execute("CREATE INDEX ix_ad_events_placement ON ad_events (placement, event, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS ad_events")
    op.execute("DROP TABLE IF EXISTS ad_consent_events")
