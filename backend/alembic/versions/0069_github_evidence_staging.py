"""Life MainAI Intelligence program -- GitHub evidence staging (Section B, Continuous Build
Directive). Founder directive (2026-11-14) authorized a "dedicated, narrowly scoped evidence
staging projection" for GitHub-derived work candidates, specifically because the existing
work_candidates/project_entities chain cannot represent this data without violating the
founder-knowledge invariant that chain was built to enforce (see app/models/github_evidence.py's
own module docstring, and PR #396's investigation, for the full reasoning).

SAME SIGNAL PRODUCER != TRUTH WRITER shape migration 0055 (work_candidates) established: a row
here is only a claim that a real GitHub PR/issue MIGHT be worth turning into governed MainAI
work -- never a claim that the founder authorized it. The ONLY path to a real MainAIGoal is
app.github_evidence.service.authorize_github_evidence_candidate(), which always requires an
explicit, caller-supplied authorized_by and delegates to the SAME create_goal() every other real
goal already goes through -- this migration/module does not weaken or duplicate that boundary.

NOTE on this revision's own numbering: this branch's own migration history still has 0068 as its
head (0069-0074 exist only on the separate, as-yet-unmerged Blueprint intelligence PR chain
rooted at PR #340/#387). This is the SAME accepted, documented pattern this repo already uses
for codex/chatgpt-import-foundation's parallel 0037 -- renumber at merge time against whichever
base actually lands first, never silently coexist past that point.
"""

from alembic import op

revision = "0069_github_evidence_staging"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE github_evidence_candidates (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_repo varchar(256) NOT NULL,
            source_kind varchar(32) NOT NULL,
            source_number integer NOT NULL,
            source_url text NOT NULL,
            title text NOT NULL,
            signals jsonb NOT NULL DEFAULT '[]'::jsonb,
            status varchar(24) NOT NULL DEFAULT 'unreviewed',
            authorized_goal_id uuid REFERENCES mainai_goals(id) ON DELETE SET NULL,
            dismissed_reason text,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_github_evidence_candidates_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_github_evidence_candidates_kind CHECK (source_kind IN ('open_pull_request', 'open_issue')),
            CONSTRAINT ck_github_evidence_candidates_number CHECK (source_number > 0),
            CONSTRAINT ck_github_evidence_candidates_status CHECK (status IN ('unreviewed', 'authorized', 'dismissed')),
            CONSTRAINT ck_github_evidence_candidates_authorized_requires_goal CHECK (
                status <> 'authorized' OR authorized_goal_id IS NOT NULL
            ),
            CONSTRAINT ck_github_evidence_candidates_title CHECK (length(btrim(title)) > 0),
            CONSTRAINT ck_github_evidence_candidates_signals CHECK (jsonb_typeof(signals) = 'array'),
            CONSTRAINT ck_github_evidence_candidates_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_github_evidence_candidates_owner_status ON github_evidence_candidates(owner_id, status);
        CREATE INDEX ix_github_evidence_candidates_goal ON github_evidence_candidates(authorized_goal_id);

        ALTER TABLE github_evidence_candidates ENABLE ROW LEVEL SECURITY;
        ALTER TABLE github_evidence_candidates FORCE ROW LEVEL SECURITY;
        CREATE POLICY github_evidence_candidates_isolation ON github_evidence_candidates
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION erase_own_github_evidence_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_github_evidence_children requires an authenticated app.current_user_id session context.';
            END IF;
            DELETE FROM public.github_evidence_candidates WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_github_evidence_children() FROM PUBLIC;
    """)

    # No explicit GRANT here, matching every migration since 0004 -- mainai_app's SELECT/INSERT/
    # UPDATE access comes from the ALTER DEFAULT PRIVILEGES set up once by backend/db-init/
    # 01-app-role.sh / backend/scripts/security/ensure_app_role.py; app/rls.py's
    # apply_mainai_execution_privileges() narrows DELETE/TRUNCATE/REFERENCES/TRIGGER away and
    # grants EXECUTE on erase_own_github_evidence_children() instead, on every boot.


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_own_github_evidence_children();")
    op.execute("DROP TABLE github_evidence_candidates;")
