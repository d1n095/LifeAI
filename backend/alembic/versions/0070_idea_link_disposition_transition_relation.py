"""Widen intelligence_idea_links.relation to also accept 'disposition_transition'.

Second half of the idea-incubation lifecycle migration pair (see migration 0069's own module
docstring). Found by direct testing, not assumed: `intelligence_ideas` is DB-enforced
append-only (migration 0038's `trg_intelligence_ideas_deny_mutation` trigger denies UPDATE/
DELETE unconditionally outside account erasure), so
`app.mainai_executive.idea_incubation.transition_idea_disposition()` cannot mutate
`IntelligenceIdea.disposition` in place -- it must record a NEW `IntelligenceIdea` row (via
the real, existing `record_idea()`) and link it back to the row it supersedes via the real,
existing `record_idea_link()`. `IntelligenceIdeaLink.relation` is itself a closed vocabulary
(`ck_intelligence_idea_link_relation`, migration 0038: reuses/contradicts/
depends_on_assumption/combined_into) -- none of which mean "supersedes" -- so recording that
link requires this second, equally small, additive widening. Mirrors migration 0065/0069's
exact CHECK-constraint-swap technique. Existing rows (all necessarily reuses/contradicts/
depends_on_assumption/combined_into today) are unaffected: the new CHECK is a superset of the
old one."""

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE intelligence_idea_links DROP CONSTRAINT ck_intelligence_idea_link_relation;
        ALTER TABLE intelligence_idea_links ADD CONSTRAINT ck_intelligence_idea_link_relation CHECK (
            relation IN ('reuses', 'contradicts', 'depends_on_assumption', 'combined_into',
                         'disposition_transition')
        );
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE intelligence_idea_links DROP CONSTRAINT ck_intelligence_idea_link_relation;
        ALTER TABLE intelligence_idea_links ADD CONSTRAINT ck_intelligence_idea_link_relation CHECK (
            relation IN ('reuses', 'contradicts', 'depends_on_assumption', 'combined_into')
        );
    """)
