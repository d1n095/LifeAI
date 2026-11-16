"""Blueprint approval integrity — migration 0071_blueprint_approval_integrity.

Closes two P1 findings on the Founder Alpha Blueprint feature by moving approval integrity into
the DATABASE (not the Python service alone), following this repo's established pattern (guard
triggers for value-conditional immutability — cf. migration 0037's
trg_documents_guard_storage_immutable — plus an owner-scoped SECURITY DEFINER function — cf.
migration 0019's transition_own_memory_source — plus grant-level narrowing in
scripts/security/s1a_privilege_policy.py).

P1-1 — the runtime role (mainai_app) could forge approval, modify approved content, or delete a
protected (approved) record. Enforced here regardless of the app layer by:
  * `trg_blueprint_drafts_approval_guard` (BEFORE UPDATE/DELETE): an approved row is fully
    immutable and cannot be deleted; a draft→approved transition must be well-formed
    (approved_at + approved_by set) and must NOT alter the draft's content (title, version, body,
    owner, source ids/refs, query, model identity). status may only move draft→approved.
  * `blueprint_approve_draft(uuid, text)` SECURITY DEFINER: the single controlled approval path.
    In production mainai_app holds no direct UPDATE/DELETE on blueprint_drafts (see
    s1a_privilege_policy `_PROTECTED_TABLES` = SELECT,INSERT) and may only EXECUTE this function,
    so it cannot write approval state directly at all.

P1-2 — concurrent Founder approvals raced. The function does `SELECT ... FOR UPDATE` then
re-checks status inside the lock, so two concurrent approvals of the same draft serialize and
exactly one succeeds; the loser sees status='approved' and is rejected.

Purely additive + reversible (downgrade drops the trigger + function). Touches no existing table
structure; no data migration.
"""

from alembic import op

revision = "0071_blueprint_approval_guard"
down_revision = "0070_blueprint_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- integrity guard trigger (fires for EVERY writer, including mainai_app) ---
    op.execute(
        """
        CREATE OR REPLACE FUNCTION trg_blueprint_drafts_approval_guard() RETURNS TRIGGER
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF OLD.status = 'approved' THEN
                    RAISE EXCEPTION 'blueprint_drafts: an approved draft is a protected record and cannot be deleted (id=%)', OLD.id
                        USING ERRCODE = 'P0001';
                END IF;
                RETURN OLD;
            END IF;

            -- UPDATE
            IF OLD.status = 'approved' THEN
                RAISE EXCEPTION 'blueprint_drafts: an approved draft is immutable (id=%)', OLD.id
                    USING ERRCODE = 'P0001';
            END IF;

            IF NEW.status NOT IN ('draft', 'approved') THEN
                RAISE EXCEPTION 'blueprint_drafts: illegal status % (id=%)', NEW.status, OLD.id
                    USING ERRCODE = 'P0001';
            END IF;

            IF NEW.status = 'approved' THEN
                -- a well-formed approval records who approved and when, and never alters content
                IF NEW.approved_at IS NULL OR NEW.approved_by IS NULL THEN
                    RAISE EXCEPTION 'blueprint_drafts: approval must set approved_at and approved_by (id=%)', OLD.id
                        USING ERRCODE = 'P0001';
                END IF;
                IF NEW.title IS DISTINCT FROM OLD.title
                   OR NEW.version IS DISTINCT FROM OLD.version
                   OR NEW.owner_id IS DISTINCT FROM OLD.owner_id
                   OR NEW.body IS DISTINCT FROM OLD.body
                   OR NEW.generated_from_query IS DISTINCT FROM OLD.generated_from_query
                   OR NEW.model_provider IS DISTINCT FROM OLD.model_provider
                   OR NEW.model_name IS DISTINCT FROM OLD.model_name
                   OR NEW.source_document_ids::text IS DISTINCT FROM OLD.source_document_ids::text
                   OR NEW.source_refs::text IS DISTINCT FROM OLD.source_refs::text
                   OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                    RAISE EXCEPTION 'blueprint_drafts: approval must not modify draft content (id=%)', OLD.id
                        USING ERRCODE = 'P0001';
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_blueprint_drafts_approval_guard
            BEFORE UPDATE OR DELETE ON blueprint_drafts
            FOR EACH ROW EXECUTE FUNCTION trg_blueprint_drafts_approval_guard();
        """
    )

    # --- the single controlled approval path: owner-scoped, transaction-safe ---
    op.execute(
        """
        CREATE OR REPLACE FUNCTION blueprint_approve_draft(p_draft_id uuid, p_approved_by text)
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        -- pg_catalog only (the repo's SECURITY DEFINER hardening, verified by
        -- s1a_privilege_policy): unqualified names must not resolve against public or a
        -- caller-created pg_temp, so every object below is schema-qualified.
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_owner uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            v_status text;
        BEGIN
            IF v_owner IS NULL THEN
                RAISE EXCEPTION 'blueprint_approve_draft: no app.current_user_id in session'
                    USING ERRCODE = 'P0001';
            END IF;

            -- Row lock first (serializes concurrent approvals of the same draft), owner-scoped.
            SELECT status INTO v_status
              FROM public.blueprint_drafts
             WHERE id = p_draft_id AND owner_id = v_owner
             FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'blueprint_approve_draft: draft % not found for owner', p_draft_id
                    USING ERRCODE = 'P0002';
            END IF;
            IF v_status <> 'draft' THEN
                RAISE EXCEPTION 'blueprint_approve_draft: draft % is not in draft status (is %)', p_draft_id, v_status
                    USING ERRCODE = 'P0001';
            END IF;

            UPDATE public.blueprint_drafts
               SET status = 'approved',
                   approved_at = now(),
                   approved_by = p_approved_by,
                   provenance = (jsonb_set(
                       jsonb_set(coalesce(provenance::jsonb, '{}'::jsonb), '{pending_founder_approval}', 'false'::jsonb, true),
                       '{approved_by_founder}', 'true'::jsonb, true))::json
             WHERE id = p_draft_id AND owner_id = v_owner;

            RETURN p_draft_id;
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_blueprint_drafts_approval_guard ON blueprint_drafts")
    op.execute("DROP FUNCTION IF EXISTS trg_blueprint_drafts_approval_guard()")
    op.execute("DROP FUNCTION IF EXISTS blueprint_approve_draft(uuid, text)")
