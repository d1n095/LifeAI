"""Stage B — Concept / idea reconciliation.

Extends project_entities (no second concept store):
- title_normalized + partial unique fingerprint for current concepts
- project_entity_aliases for differently-worded SAME bindings
- widened relationship_type vocabulary
- unique edge (owner, from, to, type)

down_revision = Stage A 0063. Claude #197's pending 0063/0064 must renumber on rebase.
"""

from alembic import op

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE project_entities
            ADD COLUMN title_normalized varchar(512);
        UPDATE project_entities
            SET title_normalized = lower(btrim(regexp_replace(title, '[[:punct:]]+', ' ', 'g')));
        UPDATE project_entities
            SET title_normalized = regexp_replace(title_normalized, '\\s+', ' ', 'g');
        ALTER TABLE project_entities
            ALTER COLUMN title_normalized SET NOT NULL;
        ALTER TABLE project_entities
            ADD CONSTRAINT ck_project_entities_title_normalized CHECK (length(btrim(title_normalized)) > 0);
        CREATE UNIQUE INDEX uq_project_entities_current_fingerprint
            ON project_entities (owner_id, entity_type, title_normalized)
            WHERE status IN ('active', 'proposed');
    """)

    op.execute("""
        CREATE TABLE project_entity_aliases (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            entity_id uuid NOT NULL,
            raw_text text NOT NULL,
            text_normalized varchar(512) NOT NULL,
            source_claim_id uuid REFERENCES knowledge_claims(id) ON DELETE SET NULL,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_project_entity_aliases_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_project_entity_aliases_norm UNIQUE (owner_id, text_normalized),
            CONSTRAINT fk_project_entity_aliases_entity_owner FOREIGN KEY (entity_id, owner_id)
                REFERENCES project_entities (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT ck_project_entity_aliases_raw CHECK (length(btrim(raw_text)) > 0),
            CONSTRAINT ck_project_entity_aliases_norm CHECK (length(btrim(text_normalized)) > 0),
            CONSTRAINT ck_project_entity_aliases_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_project_entity_aliases_entity ON project_entity_aliases(entity_id);
        ALTER TABLE project_entity_aliases ENABLE ROW LEVEL SECURITY;
        ALTER TABLE project_entity_aliases FORCE ROW LEVEL SECURITY;
        CREATE POLICY project_entity_aliases_isolation ON project_entity_aliases
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        ALTER TABLE project_entity_relationships
            DROP CONSTRAINT ck_project_entity_relationships_type;
        ALTER TABLE project_entity_relationships
            ADD CONSTRAINT ck_project_entity_relationships_type CHECK (relationship_type IN (
                'same', 'partial_overlap', 'related', 'depends_on', 'contradicts', 'supersedes',
                'extends', 'alternative', 'reuses',
                'relates_to', 'blocks', 'answers', 'duplicates', 'derived_from'
            ));
        CREATE UNIQUE INDEX uq_project_entity_relationships_edge
            ON project_entity_relationships (owner_id, from_entity_id, to_entity_id, relationship_type);
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_project_entity_relationships_edge;")
    op.execute("""
        ALTER TABLE project_entity_relationships
            DROP CONSTRAINT ck_project_entity_relationships_type;
        ALTER TABLE project_entity_relationships
            ADD CONSTRAINT ck_project_entity_relationships_type CHECK (relationship_type IN (
                'relates_to', 'supersedes', 'contradicts', 'blocks', 'answers', 'duplicates', 'derived_from'
            ));
    """)
    op.execute("DROP TABLE project_entity_aliases;")
    op.execute("DROP INDEX IF EXISTS uq_project_entities_current_fingerprint;")
    op.execute("""
        ALTER TABLE project_entities DROP CONSTRAINT ck_project_entities_title_normalized;
        ALTER TABLE project_entities DROP COLUMN title_normalized;
    """)
