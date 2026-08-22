"""Life Work Candidates -- the second half of the P4 closing bridge migration 0054 built the
first half of: `project_entities` (structured project understanding) is now connected to
`app.mainai_execution.planner.create_goal()` (the already-existing, already-governed entry
point for a real `MainAIGoal`), WITHOUT collapsing "a good inference exists" into "execution
is authorized" -- exactly the distinction the founder's own closing-phase directive named
explicitly: DERIVED WORK CANDIDATE != AUTHORIZED WORK != EXECUTABLE WORK.

Standing principle this migration exists to enforce structurally, the SAME SIGNAL PRODUCER !=
TRUTH WRITER shape migrations 0053/0054 already used, one level further down the chain this
time: `work_candidates` is the new, explicitly unauthorized staging table. A row here is only
a claim that "this piece of structured project understanding MIGHT be worth turning into real,
governed MainAI work" -- never a claim that the founder actually authorized it. The ONLY path
from a work candidate to a real `MainAIGoal` is
`app.work_candidates.service.authorize_work_candidate()`, which ALWAYS requires an explicit,
caller-supplied `authorized_by` (mirroring `planner.create_goal()`'s own `created_by`, itself
only ever set by `app/routers/mainai_execution.py`'s `Depends(require_founder)`-gated route --
this migration/module does not weaken or duplicate that authorization boundary, it sits
strictly upstream of it and calls the SAME `create_goal()` every other real goal already goes
through, not a second goal-creation path).

`authorized_goal_id` is a bare (not composite-owner-anchored) FK to `mainai_goals(id)`,
matching `mainai_plans.goal_id`'s own existing precedent for this specific table family (that
table has no `unique(id, owner_id)` constraint to anchor a composite FK against either) --
this migration follows that established convention rather than inventing a stricter one only
for this table, consistent with "don't build a second architecture"."""

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE work_candidates (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_entity_id uuid NOT NULL REFERENCES project_entities(id) ON DELETE CASCADE,
            title text NOT NULL,
            rationale text,
            dependencies jsonb NOT NULL DEFAULT '[]'::jsonb,
            priority varchar(16) NOT NULL DEFAULT 'medium',
            status varchar(24) NOT NULL DEFAULT 'unreviewed',
            authorized_goal_id uuid REFERENCES mainai_goals(id) ON DELETE SET NULL,
            dismissed_reason text,
            classifier_strategy varchar(64) NOT NULL DEFAULT 'unknown',
            classifier_confidence double precision,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_work_candidates_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_work_candidates_priority CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
            CONSTRAINT ck_work_candidates_status CHECK (status IN ('unreviewed', 'authorized', 'dismissed', 'superseded')),
            CONSTRAINT ck_work_candidates_authorized_requires_goal CHECK (
                status <> 'authorized' OR authorized_goal_id IS NOT NULL
            ),
            CONSTRAINT ck_work_candidates_title CHECK (length(btrim(title)) > 0),
            CONSTRAINT ck_work_candidates_dependencies CHECK (jsonb_typeof(dependencies) = 'array'),
            CONSTRAINT ck_work_candidates_provenance CHECK (jsonb_typeof(provenance) = 'object'),
            CONSTRAINT ck_work_candidates_confidence CHECK (classifier_confidence IS NULL OR classifier_confidence BETWEEN 0 AND 1)
        );
        CREATE INDEX ix_work_candidates_owner_status ON work_candidates(owner_id, status);
        CREATE INDEX ix_work_candidates_source_entity ON work_candidates(source_entity_id);
        CREATE INDEX ix_work_candidates_goal ON work_candidates(authorized_goal_id);

        ALTER TABLE work_candidates ENABLE ROW LEVEL SECURITY;
        ALTER TABLE work_candidates FORCE ROW LEVEL SECURITY;
        CREATE POLICY work_candidates_isolation ON work_candidates
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE FUNCTION erase_own_work_candidates_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_work_candidates_children requires an authenticated app.current_user_id session context.';
            END IF;
            DELETE FROM public.work_candidates WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_work_candidates_children() FROM PUBLIC;
    """)

    # No explicit GRANT here, matching every migration since 0004 -- mainai_app's access comes
    # from the ALTER DEFAULT PRIVILEGES set up once by backend/db-init/01-app-role.sh /
    # backend/scripts/security/ensure_app_role.py. Deletion is intended to happen ONLY through
    # erase_own_work_candidates_children() -- the Python service layer
    # (app/work_candidates/service.py) never issues a raw DELETE against this table.


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_own_work_candidates_children();")
    op.execute("DROP TABLE work_candidates;")
