"""Life Project Entities / Interpretation Queue -- the P4 layer
docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md's own §4.2/§6.4 describes and this codebase never
built: extends the already-live P3 claim extraction (`app/rag/claims.py`'s
`extract_claims_for_document()`, called from `app/rag/library_import.py` after every
successful import) with the next stage -- turning a typed `KnowledgeClaim` into structured
project understanding, WITHOUT silently promoting extraction output into founder-trusted fact.

Standing principle this migration exists to enforce structurally, exactly as migration 0053
did for `candidate_learning_signals`: SIGNAL PRODUCER != TRUTH WRITER. `extract_claims_for_
document()` is itself already a heuristic AI extraction pass over chunk text -- its own
`claim_type` classification is useful routing information, not proof. Wiring it directly into
a table other code would treat as settled project understanding would repeat exactly the
mistake migration 0053's own docstring already documented and fixed for founder memory.

`interpretation_proposals` is the new, explicitly untrusted staging table (same shape as
`candidate_learning_signals`): a row here is only a claim that "this extracted claim MIGHT be
worth turning into structured project understanding", never a claim about the project itself.
The ONLY path from a proposal to real project understanding is
`app.project_entities.service.promote_interpretation_proposal()`, which ALWAYS requires an
explicit, caller-supplied `authority`/`basis` for the `project_entities` row it creates -- the
proposal's own `classifier_confidence` (copied from the claim's own objective, grounding-based
confidence bucket, never the extracting model's self-report) is never silently copied into
that authority. `project_entities` reuses the exact `authority`/`basis` vocabulary migrations
0049/0050 already established (`founder`, `repeated_founder_preference`, `deterministic_
source`, `inferred_pattern`, `ai_interpretation`, `unknown` / `manual`, `deterministic`,
`imported`, `inferred`, `ai_interpretation`, `unknown`) -- one shared vocabulary across every
"derived knowledge" foundation this mission has built, not a fourth invented one.

`project_entity_relationships` mirrors the existing, pre-mission `claim_relationships` table
(migration 0007) exactly -- same shape, same bare (not composite-owner-anchored) FK precedent
for `from_entity_id`/`to_entity_id`, since that is this codebase's own established pattern for
an edge table between rows of the SAME parent table (RLS on the parent already prevents a
cross-owner edge from being usable, and CASCADE from `project_entities` handles cleanup).

Provenance is mandatory, not optional: `interpretation_proposals.source_claim_id` is a NOT
NULL FK to `knowledge_claims(id)` (bare, matching `claim_relationships`' own precedent for
referencing that table), and `project_entities.derived_from_claim_id` is NOT NULL ON DELETE
RESTRICT -- a `project_entities` row can never exist without a real source claim behind it,
and deleting the source claim out from under a promoted entity is rejected rather than
silently orphaning the entity's own provenance (same discipline `knowledge_claims.memory_
source_id`'s own RESTRICT choice already established).

Explicitly NOT built in this migration (see docs/LIFE_PROJECT_ENTITIES_INTERPRETATION.md):
no interpretation-queue UI, no embedding-based relation discovery, no automatic promotion.
This is the schema + service foundation only, matching this mission's own established
"foundation first, wire to real callers with an explicit governed step" methodology -- the
SAME shape as migrations 0048/0049/0050/0053 before it.
"""

from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE project_entities (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            entity_type varchar(32) NOT NULL,
            title text NOT NULL,
            summary text,
            status varchar(24) NOT NULL DEFAULT 'proposed',
            derived_from_claim_id uuid NOT NULL REFERENCES knowledge_claims(id) ON DELETE RESTRICT,
            decided_by text,
            decided_at timestamp,
            supersedes_entity_id uuid,
            authority varchar(40) NOT NULL DEFAULT 'unknown',
            basis varchar(40) NOT NULL DEFAULT 'unknown',
            confidence double precision,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            last_reviewed_at timestamp,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_project_entities_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_project_entities_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT fk_project_entities_supersedes FOREIGN KEY (supersedes_entity_id, owner_id)
                REFERENCES project_entities (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_project_entities_no_self_supersede CHECK (supersedes_entity_id IS NULL OR supersedes_entity_id <> id),
            CONSTRAINT ck_project_entities_entity_type CHECK (entity_type IN (
                'idea', 'decision', 'task_reference', 'vision_statement', 'open_question'
            )),
            CONSTRAINT ck_project_entities_status CHECK (status IN (
                'active', 'historical', 'proposed', 'superseded', 'disputed'
            )),
            CONSTRAINT ck_project_entities_authority CHECK (authority IN (
                'founder', 'repeated_founder_preference', 'deterministic_source', 'inferred_pattern',
                'ai_interpretation', 'unknown'
            )),
            CONSTRAINT ck_project_entities_basis CHECK (basis IN (
                'manual', 'deterministic', 'imported', 'inferred', 'ai_interpretation', 'unknown'
            )),
            CONSTRAINT ck_project_entities_confidence CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
            CONSTRAINT ck_project_entities_title CHECK (length(btrim(title)) > 0),
            CONSTRAINT ck_project_entities_decision_fields CHECK (
                entity_type = 'decision' OR (decided_by IS NULL AND decided_at IS NULL)
            ),
            CONSTRAINT ck_project_entities_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_project_entities_owner_status ON project_entities(owner_id, status);
        CREATE INDEX ix_project_entities_owner_type ON project_entities(owner_id, entity_type);
        CREATE INDEX ix_project_entities_claim ON project_entities(derived_from_claim_id);
        CREATE INDEX ix_project_entities_supersedes ON project_entities(supersedes_entity_id);

        ALTER TABLE project_entities ENABLE ROW LEVEL SECURITY;
        ALTER TABLE project_entities FORCE ROW LEVEL SECURITY;
        CREATE POLICY project_entities_isolation ON project_entities
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE TABLE project_entity_relationships (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            from_entity_id uuid NOT NULL REFERENCES project_entities(id) ON DELETE CASCADE,
            to_entity_id uuid NOT NULL REFERENCES project_entities(id) ON DELETE CASCADE,
            relationship_type varchar(32) NOT NULL,
            note text,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_project_entity_relationships_type CHECK (relationship_type IN (
                'relates_to', 'supersedes', 'contradicts', 'blocks', 'answers', 'duplicates', 'derived_from'
            )),
            CONSTRAINT ck_project_entity_relationships_no_self CHECK (from_entity_id <> to_entity_id)
        );
        CREATE INDEX ix_project_entity_relationships_owner ON project_entity_relationships(owner_id);
        CREATE INDEX ix_project_entity_relationships_from ON project_entity_relationships(from_entity_id);
        CREATE INDEX ix_project_entity_relationships_to ON project_entity_relationships(to_entity_id);

        ALTER TABLE project_entity_relationships ENABLE ROW LEVEL SECURITY;
        ALTER TABLE project_entity_relationships FORCE ROW LEVEL SECURITY;
        CREATE POLICY project_entity_relationships_isolation ON project_entity_relationships
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE TABLE interpretation_proposals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_claim_id uuid NOT NULL REFERENCES knowledge_claims(id) ON DELETE CASCADE,
            proposed_entity_type varchar(32) NOT NULL,
            classifier_strategy varchar(64) NOT NULL DEFAULT 'unknown',
            classifier_confidence varchar(16) NOT NULL DEFAULT 'unknown',
            classifier_reasoning text,
            status varchar(24) NOT NULL DEFAULT 'unreviewed',
            promoted_to_entity_id uuid,
            dismissed_reason text,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_interpretation_proposals_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT fk_interpretation_proposals_promoted_entity FOREIGN KEY (promoted_to_entity_id, owner_id)
                REFERENCES project_entities (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT ck_interpretation_proposals_entity_type CHECK (proposed_entity_type IN (
                'idea', 'decision', 'task_reference', 'vision_statement', 'open_question'
            )),
            CONSTRAINT ck_interpretation_proposals_confidence CHECK (classifier_confidence IN (
                'certain', 'likely', 'uncertain', 'conflict', 'no_basis', 'unknown'
            )),
            CONSTRAINT ck_interpretation_proposals_status CHECK (status IN ('unreviewed', 'promoted', 'dismissed')),
            CONSTRAINT ck_interpretation_proposals_promoted_requires_entity CHECK (
                status <> 'promoted' OR promoted_to_entity_id IS NOT NULL
            ),
            CONSTRAINT ck_interpretation_proposals_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_interpretation_proposals_owner_status ON interpretation_proposals(owner_id, status);
        CREATE INDEX ix_interpretation_proposals_claim ON interpretation_proposals(source_claim_id);

        ALTER TABLE interpretation_proposals ENABLE ROW LEVEL SECURITY;
        ALTER TABLE interpretation_proposals FORCE ROW LEVEL SECURITY;
        CREATE POLICY interpretation_proposals_isolation ON interpretation_proposals
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION erase_own_project_entities_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_project_entities_children requires an authenticated app.current_user_id session context.';
            END IF;
            DELETE FROM public.interpretation_proposals WHERE owner_id = v_owner_id;
            -- project_entity_relationships rows are removed via ON DELETE CASCADE from
            -- project_entities below -- no separate DELETE needed, matching claim_
            -- relationships' own precedent of having no independent erasure path.
            DELETE FROM public.project_entities WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_project_entities_children() FROM PUBLIC;
    """)

    # No explicit GRANT here, matching every migration since 0004: mainai_app's access comes
    # from the ALTER DEFAULT PRIVILEGES set up once by backend/db-init/01-app-role.sh /
    # backend/scripts/security/ensure_app_role.py. Deletion of interpretation_proposals/
    # project_entities/project_entity_relationships is intended to happen ONLY through
    # erase_own_project_entities_children() -- the Python service layer
    # (app/project_entities/service.py) never issues a raw DELETE against these tables --
    # matching the same convention-based discipline migrations 0049/0050/0053 already
    # established for their own owner-scoped tables.


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_own_project_entities_children();")
    op.execute("DROP TABLE interpretation_proposals;")
    op.execute("DROP TABLE project_entity_relationships;")
    op.execute("DROP TABLE project_entities;")
