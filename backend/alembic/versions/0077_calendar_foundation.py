"""Founder calendar foundation -- migration 0071.

Originally authored as a standalone "0069" (down_revision "0068") on its own branch, like three
sibling candidates that each independently claimed "0069" from the same dev tip (PR #408
system_activation_authorization, PR #403 github_evidence_staging, PR #437
repository_file_source_units). Renumbered to 0071 ONLY on the integration branch
(`claude/migration-0069-composition-integration`) that composes all four into one linear chain
in the founder's stated order; the original PR branch's own file is untouched. See
docs/BRANCH_REGISTRY.md for the composition record.

Founder directive 2026-11-15 ("Continue implementing MainAI's core functionality"), Priority
3: "Context-aware calendar, task management and proactive assistance." No such domain existed
anywhere in this codebase before this migration (verified by grep: no `scheduled_at`/`due_at`/
`start_time` column anywhere, and `app.life_intents` is goal/dependency-level, never
time-scheduled).

`calendar_events` is an ordinary, owner-scoped, mutable table -- ownership-only RLS isolation
(`app/rls.py`'s simple `RLS_STATEMENTS`/`POLICY_DEFINITIONS` tier, the same tier `documents`/
`conversations` use), NOT the heavier append-only/privilege-narrowed tier `life_intents`/
`_MAINAI_EXECUTION_TABLES` use. That heavier tier exists for MainAI's own immutable execution
audit trail; a calendar event is ordinary Founder data the Founder must be able to edit or
delete outright (fixing a mistaken entry), so `mainai_app` keeps its normal default
INSERT/SELECT/UPDATE/DELETE grant (via `ALTER DEFAULT PRIVILEGES`, see `db-init/01-app-role.
sh`) -- no REVOKE here, matching `documents`/`conversations` exactly, not `life_intents`.

Deliberately narrow for a first increment: no external-provider sync (Google Calendar/Outlook)
-- that is a real external integration decision (which provider, what credential/OAuth model,
what sync-conflict semantics) outside this directive's authority to decide unilaterally, and
is left as a later, separate task. `source` is a closed enum of just `"manual"` today so a
future external-source column change is additive, not a widening of an already-open set.
"""

from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE calendar_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title varchar(256) NOT NULL,
            description text,
            location varchar(256),
            starts_at timestamp NOT NULL,
            ends_at timestamp,
            status varchar(16) NOT NULL DEFAULT 'scheduled',
            source varchar(24) NOT NULL DEFAULT 'manual',
            cancelled_at timestamp,
            cancellation_reason text,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_calendar_events_title CHECK (length(btrim(title)) > 0),
            CONSTRAINT ck_calendar_events_times CHECK (ends_at IS NULL OR ends_at > starts_at),
            CONSTRAINT ck_calendar_events_status CHECK (status IN ('scheduled', 'cancelled', 'completed')),
            CONSTRAINT ck_calendar_events_source CHECK (source IN ('manual')),
            CONSTRAINT ck_calendar_events_cancellation CHECK (
                (status = 'cancelled' AND cancelled_at IS NOT NULL)
                OR (status != 'cancelled' AND cancelled_at IS NULL AND cancellation_reason IS NULL)
            )
        );

        CREATE INDEX ix_calendar_events_owner_starts_at ON calendar_events(owner_id, starts_at);
        CREATE INDEX ix_calendar_events_owner_status ON calendar_events(owner_id, status, starts_at);

        ALTER TABLE calendar_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE calendar_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY calendar_events_isolation ON calendar_events
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE calendar_events;")
