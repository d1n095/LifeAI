"""Blueprint approval authority — close the GUC-spoofing P1 (migration 0072).

Independent examiner finding (RETURN_TO_BUILDER) on `d13efd7`: migration 0071's
`blueprint_approve_draft` derived authority ONLY from `current_setting('app.current_user_id')` —
a custom GUC the runtime role (`mainai_app`) can set itself (it needs it for RLS). A raw
`mainai_app` connection could therefore `SET app.current_user_id = <founder uuid>` and call the
SECURITY DEFINER function (on which it holds EXECUTE), forging a Founder approval with an
arbitrary `approved_by`. A caller-writable session variable is not an authentication boundary
against the role that writes it.

Fix (reuse existing architecture; no new framework): anchor approval authority to the one thing
the runtime role CANNOT change about its own connection — `session_user`, the authenticated login
role. `SET SESSION AUTHORIZATION`/`SET ROLE` to escape it requires privileges `mainai_app` does
not have. The function now refuses, as its FIRST action, to run when the caller's `session_user`
is the runtime role. Legitimate Founder approval therefore must run on a privileged connection
whose login role is NOT `mainai_app` (the app's admin/migration connection — the same privileged-
session pattern app/worker.py already uses for storage-deletion claims); a compromised
`mainai_app` connection, even controlling every custom GUC, cannot reach the write.

Everything else from 0071 is preserved verbatim: owner is still scoped (now only reachable by a
trusted privileged caller), `SELECT ... FOR UPDATE` + status recheck still serialize concurrent
approvals, and the BEFORE UPDATE/DELETE guard trigger still makes an approved draft immutable and
undeletable. Signature is unchanged `(uuid, text)`. Additive + reversible (downgrade restores the
0071 body without the guard).
"""

from alembic import op

revision = "0072_blueprint_approve_identity"
down_revision = "0071_blueprint_approval_guard"
branch_labels = None
depends_on = None

# The runtime (application) DB login role. Must match APP_ROLE in
# backend/scripts/security/s1a_privilege_policy.py and db-init/01-app-role.sh.
_RUNTIME_ROLE = "mainai_app"

_GUARD = f"""
            -- P1 fix: the ordinary runtime role must never be able to authorize an approval,
            -- even though it holds EXECUTE and can set app.current_user_id. session_user is the
            -- authenticated login role; mainai_app cannot change it (no SET SESSION AUTHORIZATION/
            -- SET ROLE privilege), so this is a non-caller-forgeable identity boundary. Legitimate
            -- approval runs on the privileged admin connection (session_user <> mainai_app).
            IF session_user = '{_RUNTIME_ROLE}' THEN
                RAISE EXCEPTION 'unauthorized: Founder approval is not permitted from the runtime role (session_user=%)', session_user
                    USING ERRCODE = '42501';
            END IF;
"""


def _create(with_guard: bool) -> None:
    guard = _GUARD if with_guard else ""
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION blueprint_approve_draft(p_draft_id uuid, p_approved_by text)
        RETURNS uuid
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_owner uuid;
            v_status text;
        BEGIN
{guard}
            v_owner := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_owner IS NULL THEN
                RAISE EXCEPTION 'blueprint_approve_draft: no app.current_user_id in session'
                    USING ERRCODE = 'P0001';
            END IF;

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
                       jsonb_set(coalesce(provenance::jsonb, '{{}}'::jsonb), '{{pending_founder_approval}}', 'false'::jsonb, true),
                       '{{approved_by_founder}}', 'true'::jsonb, true))::json
             WHERE id = p_draft_id AND owner_id = v_owner;

            RETURN p_draft_id;
        END;
        $$;
        """
    )


def upgrade() -> None:
    _create(with_guard=True)


def downgrade() -> None:
    _create(with_guard=False)
