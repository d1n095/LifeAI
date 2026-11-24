"""Revenue & Compute Engine foundation (migration 0070).

Five changes backing app/billing/ (usage accounting, tiered limits, concurrency-safe
budget enforcement, revenue-backed compute allocation):

1. billing_tenants — the tenant registry. LifeAI is single-owner today (the founder);
   tenants exist so enterprise/account-family billing can be scoped per subject WITHOUT
   leaking budget across subjects (see allocation.py's isolation rule).
2. users.tenant_id — optional tenant membership (NULL = personal account).
3. usage_log extensions — the EXISTING per-call audit record (app/models/usage.py) gains
   tenant_id, agent, task, tier snapshot and kind (consumption|refund). It stays the only
   per-call record; app/billing/accounting.py is the ONLY writer.
4. usage_rollups — per-subject, per-period materialized totals (integer micro-USD) that
   budget enforcement reads. Derived from usage_log events by the same single writer, so
   there is exactly ONE accounting pipeline, never two contradictory ones.
5. revenue_ledger — append-only revenue entries that are explicitly either 'estimated'
   (unverified; e.g. ad-network-reported) or 'settled' (verified reference required).
   app/billing/allocation.py spends against SETTLED revenue ONLY.

All monetary values are integer micro-USD (see app/openai_consult/budgets.py's rationale:
no float rounding anywhere in enforcement).

NOTE FOR MIGRATION RECONCILIATION: the founder-alpha composition lanes number their
migrations 0069..0079 in a DISJOINT history. This revision (0070, down_revision=0069)
belongs to the Z.AI lane lineage and must be renumbered/re-chained by the composition
tooling when the lanes merge — it adds independent tables/columns and conflicts with
nothing.
"""

from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE billing_tenants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name varchar(128) NOT NULL,
            tier varchar(16) NOT NULL DEFAULT 'paid'
                CHECK (tier IN ('free', 'paid', 'enterprise')),
            daily_cost_cap_micros bigint NULL,
            monthly_cost_cap_micros bigint NULL,
            safety_margin_pct int NOT NULL DEFAULT 25
                CHECK (safety_margin_pct BETWEEN 0 AND 90),
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamp NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        ALTER TABLE users
            ADD COLUMN tenant_id uuid NULL REFERENCES billing_tenants(id) ON DELETE SET NULL
    """)
    op.execute("CREATE INDEX ix_users_tenant_id ON users (tenant_id)")

    op.execute("""
        ALTER TABLE usage_log
            ADD COLUMN tenant_id uuid NULL REFERENCES billing_tenants(id) ON DELETE SET NULL,
            ADD COLUMN agent varchar(64) NULL,
            ADD COLUMN task varchar(128) NULL,
            ADD COLUMN tier varchar(16) NOT NULL DEFAULT 'free'
                CHECK (tier IN ('free', 'paid', 'enterprise')),
            ADD COLUMN kind varchar(16) NOT NULL DEFAULT 'consumption'
                CHECK (kind IN ('consumption', 'refund'))
    """)
    op.execute("CREATE INDEX ix_usage_log_tenant_id ON usage_log (tenant_id)")
    op.execute("CREATE INDEX ix_usage_log_created_at ON usage_log (created_at)")

    op.execute("""
        CREATE TABLE usage_rollups (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            subject_type varchar(8) NOT NULL CHECK (subject_type IN ('user', 'tenant')),
            subject_id uuid NOT NULL,
            period_type varchar(8) NOT NULL CHECK (period_type IN ('day', 'month')),
            period_start date NOT NULL,
            tokens_in bigint NOT NULL DEFAULT 0 CHECK (tokens_in >= 0),
            tokens_out bigint NOT NULL DEFAULT 0 CHECK (tokens_out >= 0),
            cost_micros bigint NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_usage_rollups_subject_period
                UNIQUE (subject_type, subject_id, period_type, period_start)
        )
    """)
    op.execute("""
        CREATE INDEX ix_usage_rollups_subject_period
            ON usage_rollups (subject_type, subject_id, period_type, period_start)
    """)

    op.execute("""
        CREATE TABLE revenue_ledger (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            subject_type varchar(8) NOT NULL CHECK (subject_type IN ('tenant', 'platform')),
            subject_id uuid NULL,
            source varchar(32) NOT NULL
                CHECK (source IN ('subscription', 'ad_revenue', 'grant', 'manual')),
            status varchar(16) NOT NULL CHECK (status IN ('estimated', 'settled')),
            amount_micros bigint NOT NULL,
            period_start date NOT NULL,
            verified_ref varchar(256) NULL,
            note varchar(512) NULL,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT revenue_settled_requires_ref
                CHECK (status = 'estimated' OR verified_ref IS NOT NULL),
            CONSTRAINT platform_rows_have_no_subject CHECK (subject_type = 'platform' OR subject_id IS NOT NULL)
        )
    """)
    op.execute("""
        CREATE INDEX ix_revenue_ledger_subject
            ON revenue_ledger (subject_type, subject_id, status, period_start)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS revenue_ledger")
    op.execute("DROP TABLE IF EXISTS usage_rollups")
    op.execute("DROP INDEX IF EXISTS ix_usage_log_created_at")
    op.execute("DROP INDEX IF EXISTS ix_usage_log_tenant_id")
    op.execute("""
        ALTER TABLE usage_log
            DROP COLUMN IF EXISTS kind,
            DROP COLUMN IF EXISTS tier,
            DROP COLUMN IF EXISTS task,
            DROP COLUMN IF EXISTS agent,
            DROP COLUMN IF EXISTS tenant_id
    """)
    op.execute("DROP INDEX IF EXISTS ix_users_tenant_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS tenant_id")
    op.execute("DROP TABLE IF EXISTS billing_tenants")
