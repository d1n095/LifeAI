"""V0.2 recovery pipeline -- one new `mainai_recovery_events.event_type` value:
`takeover_declined_governed`.

WHY THIS EXISTS: `app/mainai_execution/recovery_takeover.py`'s `execute_takeover()` is the
ONE place a dead `task_execution` job's ownership can be transferred to a new attempt. Its
existing mechanism (`reset_task_for_takeover()` + `dispatch_ready_task()`) resumes the task
through V0.1's own generic, envelope-blind dispatch -- exactly the same `dispatch_ready_task()`
call `app.worker.py`'s `_advance_mainai_execution_tasks()` was fixed (migration 0057/0058/0059,
PR #154) to never make for a goal that has ever been execution-envelope-governed. That fix
lives entirely in `_advance_mainai_execution_tasks()`; `execute_takeover()` is a second,
independent caller of the same `dispatch_ready_task()` and was never covered by it -- so a
Supervisor-governed task whose job died could still be resumed by the automatic dead-agent-
recovery tick (or a founder's own `POST /tasks/{id}/recover`) with none of SupervisorScope's
`allowed_paths`/`allowed_capabilities`/`maximum_risk` narrowing. RECOVERY MUST NEVER INCREASE
OR BYPASS AUTHORITY.

`execute_takeover()` now declines the V0.1 takeover mechanism for any goal that has ever had
an `execution_authorization_envelopes` row (mere existence, any status -- same durable
EVER_GOVERNED fact migration 0057's own "never mutate, always supersede" discipline already
establishes elsewhere) and instead returns the task to `ready` with no fabricated verdict, so
the goal's OWN Supervisor tick rediscovers and redispatches it through `prepare_context()`'s
real `OperatorContext` binding, re-validated against whatever the CURRENT active envelope
authorizes at that later moment.

Adds exactly one new allowed `mainai_recovery_events.event_type` value -- no new table, no new
column, mirrors migration 0035's exact ALTER CONSTRAINT shape.

DOWNGRADE IS ONLY SAFE WHILE NO ROW HAS EVER RECORDED `takeover_declined_governed` -- proven,
not assumed (see `test_downgrading_past_this_migration_fails_loudly_once_a_real_row_exists_
not_silently` in test_recovery_takeover_authority_fencing.py, which inserts a genuine row via
the real `execute_takeover()` code path against a persistent DB, then attempts this exact
downgrade and asserts it raises `IntegrityError`). `mainai_recovery_events` is append-only at
the DATABASE level (migration 0033's `trg_mainai_recovery_events_deny_mutation` -- UPDATE is
unconditionally denied for every role, no GUC escape hatch the way `provider_spend_usage_events`
has for its own settle path; DELETE only through an authorized owner erasure). There is
therefore no way for this downgrade to rewrite or remove an existing `takeover_declined_governed`
row to make the narrower CHECK constraint pass again -- and it must never try to, since
silently bypassing that append-only guarantee just to make a downgrade succeed would be worse
than the downgrade failing loudly. `re-add the narrower CHECK constraint` failing with a real
`CheckViolation` in that situation is CORRECT, deliberate behavior, not a bug: it means real
governed-recovery history exists and this migration cannot be safely undone, exactly like
(undocumented until now) migration 0035's own `approval_granted` downgrade has always
implicitly depended on no `approval_granted` event ever having been recorded.

Revision ID: 0061
Revises: 0060
Create Date: 2026-08-26
"""

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

_OLD_EVENT_TYPES = (
    "'stalled_detected', 'dead_detected', 'recovery_started', 'recovery_inspected', "
    "'recovery_classified', 'salvage_started', 'salvage_completed', "
    "'takeover_started', 'takeover_completed', 'recovery_blocked', "
    "'manual_review_required', 'approval_granted'"
)
_NEW_EVENT_TYPES = _OLD_EVENT_TYPES + ", 'takeover_declined_governed'"


def upgrade() -> None:
    op.execute(f"""
        ALTER TABLE mainai_recovery_events DROP CONSTRAINT ck_mainai_recovery_events_event_type;
        ALTER TABLE mainai_recovery_events ADD CONSTRAINT ck_mainai_recovery_events_event_type CHECK (
            event_type IN ({_NEW_EVENT_TYPES})
        );
    """)


def downgrade() -> None:
    # Deliberately raises IntegrityError (CheckViolation) if any row already recorded
    # 'takeover_declined_governed' -- see this migration's own module docstring. Never add a
    # data-mutating workaround here; mainai_recovery_events' append-only guarantee (migration
    # 0033) must not be bypassed just to make a downgrade succeed.
    op.execute(f"""
        ALTER TABLE mainai_recovery_events DROP CONSTRAINT ck_mainai_recovery_events_event_type;
        ALTER TABLE mainai_recovery_events ADD CONSTRAINT ck_mainai_recovery_events_event_type CHECK (
            event_type IN ({_OLD_EVENT_TYPES})
        );
    """)
