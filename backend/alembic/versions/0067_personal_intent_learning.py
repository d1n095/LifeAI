"""Stage J — Personal intent learning tables.

down_revision = "0066"
must renumber on rebase independently.
"""

from alembic import op

revision = "0067"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE founder_intent_bindings (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            raw_expression text NOT NULL,
            phrase_normalized varchar(512) NOT NULL,
            interpreted_intent text NOT NULL,
            canonical_entity_id uuid,
            confidence double precision NOT NULL DEFAULT 0,
            context jsonb NOT NULL DEFAULT '{}'::jsonb,
            retrieval_trigger varchar(256),
            hit_count integer NOT NULL DEFAULT 1,
            status varchar(24) NOT NULL DEFAULT 'active',
            idempotency_key varchar(128) NOT NULL,
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_founder_intent_bindings_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_founder_intent_bindings_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT uq_founder_intent_bindings_active_phrase UNIQUE (owner_id, phrase_normalized),
            CONSTRAINT ck_founder_intent_bindings_status CHECK (status IN ('active', 'superseded', 'disputed')),
            CONSTRAINT ck_founder_intent_bindings_phrase CHECK (length(btrim(phrase_normalized)) > 0),
            CONSTRAINT ck_founder_intent_bindings_context CHECK (jsonb_typeof(context) = 'object'),
            CONSTRAINT fk_founder_intent_bindings_entity_owner FOREIGN KEY (canonical_entity_id, owner_id)
                REFERENCES project_entities (id, owner_id) ON DELETE SET NULL
        );
        CREATE INDEX ix_founder_intent_bindings_owner ON founder_intent_bindings(owner_id);
        CREATE INDEX ix_founder_intent_bindings_phrase ON founder_intent_bindings(owner_id, phrase_normalized);
        ALTER TABLE founder_intent_bindings ENABLE ROW LEVEL SECURITY;
        ALTER TABLE founder_intent_bindings FORCE ROW LEVEL SECURITY;
        CREATE POLICY founder_intent_bindings_isolation ON founder_intent_bindings
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE TABLE founder_intent_corrections (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            binding_id uuid NOT NULL,
            prior_intent text NOT NULL,
            corrected_intent text NOT NULL,
            wrong_terminology text,
            reason text NOT NULL,
            prior_entity_id uuid,
            corrected_entity_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_founder_intent_corrections_binding_owner FOREIGN KEY (binding_id, owner_id)
                REFERENCES founder_intent_bindings (id, owner_id) ON DELETE CASCADE
        );
        CREATE INDEX ix_founder_intent_corrections_binding ON founder_intent_corrections(binding_id);
        CREATE INDEX ix_founder_intent_corrections_owner ON founder_intent_corrections(owner_id);
        ALTER TABLE founder_intent_corrections ENABLE ROW LEVEL SECURITY;
        ALTER TABLE founder_intent_corrections FORCE ROW LEVEL SECURITY;
        CREATE POLICY founder_intent_corrections_isolation ON founder_intent_corrections
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS founder_intent_corrections;")
    op.execute("DROP TABLE IF EXISTS founder_intent_bindings;")
