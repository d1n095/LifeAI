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

Revision ID: 0060
Revises: 0059
Create Date: 2026-08-26
"""

from alembic import op

revision = "0060"
down_revision = "0059"
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
    op.execute(f"""
        ALTER TABLE mainai_recovery_events DROP CONSTRAINT ck_mainai_recovery_events_event_type;
        ALTER TABLE mainai_recovery_events ADD CONSTRAINT ck_mainai_recovery_events_event_type CHECK (
            event_type IN ({_OLD_EVENT_TYPES})
        );
    """)
