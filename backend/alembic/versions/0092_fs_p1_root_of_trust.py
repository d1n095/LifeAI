"""Founder sovereignty P1-fix-4: close runtime root-of-trust writes.

Revision ID: 0092_fs_p1_root_of_trust
Revises: 0091_fs_p1_authority
Create Date: 2026-10-11

`mainai_app` must not UPDATE identity-critical user fields or INSERT refresh
tokens directly. Legitimate registration still INSERTs users. Login, password
reset, session revocation, and account erasure go through SECURITY DEFINER
functions or remaining row-scoped DML (refresh-token revoke/delete, user
DELETE).

Sibling collision (do not compose here): Continuous Conversation may also have
a 0092-prefixed revision. This lane stays
`0090_founder_sovereignty` → `0091_fs_p1_authority` → `0092_fs_p1_root_of_trust`.
Alembic `version_num` is varchar(32). Do not renumber, rebase, or independently
resolve that collision in this file.

Does not grant merge, deploy, Recall, or database-superuser authority.
SELF-SET GUC != AUTHORITY. PUBLIC FOUNDER UUID != AUTHORITY.
"""

from alembic import op

revision = "0092_fs_p1_root_of_trust"
down_revision = "0091_fs_p1_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION users_identity_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF current_user <> 'mainai_app' THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF NEW.password_hash IS DISTINCT FROM OLD.password_hash
                   OR NEW.role IS DISTINCT FROM OLD.role
                   OR NEW.is_active IS DISTINCT FROM OLD.is_active
                   OR NEW.sessions_valid_after IS DISTINCT FROM OLD.sessions_valid_after THEN
                    RAISE EXCEPTION 'identity-critical user fields are not writable by the runtime role';
                END IF;
                RETURN NEW;
            END IF;
            RETURN NEW;
        END $$;

        DROP TRIGGER IF EXISTS users_identity_guard ON users;
        CREATE TRIGGER users_identity_guard
            BEFORE UPDATE ON users
            FOR EACH ROW EXECUTE FUNCTION users_identity_guard();

        CREATE OR REPLACE FUNCTION refresh_tokens_session_guard() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog AS $$
        BEGIN
            IF current_user <> 'mainai_app' THEN
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'INSERT' THEN
                RAISE EXCEPTION 'refresh tokens cannot be inserted by the runtime role; use mint_refresh_token';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF NEW.user_id IS DISTINCT FROM OLD.user_id
                   OR NEW.family_id IS DISTINCT FROM OLD.family_id
                   OR NEW.token_hash IS DISTINCT FROM OLD.token_hash
                   OR NEW.access_jti IS DISTINCT FROM OLD.access_jti
                   OR NEW.csrf_token IS DISTINCT FROM OLD.csrf_token THEN
                    RAISE EXCEPTION 'refresh token identity fields are immutable to the runtime role';
                END IF;
                RETURN NEW;
            END IF;
            RETURN NEW;
        END $$;

        DROP TRIGGER IF EXISTS refresh_tokens_session_guard ON refresh_tokens;
        CREATE TRIGGER refresh_tokens_session_guard
            BEFORE INSERT OR UPDATE ON refresh_tokens
            FOR EACH ROW EXECUTE FUNCTION refresh_tokens_session_guard();

        CREATE OR REPLACE FUNCTION mint_refresh_token(
            p_id uuid,
            p_user_id uuid,
            p_family_id uuid,
            p_token_hash character varying,
            p_access_jti character varying,
            p_csrf_token character varying,
            p_created_at timestamp without time zone,
            p_expires_at timestamp without time zone,
            p_user_agent character varying,
            p_ip_address character varying,
            p_replaces_id uuid
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        BEGIN
            IF p_id IS NULL OR p_user_id IS NULL OR p_family_id IS NULL THEN
                RAISE EXCEPTION 'mint_refresh_token requires id, user_id, and family_id';
            END IF;
            IF p_token_hash IS NULL OR length(p_token_hash) < 32 THEN
                RAISE EXCEPTION 'mint_refresh_token requires a token hash';
            END IF;
            IF p_csrf_token IS NULL OR length(p_csrf_token) < 8 THEN
                RAISE EXCEPTION 'mint_refresh_token requires a CSRF token';
            END IF;
            IF p_expires_at IS NULL THEN
                RAISE EXCEPTION 'mint_refresh_token requires expires_at';
            END IF;
            IF NOT EXISTS (SELECT 1 FROM public.users WHERE id = p_user_id) THEN
                RAISE EXCEPTION 'mint_refresh_token: user not found';
            END IF;
            INSERT INTO public.refresh_tokens (
                id, user_id, family_id, token_hash, access_jti, csrf_token,
                created_at, expires_at, user_agent, ip_address, replaces_id
            ) VALUES (
                p_id,
                p_user_id,
                p_family_id,
                p_token_hash,
                p_access_jti,
                p_csrf_token,
                COALESCE(p_created_at, timezone('utc', now())),
                p_expires_at,
                p_user_agent,
                p_ip_address,
                p_replaces_id
            );
            RETURN p_id;
        END $$;

        CREATE OR REPLACE FUNCTION consume_password_reset_token_and_set_hash(
            p_token_hash character varying,
            p_new_hash character varying
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_token public.password_reset_tokens%ROWTYPE;
            v_user public.users%ROWTYPE;
        BEGIN
            IF p_new_hash IS NULL OR length(p_new_hash) < 16 THEN
                RAISE EXCEPTION 'password hash is required';
            END IF;
            SELECT * INTO v_token
              FROM public.password_reset_tokens
             WHERE token_hash = p_token_hash
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'password reset token is invalid';
            END IF;
            IF v_token.used_at IS NOT NULL
               OR v_token.expires_at < timezone('utc', now()) THEN
                RAISE EXCEPTION 'password reset token is invalid';
            END IF;
            SELECT * INTO v_user
              FROM public.users
             WHERE id = v_token.user_id
             FOR UPDATE;
            IF NOT FOUND OR v_user.is_active IS NOT TRUE THEN
                RAISE EXCEPTION 'password reset token is invalid';
            END IF;
            UPDATE public.password_reset_tokens
               SET used_at = timezone('utc', now())
             WHERE id = v_token.id;
            UPDATE public.users
               SET password_hash = p_new_hash,
                   sessions_valid_after = date_trunc('second', timezone('utc', now()))
             WHERE id = v_user.id;
            RETURN v_user.id;
        END $$;

        CREATE OR REPLACE FUNCTION bump_sessions_valid_after(p_user_id uuid) RETURNS void
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_session uuid;
        BEGIN
            v_session := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_session IS NULL OR v_session IS DISTINCT FROM p_user_id THEN
                RAISE EXCEPTION 'bump_sessions_valid_after requires the authenticated owner';
            END IF;
            UPDATE public.users
               SET sessions_valid_after = date_trunc('second', timezone('utc', now()))
             WHERE id = p_user_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'bump_sessions_valid_after: user not found';
            END IF;
        END $$;

        REVOKE ALL ON FUNCTION users_identity_guard() FROM PUBLIC;
        REVOKE ALL ON FUNCTION refresh_tokens_session_guard() FROM PUBLIC;
        REVOKE ALL ON FUNCTION mint_refresh_token(uuid, uuid, uuid, varchar, varchar, varchar, timestamp, timestamp, varchar, varchar, uuid) FROM PUBLIC;
        REVOKE ALL ON FUNCTION consume_password_reset_token_and_set_hash(varchar, varchar) FROM PUBLIC;
        REVOKE ALL ON FUNCTION bump_sessions_valid_after(uuid) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION mint_refresh_token(uuid, uuid, uuid, varchar, varchar, varchar, timestamp, timestamp, varchar, varchar, uuid) TO mainai_app;
        GRANT EXECUTE ON FUNCTION consume_password_reset_token_and_set_hash(varchar, varchar) TO mainai_app;
        GRANT EXECUTE ON FUNCTION bump_sessions_valid_after(uuid) TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS users_identity_guard ON users;
        DROP TRIGGER IF EXISTS refresh_tokens_session_guard ON refresh_tokens;
        DROP FUNCTION IF EXISTS bump_sessions_valid_after(uuid);
        DROP FUNCTION IF EXISTS consume_password_reset_token_and_set_hash(varchar, varchar);
        DROP FUNCTION IF EXISTS mint_refresh_token(uuid, uuid, uuid, varchar, varchar, varchar, timestamp, timestamp, varchar, varchar, uuid);
        DROP FUNCTION IF EXISTS refresh_tokens_session_guard();
        DROP FUNCTION IF EXISTS users_identity_guard();
        """
    )
