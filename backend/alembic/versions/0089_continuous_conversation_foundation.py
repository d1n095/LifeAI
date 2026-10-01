"""MainAI continuous conversation foundation.

One canonical founder↔MainAI thread plus an append-only turn log. Conversation
state does not grant merge, deploy, Recall, or RLS authority.

Revision ID: 0089_continuous_conversation
Revises: 0088_erasure_completion_phase
Create Date: 2026-10-01

Alembic note: the in-flight orchestration-truth-ledger lane also parents 0088 as
`0089_orchestration_truth_ledger`. Those two 0089 revisions must not be merged
onto each other until a later merge revision joins both heads. This branch keeps
a single head. `alembic_version.version_num` is varchar(32), so this id is the
short form of "continuous conversation foundation".
"""

from alembic import op

revision = "0089_continuous_conversation"
down_revision = "0088_erasure_completion_phase"
branch_labels = None
depends_on = None

OWNER_SCOPED_TABLES = (
    "founder_canonical_conversations",
    "founder_conversation_events",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE founder_canonical_conversations (
            owner_id uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE RESTRICT,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_founder_canonical_conversations_conversation UNIQUE (conversation_id)
        );

        CREATE TABLE founder_conversation_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            conversation_id uuid REFERENCES conversations(id) ON DELETE CASCADE,
            message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
            direction varchar(16) NOT NULL,
            kind varchar(64) NOT NULL,
            suppressed boolean NOT NULL DEFAULT false,
            interrupt boolean NOT NULL DEFAULT false,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            excerpt text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_founder_conversation_events_direction CHECK (direction IN ('inbound','outbound','internal')),
            CONSTRAINT ck_founder_conversation_events_payload CHECK (jsonb_typeof(payload) = 'object')
        );
        CREATE INDEX ix_founder_conversation_events_owner_created
            ON founder_conversation_events (owner_id, created_at DESC);
        """
    )
    for table in OWNER_SCOPED_TABLES:
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_isolation ON {table}
                USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
            """
        )


def downgrade() -> None:
    for table in reversed(OWNER_SCOPED_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
