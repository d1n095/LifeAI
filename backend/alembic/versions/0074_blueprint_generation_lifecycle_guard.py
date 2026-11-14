"""Extend the Blueprint approval guard trigger for the generating/failed lifecycle states
(migration 0074).

Migration 0073 added `idempotency_key`/`lease_expires_at` and the two new transient
`BlueprintDraft.status` values this feature needs ('generating', 'failed') -- but migration
0071's `trg_blueprint_drafts_approval_guard` independently rejects ANY update whose `NEW.status`
is not literally 'draft' or 'approved', regardless of who the writer is (`IF NEW.status NOT IN
('draft', 'approved') THEN RAISE EXCEPTION 'illegal status...'`). Reproduced directly in CI on
PR #389 (exact job failure: `blueprint_drafts: illegal status generating`) -- every
claim/finalize/fail transition in app/blueprint/service.py was rejected by this trigger, not by
a grant. This migration is the fix: a real regression correction, not a hypothetical hardening.

Deliberately does NOT widen mainai_app's grant on blueprint_drafts (still SELECT, INSERT only --
see scripts/security/s1a_privilege_policy.py's own documented reasoning, unchanged by this
migration). Mission 3's generating/draft/failed transitions instead run on the privileged
admin/migration connection, the exact same pattern `blueprint_approve_draft` already uses for
its one controlled write (see app/blueprint/service.py's `_run_privileged_blueprint_update`).
This migration only teaches the trigger that fires on EVERY writer -- privileged or not -- to
recognize the two new transient states, and adds the two invariants that make it safe to do so:

  * A transition INTO 'approved' must come from 'draft', never from 'generating' or 'failed' --
    `blueprint_approve_draft()` already enforces this itself (`IF v_status <> 'draft' THEN
    RAISE EXCEPTION`), so this is defense-in-depth for ANY future writer, not a new rule.
  * A transition INTO 'approved' is refused outright when `session_user = 'mainai_app'` --
    the exact migration-0072 anti-GUC-spoofing invariant, restated here at the trigger level
    (which already "fires for every writer, including mainai_app" per 0071's own docstring)
    instead of relying solely on the function's own internal check. mainai_app cannot reach
    this trigger via an approval UPDATE today (it holds no UPDATE grant at all), but recording
    the invariant in the trigger itself means it stays true even if that grant narrowing is
    ever revisited, rather than depending on one single control.

Purely additive to the trigger function body (CREATE OR REPLACE, same trigger); the already-
reviewed approved-row immutability, approved_at/approved_by/content-unchanged checks, and
`blueprint_approve_draft` itself are untouched. Reversible: downgrade restores migration 0071's
exact trigger body.
"""

from alembic import op

revision = "0074_blueprint_generation_lifecycle_guard"
down_revision = "0073_blueprint_draft_idempotency"
branch_labels = None
depends_on = None

_RUNTIME_ROLE = "mainai_app"


def _create(allow_lifecycle_states: bool) -> None:
    legal_statuses = "('draft', 'approved', 'generating', 'failed')" if allow_lifecycle_states else "('draft', 'approved')"
    lifecycle_guards = (
        f"""
            IF NEW.status = 'approved' AND OLD.status <> 'draft' THEN
                RAISE EXCEPTION 'blueprint_drafts: approval must transition from draft status, not % (id=%)', OLD.status, OLD.id
                    USING ERRCODE = 'P0001';
            END IF;
            IF NEW.status = 'approved' AND session_user = '{_RUNTIME_ROLE}' THEN
                RAISE EXCEPTION 'blueprint_drafts: a direct approval transition is not permitted from the runtime role (session_user=%)', session_user
                    USING ERRCODE = '42501';
            END IF;
        """
        if allow_lifecycle_states
        else ""
    )
    op.execute(
        f"""
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

            IF NEW.status NOT IN {legal_statuses} THEN
                RAISE EXCEPTION 'blueprint_drafts: illegal status % (id=%)', NEW.status, OLD.id
                    USING ERRCODE = 'P0001';
            END IF;
{lifecycle_guards}
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


def upgrade() -> None:
    _create(allow_lifecycle_states=True)


def downgrade() -> None:
    _create(allow_lifecycle_states=False)
