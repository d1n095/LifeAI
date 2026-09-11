"""Widen intelligence_ideas.disposition to also accept the idea-incubation lifecycle
vocabulary (incubating/planned/ready/later).

Part of the founder-defined "Founder Reasoning + Judgment" program (Part 1),
docs/mainai_v2/MAINAI_FOUNDER_REASONING_JUDGMENT_RECONCILIATION.md §1.2 / §0's own confirmed
gap: "IdeaDisposition's 4 flat values have no INCUBATING/PLANNED/READY/LATER richness."

Additive, not a rewrite -- mirrors migration 0065's exact widening technique for
`work_candidates.priority` (drop + recreate the CHECK constraint, keep the existing
vocabulary verbatim). `intelligence_ideas.disposition` is a plain `varchar(24)` column
constrained only by `ck_intelligence_idea_disposition` (migration 0038) -- NOT a Postgres
ENUM type -- so, like 0065, this is a CHECK-constraint swap, never an `ALTER TYPE ... ADD
VALUE`. `ck_intelligence_idea_reason` (migration 0038: disposition NOT IN ('accepted',
'rejected') OR disposition_reason IS NOT NULL) is untouched -- none of the four new values
require a disposition_reason, matching `app.mainai_executive.idea_incubation`'s own
_REASON_REQUIRED_DISPOSITIONS set. Existing rows (all necessarily accepted/rejected/deferred/
unknown today) are unaffected: the new CHECK is a superset of the old one."""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE intelligence_ideas DROP CONSTRAINT ck_intelligence_idea_disposition;
        ALTER TABLE intelligence_ideas ADD CONSTRAINT ck_intelligence_idea_disposition CHECK (
            disposition IN ('accepted', 'rejected', 'deferred', 'unknown',
                             'incubating', 'planned', 'ready', 'later')
        );
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE intelligence_ideas DROP CONSTRAINT ck_intelligence_idea_disposition;
        ALTER TABLE intelligence_ideas ADD CONSTRAINT ck_intelligence_idea_disposition CHECK (
            disposition IN ('accepted', 'rejected', 'deferred', 'unknown')
        );
    """)
