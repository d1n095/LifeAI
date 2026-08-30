"""Stage N — hot/warm/cold memory tiers (Alembic 0068)."""

from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE memory_tier_states (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            target_kind varchar(64) NOT NULL,
            target_id uuid NOT NULL,
            tier varchar(16) NOT NULL DEFAULT 'warm',
            retrieval_count integer NOT NULL DEFAULT 0,
            last_retrieved_at timestamptz,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_memory_tier_states_target UNIQUE (owner_id, target_kind, target_id),
            CONSTRAINT ck_memory_tier_states_tier CHECK (tier IN ('hot', 'warm', 'cold')),
            CONSTRAINT ck_memory_tier_states_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_memory_tier_states_owner_tier ON memory_tier_states(owner_id, tier);
        ALTER TABLE memory_tier_states ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memory_tier_states FORCE ROW LEVEL SECURITY;
        CREATE POLICY memory_tier_states_isolation ON memory_tier_states
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS memory_tier_states;")
