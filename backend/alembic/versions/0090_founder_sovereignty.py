"""Founder sovereignty + family delegation foundation.

Child of 0089_continuous_conversation. Not another sibling 0089.

Revision ID: 0090_founder_sovereignty
Revises: 0089_continuous_conversation
Create Date: 2026-10-02

This subsystem issues scoped Founder-approved capabilities only. It does not
grant merge, deploy, Recall activation, or database-superuser authority.
"""

from alembic import op

revision = "0090_founder_sovereignty"
down_revision = "0089_continuous_conversation"
branch_labels = None
depends_on = None

FOUNDER_USER_ID = "00000000-0000-0000-0000-000000000001"

OWNER_SCOPED_TABLES = (
    "founder_instance_bindings",
    "founder_policy_versions",
    "founder_policy_heads",
    "founder_policy_proposals",
    "family_members",
    "family_approval_requests",
    "family_approval_receipts",
    "family_capability_grants",
    "userai_tenant_boundaries",
)

APPEND_ONLY = ("founder_policy_versions", "family_approval_receipts")


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE founder_instance_bindings (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            founder_user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
            tenant_kind varchar(32) NOT NULL DEFAULT 'founder_mainai',
            bound_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_founder_instance_bindings_founder
                CHECK (founder_user_id = '{FOUNDER_USER_ID}'::uuid),
            CONSTRAINT ck_founder_instance_bindings_tenant
                CHECK (tenant_kind = 'founder_mainai'),
            CONSTRAINT uq_founder_instance_bindings_singleton UNIQUE (tenant_kind)
        );

        CREATE TABLE founder_policy_versions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            policy_key varchar(128) NOT NULL,
            policy_class varchar(32) NOT NULL,
            version_number integer NOT NULL,
            payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            reason text NOT NULL DEFAULT '',
            actor_kind varchar(32) NOT NULL,
            previous_version_id uuid REFERENCES founder_policy_versions(id),
            approval_receipt_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_founder_policy_versions_class
                CHECK (policy_class IN ('kernel_security_invariant','founder_policy','runtime_preference')),
            CONSTRAINT ck_founder_policy_versions_actor
                CHECK (actor_kind = 'founder'),
            CONSTRAINT ck_founder_policy_versions_payload CHECK (jsonb_typeof(payload) = 'object'),
            CONSTRAINT uq_founder_policy_versions_key_n UNIQUE (owner_id, policy_key, version_number)
        );

        CREATE TABLE founder_policy_heads (
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            policy_key varchar(128) NOT NULL,
            current_version_id uuid NOT NULL REFERENCES founder_policy_versions(id),
            workflow_locked boolean NOT NULL DEFAULT false,
            updated_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (owner_id, policy_key)
        );

        CREATE TABLE founder_policy_proposals (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            policy_key varchar(128) NOT NULL,
            payload jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            source varchar(32) NOT NULL,
            applied boolean NOT NULL DEFAULT false,
            notes text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_founder_policy_proposals_source CHECK (source IN (
                'founder','ai_proposal','remote_agent','bug_report','prompt_injection','model_output'
            )),
            CONSTRAINT ck_founder_policy_proposals_payload CHECK (jsonb_typeof(payload) = 'object')
        );

        CREATE TABLE family_members (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            principal_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            relationship varchar(16) NOT NULL,
            display_name varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_family_members_relationship CHECK (relationship IN ('partner','child','other')),
            CONSTRAINT ck_family_members_not_founder CHECK (principal_id <> '{FOUNDER_USER_ID}'::uuid),
            CONSTRAINT uq_family_members_principal UNIQUE (owner_id, principal_id)
        );

        CREATE TABLE family_approval_requests (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            principal_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            capability_key varchar(128) NOT NULL,
            resource varchar(128) NOT NULL,
            action varchar(64) NOT NULL,
            scope varchar(64) NOT NULL DEFAULT 'family',
            requested_duration varchar(64) NOT NULL DEFAULT 'once',
            requested_data jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            consequences text NOT NULL DEFAULT '',
            risk_tier varchar(16) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'pending',
            request_token_hash varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_family_approval_requests_status CHECK (status IN ('pending','decided','expired')),
            CONSTRAINT ck_family_approval_requests_risk CHECK (risk_tier IN ('low','medium','high','founder_only'))
        );

        CREATE TABLE family_approval_receipts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            request_id uuid NOT NULL REFERENCES family_approval_requests(id) ON DELETE CASCADE,
            decision varchar(32) NOT NULL,
            receipt_token_hash varchar(128) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_family_approval_receipts_decision CHECK (decision IN (
                'allow_once','allow_for_duration','allow_until_date','always_allow',
                'deny','deny_and_block','review_exact_action'
            ))
        );

        CREATE TABLE family_capability_grants (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            principal_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            capability_key varchar(128) NOT NULL,
            resource varchar(128) NOT NULL,
            action varchar(64) NOT NULL,
            scope varchar(64) NOT NULL DEFAULT 'family',
            duration_mode varchar(32) NOT NULL,
            expires_at timestamptz,
            remaining_uses integer,
            limits jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            issuer_id uuid NOT NULL REFERENCES users(id),
            approval_receipt_id uuid REFERENCES family_approval_receipts(id),
            session_id varchar(128),
            revoked_at timestamptz,
            blocked_future boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_family_capability_grants_limits CHECK (jsonb_typeof(limits) = 'object')
        );

        CREATE TABLE userai_tenant_boundaries (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tenant_kind varchar(32) NOT NULL,
            memory_root varchar(128) NOT NULL,
            policy_root varchar(128) NOT NULL,
            notes text NOT NULL DEFAULT '',
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_userai_tenant_boundaries_kind CHECK (tenant_kind IN ('founder_mainai','userai_personal'))
        );

        CREATE OR REPLACE FUNCTION founder_sovereignty_deny_mutation() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'append-only founder sovereignty history cannot be rewritten';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER founder_policy_versions_deny_mutation
            BEFORE UPDATE OR DELETE ON founder_policy_versions
            FOR EACH ROW EXECUTE FUNCTION founder_sovereignty_deny_mutation();
        CREATE TRIGGER family_approval_receipts_deny_mutation
            BEFORE UPDATE OR DELETE ON family_approval_receipts
            FOR EACH ROW EXECUTE FUNCTION founder_sovereignty_deny_mutation();
        """
    )
    for table in OWNER_SCOPED_TABLES:
        op.execute(
            f"""
            ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
            ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
            CREATE POLICY {table}_isolation ON {table}
                USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
                WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
            """
        )


def downgrade() -> None:
    for table in reversed(OWNER_SCOPED_TABLES):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP FUNCTION IF EXISTS founder_sovereignty_deny_mutation()")
