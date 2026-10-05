"""Founder sovereignty P1 authority: session-bound identity, binding squat,
self-unlock, policy heads, kernel table, approval snapshots, atomic ALLOW_ONCE,
irreversible revocation, restore_to_version, step-up, governed erasure.

Revision ID: 0091_fs_p1_authority
Revises: 0090_founder_sovereignty
Create Date: 2026-10-05

Does not grant merge, deploy, Recall, or database-superuser authority.
"""

from alembic import op

revision = "0091_fs_p1_authority"
down_revision = "0090_founder_sovereignty"
branch_labels = None
depends_on = None

FOUNDER_USER_ID = "00000000-0000-0000-0000-000000000001"

KERNEL_ROWS = (
    ("founder_identity", "MainAI binds to exactly one Founder identity. AI cannot become Founder."),
    ("founder_authentication", "Founder identity verification cannot be bypassed by policy."),
    ("owner_isolation", "Owner isolation / RLS cannot be disabled by founder-policy recovery."),
    ("audit_immutability", "Historical audit evidence and approval receipts cannot be rewritten."),
    ("no_silent_credentials", "Credentials cannot be granted silently through founder policy."),
    ("recall_default_off", "Personal Recall stays default-off. This package never activates it."),
    ("no_merge_deploy_superuser", "This subsystem does not grant merge, deploy, Recall, or database-superuser authority."),
)

CATALOG_ROWS = (
    ("lights.control", "home_lights", "control", "low"),
    ("calendar.create", "family", "create", "medium"),
    ("tv.control", "home_tv", "control", "low"),
    ("family_calendar.read", "family_calendar", "read", "medium"),
    ("family_calendar.create", "family_calendar", "create", "medium"),
    ("family_calendar.update", "family_calendar", "update", "medium"),
    ("family_calendar.create_event", "family_calendar", "create_event", "medium"),
    ("family_calendar.delete", "family_calendar", "delete", "medium"),
    ("shopping_list.update", "shopping_list", "update", "medium"),
    ("founder_private_calendar.read", "founder_private_calendar", "read", "founder_only"),
    ("purchases.create", "purchases", "create", "high"),
    ("economy.read", "economy", "read", "high"),
    ("private_documents.read", "private_documents", "read", "high"),
    ("vehicle.security", "vehicle", "security", "high"),
    ("account.settings", "account", "settings", "high"),
    ("mainai.policy.change", "mainai_policy", "change", "founder_only"),
    ("mainai.security.policy", "security_policy", "change", "founder_only"),
    ("credentials.read", "credentials", "read", "founder_only"),
    ("system.authority", "system", "authority", "founder_only"),
    ("agent_control.root", "agent_control", "root", "founder_only"),
    ("delegation.root", "delegation", "root", "founder_only"),
    ("founder.identity.change", "founder_identity", "change", "founder_only"),
)

OWNER_SCOPED_NEW = (
    "founder_step_up_receipts",
    "family_grant_consumption_receipts",
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE founder_instance_bindings
            DROP CONSTRAINT IF EXISTS ck_founder_instance_bindings_founder;
        ALTER TABLE founder_instance_bindings
            ADD CONSTRAINT ck_founder_instance_bindings_founder
            CHECK (
                founder_user_id = '{FOUNDER_USER_ID}'::uuid
                AND owner_id = founder_user_id
            );

        ALTER TABLE family_approval_requests
            ADD COLUMN IF NOT EXISTS requested_limits jsonb NOT NULL DEFAULT '{{}}'::jsonb,
            ADD COLUMN IF NOT EXISTS snapshot_hash varchar(64) NOT NULL DEFAULT '',
            ADD COLUMN IF NOT EXISTS session_id varchar(128),
            ADD COLUMN IF NOT EXISTS device_id varchar(128);
        ALTER TABLE family_approval_requests
            DROP CONSTRAINT IF EXISTS ck_family_approval_requests_limits;
        ALTER TABLE family_approval_requests
            ADD CONSTRAINT ck_family_approval_requests_limits
            CHECK (jsonb_typeof(requested_limits) = 'object');

        ALTER TABLE family_approval_receipts
            ADD COLUMN IF NOT EXISTS snapshot_hash varchar(64) NOT NULL DEFAULT '';

        ALTER TABLE family_capability_grants
            ADD COLUMN IF NOT EXISTS requested_data jsonb NOT NULL DEFAULT '{{}}'::jsonb;
        ALTER TABLE family_capability_grants
            DROP CONSTRAINT IF EXISTS ck_family_capability_grants_until;
        ALTER TABLE family_capability_grants
            ADD CONSTRAINT ck_family_capability_grants_until
            CHECK (expires_at IS NULL OR expires_at <= created_at + interval '366 days');

        ALTER TABLE userai_tenant_boundaries
            DROP CONSTRAINT IF EXISTS ck_userai_tenant_boundaries_kind;
        ALTER TABLE userai_tenant_boundaries
            ADD CONSTRAINT ck_userai_tenant_boundaries_kind CHECK (tenant_kind IN ('founder_mainai','userai_personal'));
        ALTER TABLE userai_tenant_boundaries
            DROP CONSTRAINT IF EXISTS ck_userai_tenant_boundaries_roots;
        ALTER TABLE userai_tenant_boundaries
            ADD CONSTRAINT ck_userai_tenant_boundaries_roots CHECK (
                tenant_kind <> 'userai_personal'
                OR (
                    memory_root NOT LIKE 'founder%'
                    AND policy_root NOT LIKE 'founder%'
                    AND memory_root NOT IN ('founder_private_memory','founder_tenant','founder_secrets','founder_authority')
                    AND policy_root NOT IN ('founder_policies','founder_policy_root','founder_authority')
                )
            );

        CREATE TABLE IF NOT EXISTS kernel_security_invariants (
            invariant_key varchar(64) PRIMARY KEY,
            statement text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS founder_step_up_receipts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            purpose varchar(64) NOT NULL,
            session_jti varchar(128) NOT NULL DEFAULT '',
            verified_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            consumed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_founder_step_up_purpose CHECK (purpose IN (
                'policy_rollback','workflow_unlock','permanent_high_risk_delegation','founder_only_capability_change'
            ))
        );

        CREATE TABLE IF NOT EXISTS family_capability_catalog (
            capability_key varchar(128) PRIMARY KEY,
            resource varchar(128) NOT NULL,
            action varchar(64) NOT NULL,
            risk_tier varchar(16) NOT NULL,
            CONSTRAINT ck_family_capability_catalog_risk CHECK (risk_tier IN ('low','medium','high','founder_only'))
        );

        CREATE TABLE IF NOT EXISTS family_grant_consumption_receipts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            grant_id uuid NOT NULL REFERENCES family_capability_grants(id),
            principal_id uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )

    for key, statement in KERNEL_ROWS:
        op.execute(
            f"""
            INSERT INTO kernel_security_invariants (invariant_key, statement)
            VALUES ('{key}', '{statement.replace("'", "''")}')
            ON CONFLICT (invariant_key) DO NOTHING
            """
        )
    for key, resource, action, risk in CATALOG_ROWS:
        op.execute(
            f"""
            INSERT INTO family_capability_catalog (capability_key, resource, action, risk_tier)
            VALUES ('{key}', '{resource}', '{action}', '{risk}')
            ON CONFLICT (capability_key) DO NOTHING
            """
        )

    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION founder_sovereignty_erasure_on() RETURNS boolean
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RETURN current_setting('app.founder_sovereignty_erasure', true) = 'on';
        END $$;

        CREATE OR REPLACE FUNCTION founder_authenticated_session_uid() RETURNS uuid
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            v_uid uuid;
            v_role text;
        BEGIN
            v_uid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_uid IS NULL THEN
                RAISE EXCEPTION 'Founder authority requires an authenticated session-bound identity';
            END IF;
            IF v_uid <> '{FOUNDER_USER_ID}'::uuid THEN
                RAISE EXCEPTION 'PUBLIC FOUNDER UUID != AUTHORITY; session is not the Founder';
            END IF;
            SELECT u.role::text INTO v_role FROM public.users u WHERE u.id = v_uid AND u.is_active = true;
            IF v_role IS DISTINCT FROM 'founder' THEN
                RAISE EXCEPTION 'governed Founder binding requires the authenticated Founder row';
            END IF;
            RETURN v_uid;
        END $$;

        CREATE OR REPLACE FUNCTION founder_sovereignty_deny_mutation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'append-only founder sovereignty history cannot be rewritten';
        END $$;

        CREATE OR REPLACE FUNCTION founder_instance_bindings_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' OR TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'Founder instance binding cannot be rewritten outside governed erasure';
            END IF;
            PERFORM public.founder_authenticated_session_uid();
            IF NEW.founder_user_id <> '{FOUNDER_USER_ID}'::uuid
               OR NEW.owner_id <> NEW.founder_user_id
               OR NEW.tenant_kind <> 'founder_mainai' THEN
                RAISE EXCEPTION 'only authenticated Founder bootstrap may create the Founder binding';
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION founder_policy_versions_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP IN ('UPDATE','DELETE') THEN
                RAISE EXCEPTION 'append-only founder sovereignty history cannot be rewritten';
            END IF;
            PERFORM public.founder_authenticated_session_uid();
            IF NEW.policy_class = 'kernel_security_invariant'
               OR EXISTS (SELECT 1 FROM public.kernel_security_invariants k WHERE k.invariant_key = NEW.policy_key) THEN
                RAISE EXCEPTION 'KERNEL_SECURITY_INVARIANT cannot be inserted or altered by runtime or founder-policy recovery';
            END IF;
            IF NEW.actor_kind <> 'founder' THEN
                RAISE EXCEPTION 'forged Founder policy version rejected';
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION kernel_security_invariants_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'kernel security invariants cannot be inserted or altered at runtime';
        END $$;

        CREATE OR REPLACE FUNCTION founder_policy_heads_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            v_key text;
            v_owner uuid;
            v_class text;
            v_step uuid;
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'founder policy heads cannot be deleted outside governed erasure';
            END IF;
            PERFORM public.founder_authenticated_session_uid();
            SELECT policy_key, owner_id, policy_class INTO v_key, v_owner, v_class
              FROM public.founder_policy_versions WHERE id = NEW.current_version_id;
            IF v_key IS NULL OR v_key IS DISTINCT FROM NEW.policy_key OR v_owner IS DISTINCT FROM NEW.owner_id THEN
                RAISE EXCEPTION 'policy head must point at a version of the same policy_key and owner';
            END IF;
            IF v_class = 'kernel_security_invariant' THEN
                RAISE EXCEPTION 'policy head cannot point at kernel invariant state';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF NEW.owner_id IS DISTINCT FROM OLD.owner_id OR NEW.policy_key IS DISTINCT FROM OLD.policy_key THEN
                    RAISE EXCEPTION 'policy head identity is immutable';
                END IF;
                IF OLD.workflow_locked IS TRUE AND NEW.workflow_locked IS FALSE THEN
                    SELECT r.id INTO v_step
                      FROM public.founder_step_up_receipts r
                     WHERE r.owner_id = NEW.owner_id
                       AND r.purpose IN ('workflow_unlock','policy_rollback')
                       AND r.consumed_at IS NULL
                       AND r.expires_at > now()
                       AND r.verified_at IS NOT NULL
                     ORDER BY r.verified_at DESC
                     LIMIT 1
                     FOR UPDATE SKIP LOCKED;
                    IF v_step IS NULL THEN
                        RAISE EXCEPTION 'workflow unlock requires a verified Founder step-up receipt';
                    END IF;
                    UPDATE public.founder_step_up_receipts
                       SET consumed_at = now()
                     WHERE id = v_step AND consumed_at IS NULL;
                    IF NOT FOUND THEN
                        RAISE EXCEPTION 'workflow unlock requires a verified Founder step-up receipt';
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION family_approval_requests_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            v_session uuid;
            v_tier text;
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'approval requests cannot be deleted outside governed erasure';
            END IF;
            v_session := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_session IS NULL THEN
                RAISE EXCEPTION 'approval requests require an authenticated session';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.principal_id IS DISTINCT FROM v_session AND v_session <> '{FOUNDER_USER_ID}'::uuid THEN
                    RAISE EXCEPTION 'family approval request principal must match the authenticated session';
                END IF;
                IF NEW.snapshot_hash IS NULL OR length(NEW.snapshot_hash) < 32 THEN
                    RAISE EXCEPTION 'approval request requires an immutable snapshot hash';
                END IF;
                SELECT risk_tier INTO v_tier FROM public.family_capability_catalog WHERE capability_key = NEW.capability_key;
                IF v_tier IS NULL THEN
                    RAISE EXCEPTION 'unknown family capability';
                END IF;
                IF v_tier = 'founder_only' THEN
                    RAISE EXCEPTION 'FAMILY MEMBER != CAPABILITY; FOUNDER_ONLY cannot be requested';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.principal_id IS DISTINCT FROM OLD.principal_id
               OR NEW.capability_key IS DISTINCT FROM OLD.capability_key
               OR NEW.resource IS DISTINCT FROM OLD.resource
               OR NEW.action IS DISTINCT FROM OLD.action
               OR NEW.scope IS DISTINCT FROM OLD.scope
               OR NEW.requested_duration IS DISTINCT FROM OLD.requested_duration
               OR NEW.requested_data IS DISTINCT FROM OLD.requested_data
               OR NEW.requested_limits IS DISTINCT FROM OLD.requested_limits
               OR NEW.consequences IS DISTINCT FROM OLD.consequences
               OR NEW.risk_tier IS DISTINCT FROM OLD.risk_tier
               OR NEW.snapshot_hash IS DISTINCT FROM OLD.snapshot_hash
               OR NEW.session_id IS DISTINCT FROM OLD.session_id
               OR NEW.device_id IS DISTINCT FROM OLD.device_id
               OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
               OR NEW.request_token_hash IS DISTINCT FROM OLD.request_token_hash THEN
                RAISE EXCEPTION 'APPROVED DISPLAY != MUTABLE ROW; approval snapshot fields are immutable';
            END IF;
            IF OLD.status <> 'pending' AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION 'approval request status cannot be rewritten after decision';
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION family_capability_grants_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        DECLARE
            v_tier text;
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'capability grants cannot be deleted outside governed erasure';
            END IF;
            IF TG_OP = 'INSERT' THEN
                PERFORM public.founder_authenticated_session_uid();
                SELECT risk_tier INTO v_tier FROM public.family_capability_catalog WHERE capability_key = NEW.capability_key;
                IF v_tier IS NULL OR v_tier = 'founder_only' THEN
                    RAISE EXCEPTION 'family capability cannot become Founder capability';
                END IF;
                IF NEW.principal_id = '{FOUNDER_USER_ID}'::uuid THEN
                    RAISE EXCEPTION 'FAMILY MEMBER != FOUNDER';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.capability_key IS DISTINCT FROM OLD.capability_key
               OR NEW.resource IS DISTINCT FROM OLD.resource
               OR NEW.action IS DISTINCT FROM OLD.action
               OR NEW.scope IS DISTINCT FROM OLD.scope
               OR NEW.principal_id IS DISTINCT FROM OLD.principal_id
               OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
               OR NEW.issuer_id IS DISTINCT FROM OLD.issuer_id
               OR NEW.approval_receipt_id IS DISTINCT FROM OLD.approval_receipt_id
               OR NEW.duration_mode IS DISTINCT FROM OLD.duration_mode
               OR NEW.limits IS DISTINCT FROM OLD.limits
               OR NEW.requested_data IS DISTINCT FROM OLD.requested_data
               OR NEW.blocked_future IS DISTINCT FROM OLD.blocked_future THEN
                RAISE EXCEPTION 'authority-bearing grant fields are immutable; a changed capability requires a new grant';
            END IF;
            IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS NULL THEN
                RAISE EXCEPTION 'revoked grants cannot be reactivated';
            END IF;
            IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
                RAISE EXCEPTION 'revoked grants cannot be rewritten';
            END IF;
            IF OLD.remaining_uses IS NOT NULL AND NEW.remaining_uses IS NOT NULL
               AND NEW.remaining_uses > OLD.remaining_uses THEN
                RAISE EXCEPTION 'grant remaining_uses cannot be increased';
            END IF;
            IF OLD.expires_at IS NOT NULL AND NEW.expires_at IS NOT NULL AND NEW.expires_at > OLD.expires_at THEN
                RAISE EXCEPTION 'grant expiry cannot be extended by runtime';
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION family_capability_catalog_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            RAISE EXCEPTION 'family capability catalog cannot be mutated at runtime';
        END $$;

        CREATE OR REPLACE FUNCTION userai_tenant_boundaries_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'UserAI boundary cannot be deleted outside governed erasure';
            END IF;
            PERFORM public.founder_authenticated_session_uid();
            IF NEW.tenant_kind = 'userai_personal' AND (
                NEW.memory_root LIKE 'founder%' OR NEW.policy_root LIKE 'founder%'
            ) THEN
                RAISE EXCEPTION 'UserAI cannot imply Founder tenant, memory, policy root, secrets, or authority';
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION founder_step_up_receipts_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'step-up receipts cannot be deleted outside governed erasure';
            END IF;
            IF TG_OP = 'INSERT' THEN
                PERFORM public.founder_authenticated_session_uid();
                IF NEW.owner_id <> '{FOUNDER_USER_ID}'::uuid THEN
                    RAISE EXCEPTION 'step-up receipts belong to the Founder';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.purpose IS DISTINCT FROM OLD.purpose
               OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
               OR NEW.session_jti IS DISTINCT FROM OLD.session_jti
               OR NEW.verified_at IS DISTINCT FROM OLD.verified_at
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at THEN
                RAISE EXCEPTION 'step-up receipt identity is immutable';
            END IF;
            IF OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at THEN
                RAISE EXCEPTION 'consumed step-up receipts cannot be rewritten';
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION family_members_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'family members cannot be deleted outside governed erasure';
            END IF;
            PERFORM public.founder_authenticated_session_uid();
            IF NEW.principal_id = '00000000-0000-0000-0000-000000000001'::uuid THEN
                RAISE EXCEPTION 'FAMILY MEMBER != FOUNDER';
            END IF;
            RETURN NEW;
        END $$;

        CREATE OR REPLACE FUNCTION family_grant_consumption_receipts_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF public.founder_sovereignty_erasure_on() THEN
                IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
                RETURN NEW;
            END IF;
            IF TG_OP IN ('UPDATE','DELETE') THEN
                RAISE EXCEPTION 'grant consumption receipts are append-only';
            END IF;
            RETURN NEW;
        END $$;

        DROP TRIGGER IF EXISTS founder_instance_bindings_guard ON founder_instance_bindings;
        CREATE TRIGGER founder_instance_bindings_guard
            BEFORE INSERT OR UPDATE OR DELETE ON founder_instance_bindings
            FOR EACH ROW EXECUTE FUNCTION founder_instance_bindings_guard();

        DROP TRIGGER IF EXISTS founder_policy_versions_deny_mutation ON founder_policy_versions;
        DROP TRIGGER IF EXISTS founder_policy_versions_guard ON founder_policy_versions;
        CREATE TRIGGER founder_policy_versions_guard
            BEFORE INSERT OR UPDATE OR DELETE ON founder_policy_versions
            FOR EACH ROW EXECUTE FUNCTION founder_policy_versions_guard();

        DROP TRIGGER IF EXISTS kernel_security_invariants_guard ON kernel_security_invariants;
        CREATE TRIGGER kernel_security_invariants_guard
            BEFORE INSERT OR UPDATE OR DELETE ON kernel_security_invariants
            FOR EACH ROW EXECUTE FUNCTION kernel_security_invariants_guard();

        DROP TRIGGER IF EXISTS founder_policy_heads_guard ON founder_policy_heads;
        CREATE TRIGGER founder_policy_heads_guard
            BEFORE INSERT OR UPDATE OR DELETE ON founder_policy_heads
            FOR EACH ROW EXECUTE FUNCTION founder_policy_heads_guard();

        DROP TRIGGER IF EXISTS family_approval_requests_guard ON family_approval_requests;
        CREATE TRIGGER family_approval_requests_guard
            BEFORE INSERT OR UPDATE OR DELETE ON family_approval_requests
            FOR EACH ROW EXECUTE FUNCTION family_approval_requests_guard();

        DROP TRIGGER IF EXISTS family_capability_grants_guard ON family_capability_grants;
        CREATE TRIGGER family_capability_grants_guard
            BEFORE INSERT OR UPDATE OR DELETE ON family_capability_grants
            FOR EACH ROW EXECUTE FUNCTION family_capability_grants_guard();

        DROP TRIGGER IF EXISTS family_capability_catalog_guard ON family_capability_catalog;
        CREATE TRIGGER family_capability_catalog_guard
            BEFORE INSERT OR UPDATE OR DELETE ON family_capability_catalog
            FOR EACH ROW EXECUTE FUNCTION family_capability_catalog_guard();

        DROP TRIGGER IF EXISTS userai_tenant_boundaries_guard ON userai_tenant_boundaries;
        CREATE TRIGGER userai_tenant_boundaries_guard
            BEFORE INSERT OR UPDATE OR DELETE ON userai_tenant_boundaries
            FOR EACH ROW EXECUTE FUNCTION userai_tenant_boundaries_guard();

        DROP TRIGGER IF EXISTS founder_step_up_receipts_guard ON founder_step_up_receipts;
        CREATE TRIGGER founder_step_up_receipts_guard
            BEFORE INSERT OR UPDATE OR DELETE ON founder_step_up_receipts
            FOR EACH ROW EXECUTE FUNCTION founder_step_up_receipts_guard();

        DROP TRIGGER IF EXISTS family_members_guard ON family_members;
        CREATE TRIGGER family_members_guard
            BEFORE INSERT OR UPDATE OR DELETE ON family_members
            FOR EACH ROW EXECUTE FUNCTION family_members_guard();

        DROP TRIGGER IF EXISTS family_grant_consumption_receipts_guard ON family_grant_consumption_receipts;
        CREATE TRIGGER family_grant_consumption_receipts_guard
            BEFORE INSERT OR UPDATE OR DELETE ON family_grant_consumption_receipts
            FOR EACH ROW EXECUTE FUNCTION family_grant_consumption_receipts_guard();

        CREATE OR REPLACE FUNCTION consume_family_capability_grant_once(p_grant_id uuid) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_session uuid;
            v_id uuid;
            v_owner uuid;
            v_principal uuid;
        BEGIN
            v_session := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_session IS NULL THEN
                RAISE EXCEPTION 'ALLOW_ONCE consumption requires an authenticated session';
            END IF;
            UPDATE public.family_capability_grants g
               SET remaining_uses = g.remaining_uses - 1
             WHERE g.id = p_grant_id
               AND g.remaining_uses > 0
               AND g.revoked_at IS NULL
               AND g.blocked_future = false
               AND (g.expires_at IS NULL OR g.expires_at > now())
               AND g.principal_id = v_session
            RETURNING g.id, g.owner_id, g.principal_id INTO v_id, v_owner, v_principal;
            IF v_id IS NULL THEN
                RAISE EXCEPTION 'allow_once_exhausted';
            END IF;
            INSERT INTO public.family_grant_consumption_receipts (owner_id, grant_id, principal_id)
            VALUES (v_owner, v_id, v_principal);
            RETURN v_id;
        END $$;

        CREATE OR REPLACE FUNCTION erase_own_founder_sovereignty_children() RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_owner uuid;
        BEGIN
            v_owner := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner IS NULL THEN
                RAISE EXCEPTION 'erase_own_founder_sovereignty_children requires an authenticated app.current_user_id session context.';
            END IF;
            PERFORM set_config('app.founder_sovereignty_erasure', 'on', true);
            DELETE FROM public.family_grant_consumption_receipts WHERE owner_id = v_owner;
            DELETE FROM public.family_capability_grants WHERE owner_id = v_owner;
            DELETE FROM public.family_approval_receipts WHERE owner_id = v_owner;
            DELETE FROM public.family_approval_requests WHERE owner_id = v_owner;
            DELETE FROM public.family_members WHERE owner_id = v_owner;
            DELETE FROM public.founder_policy_heads WHERE owner_id = v_owner;
            DELETE FROM public.founder_policy_proposals WHERE owner_id = v_owner;
            DELETE FROM public.founder_policy_versions WHERE owner_id = v_owner;
            DELETE FROM public.founder_step_up_receipts WHERE owner_id = v_owner;
            DELETE FROM public.userai_tenant_boundaries WHERE owner_id = v_owner;
            DELETE FROM public.founder_instance_bindings WHERE owner_id = v_owner;
            PERFORM set_config('app.founder_sovereignty_erasure', 'off', true);
        END $$;

        REVOKE ALL ON FUNCTION consume_family_capability_grant_once(uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION erase_own_founder_sovereignty_children() FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION consume_family_capability_grant_once(uuid) TO mainai_app;
        GRANT EXECUTE ON FUNCTION erase_own_founder_sovereignty_children() TO mainai_app;
        """
    )

    for table in OWNER_SCOPED_NEW:
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
    op.execute("DROP FUNCTION IF EXISTS erase_own_founder_sovereignty_children()")
    op.execute("DROP FUNCTION IF EXISTS consume_family_capability_grant_once(uuid)")
    op.execute("DROP TABLE IF EXISTS family_grant_consumption_receipts CASCADE")
    op.execute("DROP TABLE IF EXISTS founder_step_up_receipts CASCADE")
    op.execute("DROP TABLE IF EXISTS family_capability_catalog CASCADE")
    op.execute("DROP TABLE IF EXISTS kernel_security_invariants CASCADE")
