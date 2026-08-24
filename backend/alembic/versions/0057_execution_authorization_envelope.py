"""Life Execution Authorization Envelope -- the founder-decided architecture for the missing
piece Cursor's own handoff identified: `app.development_supervisor.service.run_supervisor()`
requires a `SupervisorScope` with `allowed_paths`/`allowed_capabilities` -- genuinely
per-task security-scope fields with NO safe generic default (confirmed by direct inspection:
zero non-test construction sites for `SupervisorScope` anywhere in the codebase; the one
existing test hand-picks `allowed_paths=("calculator.py", "test_calculator.py")` for that
specific task). Deriving these from `task_type` alone, or requiring the founder to hand-type
every path from scratch for every goal, were both explicitly rejected as the wrong shape.

Founder-decided architecture instead:

    ProjectEntity / evidence
    -> WorkCandidate
    -> execution_scope_proposals   (PROPOSED, never authority -- MainAI may suggest)
    -> founder review/authorization
    -> execution_authorization_envelopes   (AUTHORIZED -- ONLY the founder can create one)
    -> MainAIGoal
    -> plan/tasks (narrower WorkBinding scope, a LATER, separate closing step)
    -> SupervisorScope / Safe Planner / Operator intersection
    -> execution

The EXACT SAME SIGNAL PRODUCER != TRUTH WRITER shape migrations 0053/0054/0055 already
established, applied a fourth time to execution authority instead of knowledge/work: two
structurally separate tables, not one row with dual-purpose proposed_*/authorized_* columns --
`execution_scope_proposals` has NO authority-bearing columns at all (structurally incapable of
granting anything), `execution_authorization_envelopes` is created ONLY by
`app.execution_envelopes.service.authorize_execution_scope()`, which ALWAYS requires the
caller's own explicit `authorized_by`/`authorized_paths`/`authorized_capabilities`/
`authorized_risk` -- the proposal's own suggested values are never silently copied in as
authority. `PROPOSED_SCOPE != AUTHORIZED_SCOPE`, structurally, not just documented.

Authorizing a NEW envelope for a goal that already has a current one supersedes the old one
(never mutates it) -- exactly the same never-mutate-just-supersede discipline every other
"derived knowledge"/"derived authority" foundation in this mission uses, so a founder's
original authorization decision remains durably auditable even after a later re-authorization
narrows or expands it.

`goal_id` on both tables is a composite owner-anchored FK to `mainai_goals(id, owner_id)` --
`mainai_goals` already has the `uq_mainai_goals_id_owner_id` unique constraint (migration
0032) needed to anchor against, so this migration needs no separate constraint-adding step the
way migration 0056 needed for `knowledge_claims`. Applying that same lesson proactively here,
for a table whose whole purpose IS defining execution authority, rather than waiting for
another review to catch a bare FK on it."""

from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE execution_scope_proposals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            goal_id uuid NOT NULL,
            repository_identity text,
            proposed_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            proposed_capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
            proposed_risk varchar(16) NOT NULL DEFAULT 'low',
            proposal_reasoning text,
            proposal_strategy varchar(64) NOT NULL DEFAULT 'unknown',
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            status varchar(24) NOT NULL DEFAULT 'unreviewed',
            authorized_envelope_id uuid,
            rejected_reason text,
            idempotency_key varchar(128) NOT NULL,
            observed_at timestamp NOT NULL DEFAULT now(),
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT fk_execution_scope_proposals_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT uq_execution_scope_proposals_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_execution_scope_proposals_risk CHECK (proposed_risk IN ('low', 'medium', 'high')),
            CONSTRAINT ck_execution_scope_proposals_status CHECK (status IN ('unreviewed', 'authorized', 'rejected')),
            CONSTRAINT ck_execution_scope_proposals_authorized_requires_envelope CHECK (
                status <> 'authorized' OR authorized_envelope_id IS NOT NULL
            ),
            CONSTRAINT ck_execution_scope_proposals_paths CHECK (jsonb_typeof(proposed_paths) = 'array'),
            CONSTRAINT ck_execution_scope_proposals_capabilities CHECK (jsonb_typeof(proposed_capabilities) = 'array'),
            CONSTRAINT ck_execution_scope_proposals_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_execution_scope_proposals_owner_status ON execution_scope_proposals(owner_id, status);
        CREATE INDEX ix_execution_scope_proposals_goal ON execution_scope_proposals(goal_id);

        ALTER TABLE execution_scope_proposals ENABLE ROW LEVEL SECURITY;
        ALTER TABLE execution_scope_proposals FORCE ROW LEVEL SECURITY;
        CREATE POLICY execution_scope_proposals_isolation ON execution_scope_proposals
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE TABLE execution_authorization_envelopes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            goal_id uuid NOT NULL,
            source_proposal_id uuid,
            repository_identity text,
            authorized_paths jsonb NOT NULL DEFAULT '[]'::jsonb,
            authorized_capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
            authorized_risk varchar(16) NOT NULL,
            authorized_by varchar(64) NOT NULL,
            authorized_at timestamp NOT NULL DEFAULT now(),
            status varchar(24) NOT NULL DEFAULT 'active',
            supersedes_envelope_id uuid,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            idempotency_key varchar(128) NOT NULL,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_execution_authorization_envelopes_id_owner UNIQUE (id, owner_id),
            CONSTRAINT fk_execution_authorization_envelopes_goal_owner FOREIGN KEY (goal_id, owner_id)
                REFERENCES mainai_goals (id, owner_id) ON DELETE CASCADE,
            CONSTRAINT fk_execution_authorization_envelopes_supersedes FOREIGN KEY (supersedes_envelope_id, owner_id)
                REFERENCES execution_authorization_envelopes (id, owner_id) ON DELETE SET NULL,
            CONSTRAINT uq_execution_authorization_envelopes_idem UNIQUE (owner_id, idempotency_key),
            CONSTRAINT ck_execution_authorization_envelopes_risk CHECK (authorized_risk IN ('low', 'medium', 'high')),
            CONSTRAINT ck_execution_authorization_envelopes_status CHECK (status IN ('active', 'superseded')),
            CONSTRAINT ck_execution_authorization_envelopes_no_self_supersede CHECK (supersedes_envelope_id IS NULL OR supersedes_envelope_id <> id),
            CONSTRAINT ck_execution_authorization_envelopes_paths CHECK (jsonb_typeof(authorized_paths) = 'array'),
            CONSTRAINT ck_execution_authorization_envelopes_capabilities CHECK (jsonb_typeof(authorized_capabilities) = 'array'),
            CONSTRAINT ck_execution_authorization_envelopes_provenance CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE INDEX ix_execution_authorization_envelopes_owner_goal ON execution_authorization_envelopes(owner_id, goal_id);
        CREATE INDEX ix_execution_authorization_envelopes_owner_status ON execution_authorization_envelopes(owner_id, status);

        ALTER TABLE execution_authorization_envelopes ENABLE ROW LEVEL SECURITY;
        ALTER TABLE execution_authorization_envelopes FORCE ROW LEVEL SECURITY;
        CREATE POLICY execution_authorization_envelopes_isolation ON execution_authorization_envelopes
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        ALTER TABLE execution_scope_proposals ADD CONSTRAINT fk_execution_scope_proposals_authorized_envelope_owner
            FOREIGN KEY (authorized_envelope_id, owner_id)
            REFERENCES execution_authorization_envelopes (id, owner_id) ON DELETE SET NULL;
    """)

    op.execute("""
        CREATE FUNCTION erase_own_execution_authorization_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE v_owner_id uuid;
        BEGIN
            v_owner_id := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner_id IS NULL THEN
                RAISE EXCEPTION 'erase_own_execution_authorization_children requires an authenticated app.current_user_id session context.';
            END IF;
            DELETE FROM public.execution_scope_proposals WHERE owner_id = v_owner_id;
            DELETE FROM public.execution_authorization_envelopes WHERE owner_id = v_owner_id;
        END;
        $$;
        REVOKE ALL ON FUNCTION erase_own_execution_authorization_children() FROM PUBLIC;
    """)

    # No explicit GRANT here, matching every migration since 0004 -- mainai_app's access comes
    # from the ALTER DEFAULT PRIVILEGES set up once by backend/db-init/01-app-role.sh /
    # backend/scripts/security/ensure_app_role.py. Deletion is intended to happen ONLY through
    # erase_own_execution_authorization_children() -- the Python service layer
    # (app/execution_envelopes/service.py) never issues a raw DELETE against either table.


def downgrade() -> None:
    op.execute("DROP FUNCTION erase_own_execution_authorization_children();")
    op.execute("ALTER TABLE execution_scope_proposals DROP CONSTRAINT fk_execution_scope_proposals_authorized_envelope_owner;")
    op.execute("DROP TABLE execution_authorization_envelopes;")
    op.execute("DROP TABLE execution_scope_proposals;")
