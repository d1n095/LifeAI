"""Founder email recovery requests (migration 0069).

One table backing the secure founder account recovery flow (app/founder_recovery.py):
a pending email-change request with a single-use, hashed verification token. The row is
owner-scoped (user_id FK) and carries no secrets — only the SHA-256 token hash.

Reconciliation note (R15 staging merge, 2026-11-21): originally authored as revision 0069
(down_revision=0068) in the Z.AI consultation lane's own disjoint numbering. Renumbered to
0080 and re-chained onto 0079 ("audit_log becomes append-only") here, per this file's own
prior note that the composition tooling must do so when the lanes merge — it adds one
independent table (`founder_email_change_requests`) and conflicts with nothing 0079 does.
"""

from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE founder_email_change_requests (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            new_email varchar(320) NOT NULL,
            old_email varchar(320) NOT NULL,
            token_hash varchar(64) NOT NULL UNIQUE,
            created_at timestamp NOT NULL DEFAULT now(),
            expires_at timestamp NOT NULL,
            used_at timestamp NULL
        )
    """)
    op.execute("CREATE INDEX ix_founder_email_change_requests_user_id ON founder_email_change_requests (user_id)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS founder_email_change_requests")
