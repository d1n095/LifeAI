"""Fixes an owner-anchoring gap in migrations 0054/0055 a founder adversarial review found:
`interpretation_proposals.source_claim_id`, `project_entities.derived_from_claim_id`, and
`project_entity_relationships.from_entity_id`/`to_entity_id` used BARE foreign keys, the same
defect class `app/models/knowledge_claim.py`'s own module docstring already explicitly
documents and fixes for `memory_source_id` (see that model's `fk_knowledge_claims_memory_
source_owner` composite FK, migration 0019): a bare FK only proves the referenced row exists,
it does NOT prove it belongs to the same owner -- FK constraint checks run independently of
RLS, so a caller (buggy, compromised, or simply future code that does not re-derive the
reference the way the current single call site does) could otherwise construct
`owner_id=A, source_claim_id=<B's claim>` and have the database accept it.

Migration 0054 itself got this right for `project_entities.supersedes_entity_id` (composite
`(supersedes_entity_id, owner_id) -> project_entities(id, owner_id)`) and for
`interpretation_proposals.promoted_to_entity_id` (composite `(promoted_to_entity_id, owner_id)
-> project_entities(id, owner_id)`) -- this migration brings the remaining four references up
to the SAME standard, rather than leaving them at `claim_relationships` (migration 0007, which
predates this mission's own owner-anchoring discipline)'s older, looser bare-FK precedent.
"Existing precedent" is not automatically "correct precedent" -- the newer, already-proven-safer
pattern wins.

`knowledge_claims` has no `UNIQUE(id, owner_id)` constraint to anchor a composite FK against
yet (only `id` is unique, via its primary key) -- this migration adds one. Since `id` alone is
already globally unique, `(id, owner_id)` is trivially unique too; this is a pure additive
constraint, not a data-changing one, and applies instantly regardless of table size.

Both affected tables (created by migrations 0054/0055, days old at the time of this review)
are expected to hold zero or near-zero production rows -- these ALTER TABLE ... DROP/ADD
CONSTRAINT statements are not expected to encounter any actually-cross-owner row in practice;
if one somehow exists, the ADD CONSTRAINT step below will correctly fail rather than silently
succeed, which is the intended fail-closed behavior for a data-integrity migration."""

from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE knowledge_claims ADD CONSTRAINT uq_knowledge_claims_id_owner UNIQUE (id, owner_id);
    """)

    op.execute("""
        ALTER TABLE interpretation_proposals DROP CONSTRAINT interpretation_proposals_source_claim_id_fkey;
        ALTER TABLE interpretation_proposals ADD CONSTRAINT fk_interpretation_proposals_source_claim_owner
            FOREIGN KEY (source_claim_id, owner_id) REFERENCES knowledge_claims (id, owner_id) ON DELETE CASCADE;
    """)

    op.execute("""
        ALTER TABLE project_entities DROP CONSTRAINT project_entities_derived_from_claim_id_fkey;
        ALTER TABLE project_entities ADD CONSTRAINT fk_project_entities_derived_from_claim_owner
            FOREIGN KEY (derived_from_claim_id, owner_id) REFERENCES knowledge_claims (id, owner_id) ON DELETE RESTRICT;
    """)

    op.execute("""
        ALTER TABLE project_entity_relationships DROP CONSTRAINT project_entity_relationships_from_entity_id_fkey;
        ALTER TABLE project_entity_relationships ADD CONSTRAINT fk_project_entity_relationships_from_entity_owner
            FOREIGN KEY (from_entity_id, owner_id) REFERENCES project_entities (id, owner_id) ON DELETE CASCADE;
        ALTER TABLE project_entity_relationships DROP CONSTRAINT project_entity_relationships_to_entity_id_fkey;
        ALTER TABLE project_entity_relationships ADD CONSTRAINT fk_project_entity_relationships_to_entity_owner
            FOREIGN KEY (to_entity_id, owner_id) REFERENCES project_entities (id, owner_id) ON DELETE CASCADE;
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE project_entity_relationships DROP CONSTRAINT fk_project_entity_relationships_to_entity_owner;
        ALTER TABLE project_entity_relationships ADD CONSTRAINT project_entity_relationships_to_entity_id_fkey
            FOREIGN KEY (to_entity_id) REFERENCES project_entities (id) ON DELETE CASCADE;
        ALTER TABLE project_entity_relationships DROP CONSTRAINT fk_project_entity_relationships_from_entity_owner;
        ALTER TABLE project_entity_relationships ADD CONSTRAINT project_entity_relationships_from_entity_id_fkey
            FOREIGN KEY (from_entity_id) REFERENCES project_entities (id) ON DELETE CASCADE;
    """)
    op.execute("""
        ALTER TABLE project_entities DROP CONSTRAINT fk_project_entities_derived_from_claim_owner;
        ALTER TABLE project_entities ADD CONSTRAINT project_entities_derived_from_claim_id_fkey
            FOREIGN KEY (derived_from_claim_id) REFERENCES knowledge_claims (id) ON DELETE RESTRICT;
    """)
    op.execute("""
        ALTER TABLE interpretation_proposals DROP CONSTRAINT fk_interpretation_proposals_source_claim_owner;
        ALTER TABLE interpretation_proposals ADD CONSTRAINT interpretation_proposals_source_claim_id_fkey
            FOREIGN KEY (source_claim_id) REFERENCES knowledge_claims (id) ON DELETE CASCADE;
    """)
    op.execute("""
        ALTER TABLE knowledge_claims DROP CONSTRAINT uq_knowledge_claims_id_owner;
    """)
