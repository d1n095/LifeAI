"""System activation authorization — migration 0069.

Founder directive 2026-11-14 ("Resolve execution authority and security gaps"), Priority 2:
"An environment variable alone must not constitute production activation authority." This
table is the durable authorization EVIDENCE `app.agent_coordination.activation_gate.
require_system_activation_authority()` requires alongside (never instead of) the
`FIRST_CYCLE_GO` env var — see `app/models/agent_coordination.py`'s
`SystemActivationAuthorization` docstring for the full rationale.

Deliberately NOT owner-scoped (no owner_id, no RLS policy) — same treatment as
`coordination_agents` (migration 0046): whether the whole system is authorized to activate is
founder-wide state. The security property that matters here is narrower and stricter than
RLS: `mainai_app` (the restricted runtime role every request/worker actually runs as) is
REVOKEd everything except SELECT on this table — no application code path, however it is
reached, can ever INSERT, UPDATE or DELETE a row here. The only way a row exists is an
out-of-band action against the migration/admin connection, never the running application.
`app/rls.py`'s `apply_mainai_execution_privileges()` re-verifies this exact fact at every
boot, the same discipline already applied to every other privilege-narrowed table.
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE system_activation_authorizations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            authorized_by varchar(64) NOT NULL,
            reason text NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'active',
            authorized_at timestamp NOT NULL DEFAULT now(),
            expires_at timestamp,
            revoked_at timestamp,
            revoked_by varchar(64),
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_system_activation_authorizations_status CHECK (status IN ('active', 'revoked')),
            CONSTRAINT ck_system_activation_authorizations_revoked_fields CHECK (
                (status = 'active' AND revoked_at IS NULL AND revoked_by IS NULL)
                OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL)
            )
        );

        REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
            ON system_activation_authorizations FROM mainai_app;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE system_activation_authorizations;")
