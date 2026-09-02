"""Durable workforce authority-epoch table — closes the grant/kill-switch race (migration 0069).

THE RACE THIS CLOSES (P0, found by red-team review of PR #234's activation-prep changes,
follow-on to PR #239's owner-scoping fix): `activate_kill_switch()`'s "revoke all live
assignments" SELECT was not serialized in any way against a genuinely concurrent NEW
assignment grant (`app/workforce/broker.py`'s `resolve_delegation()`, which creates a
`WorkforceAssignment` with `status="assigned"`) happening on a separate DB connection.
Reproduced window: kill switch's own live-assignments SELECT returns empty, the concurrent
grant fully commits, then the kill switch's own commit lands — the just-granted assignment
survives PERMANENTLY as live, unrevoked execution authority while the kill switch itself
reports `active=True`. `app.workforce.kill_switch.prove_no_reusable_live_authority()`
(the system's own safety-invariant oracle) correctly returns `False` for that state.

FIX: every scope (a global row + one row per owner, created lazily) gets a durable
`authority_epoch` row here. The grant path (`broker.resolve_delegation`, via
`kill_switch.assert_grant_allowed`) takes a `SELECT ... FOR SHARE` row lock on the GLOBAL
row and then its own owner's row, in the SAME transaction that inserts the new
`WorkforceAssignment`, and refuses the grant if `stopped` is already true. The stop path
(`activate_kill_switch` / `activate_global_kill_switch`) takes a conflicting
`SELECT ... FOR UPDATE` on the same row(s) as part of the SAME transaction that revokes
live assignments and flips `stopped`. Postgres's own row-lock manager — not application
timing — then enforces one strict ordering between any one grant and any one stop for a
given scope: whichever transaction's lock request is granted first is authoritative, and
the other transaction can only proceed after the first has fully committed (or rolled
back), at which point it re-reads the row it just locked and observes the winner's
committed state. `FOR SHARE` (not `FOR UPDATE`) is used on the grant side deliberately —
many concurrent grants across many owners must not serialize against each other, only
against an actual stop, which is comparatively rare.

Durability: this state lives in Postgres, not a process-local Python dict (the previous
`_STATE`/`_GLOBAL_STATE` module globals in `app/workforce/kill_switch.py`, still present
after PR #239's owner-scoping fix). A stop committed by one process (one uvicorn/gunicorn
worker) is now immediately visible to every other process via a plain read-committed
SELECT — no cross-process staleness window, and a "process restart between operations"
never resets safety-relevant state, because there is no process-local state left to reset.

Not RLS-scoped: this is system safety-control state (an epoch counter, a stopped flag, a
reason string), not tenant content, and it MUST remain lockable/readable across owner
scopes — the grant path for owner X always needs to lock and read the GLOBAL row
regardless of which owner's request set `app.current_user_id`. Access is still
owner-scoped in practice: every code path here is reached only with an explicit
`owner_id` from an already-authenticated caller, matching the access-control level the
prior in-memory `dict[owner_id, KillSwitchState]` had (RLS would additionally have to
special-case the GLOBAL row's NULL owner_id to avoid hiding it from every owner-scoped
session, so it buys no real isolation this table doesn't already have to enforce a
different way).
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE workforce_authority_epoch (
            scope_key varchar(64) PRIMARY KEY,
            -- 'GLOBAL' for the single true-global-stop row (owner_id NULL); otherwise
            -- str(owner_id) for that owner's own row.
            owner_id uuid REFERENCES users(id) ON DELETE CASCADE,
            epoch integer NOT NULL DEFAULT 0,
            stopped boolean NOT NULL DEFAULT false,
            reason text,
            activated_at timestamp,
            revoked_assignment_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT ck_workforce_authority_epoch_scope CHECK (
                (scope_key = 'GLOBAL' AND owner_id IS NULL)
                OR (scope_key <> 'GLOBAL' AND owner_id IS NOT NULL AND scope_key = owner_id::text)
            ),
            CONSTRAINT ck_workforce_authority_epoch_nonneg CHECK (epoch >= 0),
            CONSTRAINT ck_workforce_authority_epoch_revoked_array
                CHECK (jsonb_typeof(revoked_assignment_ids) = 'array')
        );

        -- Seed the single GLOBAL row up front so both the grant path's FOR SHARE lock and
        -- the global-stop path's FOR UPDATE lock always have a row to lock without a racy
        -- get-or-create on first use. Per-owner rows are still created lazily (an owner
        -- that has never been granted or stopped needs no row: "missing" == "not stopped").
        INSERT INTO workforce_authority_epoch (scope_key, owner_id, epoch, stopped)
        VALUES ('GLOBAL', NULL, 0, false)
        ON CONFLICT (scope_key) DO NOTHING;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS workforce_authority_epoch CASCADE")
