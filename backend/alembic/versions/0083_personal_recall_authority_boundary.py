"""Tighten Personal Recall founder authority trigger boundary.

Revision ID: 0083_recall_auth_boundary
Revises: 0082_high_risk_evidence_claim
Create Date: 2026-09-20
"""

from alembic import op

revision = "0083_recall_auth_boundary"
down_revision = "0082_high_risk_evidence_claim"
branch_labels = None
depends_on = None

FOUNDER_USER_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute(f"""
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
    """)


def downgrade() -> None:
    op.execute("""
    CREATE OR REPLACE FUNCTION personal_recall_key_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'personal recall owner keys are not deletable'; END IF;
      IF TG_OP = 'INSERT' AND current_setting('app.personal_recall_key_authority', true) <> 'founder_authorized' THEN
        RAISE EXCEPTION 'personal recall key creation requires governed key authority';
      END IF;
      IF TG_OP = 'UPDATE' AND current_setting('app.personal_recall_key_authority', true) <> 'founder_authorized' THEN
        RAISE EXCEPTION 'personal recall key updates require governed key authority';
      END IF;
      RETURN NEW;
    END $$;
    CREATE OR REPLACE FUNCTION personal_recall_grant_guard()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'personal recall grants are revoked, not deleted'; END IF;
      IF TG_OP = 'INSERT' AND current_setting('app.personal_recall_grant_authority', true) <> 'founder_authorized' THEN
        RAISE EXCEPTION 'personal recall grant creation requires founder-authorized path';
      END IF;
      IF TG_OP = 'UPDATE' AND OLD.owner_id <> NEW.owner_id THEN RAISE EXCEPTION 'grant owner is immutable'; END IF;
      IF TG_OP = 'UPDATE' AND OLD.session_id <> NEW.session_id THEN RAISE EXCEPTION 'grant session is immutable'; END IF;
      RETURN NEW;
    END $$;
    DROP FUNCTION IF EXISTS personal_recall_founder_authority_current(uuid);
    """)
