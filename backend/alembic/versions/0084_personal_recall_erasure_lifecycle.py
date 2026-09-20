"""Governed Personal Recall account erasure lifecycle.

Revision ID: 0084_recall_erasure_lifecycle
Revises: 0083_recall_auth_boundary
Create Date: 2026-09-20
"""

from alembic import op

revision = "0084_recall_erasure_lifecycle"
down_revision = "0083_recall_auth_boundary"
branch_labels = None
depends_on = None

FOUNDER_USER_ID = "00000000-0000-0000-0000-000000000001"


_FOUNDER_AUTH_FUNCTION = f"""
CREATE OR REPLACE FUNCTION personal_recall_founder_authority_current(p_owner_id uuid)
RETURNS boolean LANGUAGE plpgsql STABLE AS $$
DECLARE
  v_current_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
  v_access_jti text := NULLIF(current_setting('app.personal_recall_founder_access_jti', true), '');
BEGIN
  IF p_owner_id <> '{FOUNDER_USER_ID}'::uuid OR v_current_owner IS DISTINCT FROM p_owner_id OR v_access_jti IS NULL THEN
    RETURN false;
  END IF;
  RETURN EXISTS (
    SELECT 1
    FROM users u
    JOIN refresh_tokens rt ON rt.user_id = u.id
    WHERE u.id = '{FOUNDER_USER_ID}'::uuid
      AND u.role = 'founder'
      AND u.is_active IS TRUE
      AND rt.access_jti = v_access_jti
      AND rt.revoked_at IS NULL
      AND rt.expires_at > now()
      AND rt.created_at > u.sessions_valid_after
  );
END $$;
"""


def upgrade() -> None:
    op.execute(_FOUNDER_AUTH_FUNCTION)
    op.execute("""
    CREATE OR REPLACE FUNCTION personal_recall_erasure_current(p_owner_id uuid)
    RETURNS boolean LANGUAGE plpgsql STABLE AS $$
    DECLARE
      v_current_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
    BEGIN
      RETURN current_setting('app.personal_recall_erasure_in_progress', true) = 'true'
         AND v_current_owner IS NOT NULL
         AND v_current_owner = p_owner_id;
    END $$;

    CREATE OR REPLACE FUNCTION personal_recall_key_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF personal_recall_erasure_current(OLD.owner_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'personal recall owner keys are not ordinarily deletable';
      END IF;
      IF TG_OP = 'UPDATE' AND personal_recall_erasure_current(OLD.owner_id) AND NEW.owner_id = OLD.owner_id THEN
        RETURN NEW;
      END IF;
      IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
        IF current_setting('app.personal_recall_key_authority', true) <> 'founder_authorized'
           OR NOT personal_recall_founder_authority_current(NEW.owner_id) THEN
          RAISE EXCEPTION 'personal recall key mutation requires current canonical founder authority';
        END IF;
      END IF;
      RETURN NEW;
    END $$;

    CREATE OR REPLACE FUNCTION personal_recall_grant_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN
        IF personal_recall_erasure_current(OLD.owner_id) THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'personal recall grants are revoked, not ordinarily deleted';
      END IF;
      IF TG_OP = 'INSERT' THEN
        IF current_setting('app.personal_recall_grant_authority', true) <> 'founder_authorized'
           OR NOT personal_recall_founder_authority_current(NEW.owner_id) THEN
          RAISE EXCEPTION 'personal recall grant creation requires current canonical founder authority';
        END IF;
      END IF;
      IF TG_OP = 'UPDATE' THEN
        IF OLD.owner_id <> NEW.owner_id THEN RAISE EXCEPTION 'grant owner is immutable'; END IF;
        IF OLD.session_id <> NEW.session_id THEN RAISE EXCEPTION 'grant session is immutable'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    """)


def downgrade() -> None:
    op.execute(_FOUNDER_AUTH_FUNCTION)
    op.execute("""
    CREATE OR REPLACE FUNCTION personal_recall_key_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'personal recall owner keys are not deletable'; END IF;
      IF TG_OP = 'INSERT' OR TG_OP = 'UPDATE' THEN
        IF current_setting('app.personal_recall_key_authority', true) <> 'founder_authorized'
           OR NOT personal_recall_founder_authority_current(NEW.owner_id) THEN
          RAISE EXCEPTION 'personal recall key mutation requires current canonical founder authority';
        END IF;
      END IF;
      RETURN NEW;
    END $$;

    CREATE OR REPLACE FUNCTION personal_recall_grant_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'personal recall grants are revoked, not deleted'; END IF;
      IF TG_OP = 'INSERT' THEN
        IF current_setting('app.personal_recall_grant_authority', true) <> 'founder_authorized'
           OR NOT personal_recall_founder_authority_current(NEW.owner_id) THEN
          RAISE EXCEPTION 'personal recall grant creation requires current canonical founder authority';
        END IF;
      END IF;
      IF TG_OP = 'UPDATE' THEN
        IF OLD.owner_id <> NEW.owner_id THEN RAISE EXCEPTION 'grant owner is immutable'; END IF;
        IF OLD.session_id <> NEW.session_id THEN RAISE EXCEPTION 'grant session is immutable'; END IF;
      END IF;
      RETURN NEW;
    END $$;
    DROP FUNCTION IF EXISTS personal_recall_erasure_current(uuid);
    """)
