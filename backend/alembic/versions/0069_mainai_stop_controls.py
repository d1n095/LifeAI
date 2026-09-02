"""Durable MainAI stop controls — owner stop vs global emergency.

Revision 0069. Process-global module memory is NOT the source of truth.
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE mainai_stop_state (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            scope varchar(16) NOT NULL,
            owner_id uuid REFERENCES users(id) ON DELETE CASCADE,
            active boolean NOT NULL DEFAULT false,
            reason text NOT NULL DEFAULT '',
            sequence bigint NOT NULL DEFAULT 0,
            updated_at timestamp NOT NULL DEFAULT now(),
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            CONSTRAINT ck_mainai_stop_scope CHECK (scope IN ('owner', 'global')),
            CONSTRAINT ck_mainai_stop_owner_null CHECK (
                (scope = 'global' AND owner_id IS NULL)
                OR (scope = 'owner' AND owner_id IS NOT NULL)
            ),
            CONSTRAINT ck_mainai_stop_seq CHECK (sequence >= 0),
            CONSTRAINT ck_mainai_stop_prov_object CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE UNIQUE INDEX uq_mainai_stop_global
            ON mainai_stop_state ((scope)) WHERE scope = 'global';
        CREATE UNIQUE INDEX uq_mainai_stop_owner
            ON mainai_stop_state (owner_id) WHERE scope = 'owner';

        CREATE TABLE mainai_stop_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            scope varchar(16) NOT NULL,
            owner_id uuid,
            event_kind varchar(32) NOT NULL,
            sequence bigint NOT NULL,
            reason text NOT NULL,
            founder_ack text,
            clear_request_id uuid,
            actor_kind varchar(32) NOT NULL,
            provenance jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_mainai_stop_ev_scope CHECK (scope IN ('owner', 'global')),
            CONSTRAINT ck_mainai_stop_ev_kind CHECK (event_kind IN (
                'activate', 'clear', 'reject_clear', 'boot_blocked'
            )),
            CONSTRAINT ck_mainai_stop_ev_actor CHECK (actor_kind IN (
                'founder', 'operator', 'system'
            )),
            CONSTRAINT ck_mainai_stop_ev_prov CHECK (jsonb_typeof(provenance) = 'object')
        );
        CREATE UNIQUE INDEX uq_mainai_stop_clear_request
            ON mainai_stop_events (clear_request_id)
            WHERE clear_request_id IS NOT NULL;
        CREATE INDEX ix_mainai_stop_events_owner
            ON mainai_stop_events (owner_id, created_at DESC);

        -- Seed inactive global row so reads are stable.
        INSERT INTO mainai_stop_state (scope, owner_id, active, reason, sequence)
        VALUES ('global', NULL, false, '', 0);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS mainai_stop_events")
    op.execute("DROP TABLE IF EXISTS mainai_stop_state")
