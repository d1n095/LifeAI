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

The REVOKE itself is deliberately NOT in this migration: `mainai_app` is a cluster-level role
created by a separate bootstrap step (`db-init/01-app-role.sh`), not by any migration, and is
not guaranteed to exist yet when migrations run in isolation (e.g. the CI "Alembic migration
check" job runs `alembic upgrade head` against a fresh database with no such role). Every
other privilege-narrowed table in this codebase follows the same rule — see
`app/rls.py`'s `apply_mainai_execution_privileges()`, which performs AND re-verifies this exact
REVOKE at every boot, the same discipline already applied to every other privilege-narrowed
table (that function is where this table's own REVOKE statement lives, not here).
"""

from alembic import op

revision = "0075"
down_revision = "0074_blueprint_lifecycle_guard"
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
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE system_activation_authorizations;")
