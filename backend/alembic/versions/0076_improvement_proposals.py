"""Round 10 M7: MAINAI IMPROVEMENT PROPOSALS — controlled initiative without becoming
an uncontrolled development agent.

MainAI can now recognize recurring cognitive failures and PROPOSE improvements to its own
reasoning workflows. The boundary this migration encodes (mission M7, verbatim in spirit):

  - An identified improvement is NOT permission to execute it. The proposal row records
    benefit/risk estimates and sits in status='proposed' until the FOUNDER decides.
  - There is NO execution path anywhere in this change: no code runs proposals, no new
    agent missions spawn, no security-policy or privileged changes are reachable. The
    proposal is a REVIEW ARTIFACT for the founder.
  - Duplicate suppression via deterministic duplicate_key (source + normalized title):
    the same observation never spawns endless duplicate proposals.
  - Decisions (accept/reject/implement/revert) are founder-only, recorded with notes and
    timestamps; reverting an ineffective change is a first-class outcome.
  - Append-only/auditable: status transitions are constrained (see the check constraint),
    and every decision writes an audit_log row through the service layer.

RLS: owner-scoped like every cognitive table (FORCE + isolation policy), following the
0074 pattern. The app-role grant is SELECT/INSERT/UPDATE (status transitions) — no DELETE
proposals persist as the initiative record; account erasure cascades on owner.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE mainai_improvement_proposals (
            id UUID PRIMARY KEY,
            owner_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            source_type VARCHAR(48) NOT NULL,
            title VARCHAR(200) NOT NULL,
            rationale TEXT NOT NULL,
            benefit_estimate TEXT,
            risk_estimate TEXT,
            duplicate_key VARCHAR(64) NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'proposed',
            proposed_by VARCHAR(64) NOT NULL DEFAULT 'mainai',
            decision_notes TEXT,
            decided_by VARCHAR(64),
            decided_at TIMESTAMP,
            outcome_notes TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            updated_at TIMESTAMP NOT NULL DEFAULT now(),
            CONSTRAINT fk_improvement_proposals_owner FOREIGN KEY (owner_id)
                REFERENCES users (id) ON DELETE CASCADE,
            CONSTRAINT ck_improvement_proposals_source CHECK (source_type IN (
                'recurring_failure_cluster', 'lesson_recurrence', 'founder_observation',
                'manual'
            )),
            CONSTRAINT ck_improvement_proposals_status CHECK (status IN (
                'proposed', 'accepted', 'rejected', 'implemented', 'reverted'
            )),
            CONSTRAINT uq_improvement_proposals_open UNIQUE (owner_id, duplicate_key, status)
        );
        CREATE INDEX ix_improvement_proposals_owner_status
            ON mainai_improvement_proposals(owner_id, status);

        ALTER TABLE mainai_improvement_proposals ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_improvement_proposals FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_improvement_proposals_isolation ON mainai_improvement_proposals
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)
    # Duplicate suppression across OPEN statuses only: a UNIQUE (owner, key, status) lets a
    # rejected proposal be re-proposed later if the evidence recurs, while open duplicates
    # collapse to the existing row.


def downgrade() -> None:
    op.execute("DROP TABLE mainai_improvement_proposals;")
