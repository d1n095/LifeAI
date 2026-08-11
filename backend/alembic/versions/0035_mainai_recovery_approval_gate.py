"""MainAI V0.2 -- approval gate for recovery/takeover, mirroring V0.1's task-level approval
gate (app/mainai_execution/approval.py) but for a genuinely different decision: not "is this
task's CONTENT allowed to run" but "is it safe to let an autonomous recovery pass take over a
DEAD job's attempt without a founder looking at it first".

AUTO_SALVAGEABLE_CLASSIFICATIONS (app/models/mainai_recovery.py) already documents that these
seven classifications are meant to be auto-continued "without founder approval" -- that
remains true for five of them (NOTHING_DONE, CHECKPOINTED_WORK, LOCAL_UNCOMMITTED_WORK,
LOCAL_COMMITTED_NOT_PUSHED, VERIFIED_WORK): nothing has left this system's own local/durable
state, so a takeover here is purely internal bookkeeping. PUSHED_NO_PR and PR_EXISTS are
different in kind: the dead attempt's code is ALREADY visible on GitHub (a real, external,
shared-state artifact -- exactly the class of action the project's own standing git-safety
principles single out for confirmation before proceeding), so `app/mainai_execution/
recovery_approval.py`'s new `standard_recovery` policy requires an explicit
`approval_granted` MainAIRecoveryEvent before `execute_takeover()` continues past those two
classifications. CONFLICTED_STATE/UNSAFE_TO_AUTO_RECOVER never reach this gate at all --
`execute_takeover()`'s own classification check already refuses them first, and no approval
click is meant to bypass that (per the founder's explicit "ingen destructive recovery"; those
require a human to actually resolve the underlying git divergence out-of-band, not merely
click approve).

Adds exactly one new allowed `mainai_recovery_events.event_type` value -- no new table, no new
column -- matching how migration 0033 already models "is a task-level approval decision
durably recorded" (MainAITaskEventType.approval_granted, a plain append-only event, not a
dedicated column on the task).

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-11
"""

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

_OLD_EVENT_TYPES = (
    "'stalled_detected', 'dead_detected', 'recovery_started', 'recovery_inspected', "
    "'recovery_classified', 'salvage_started', 'salvage_completed', "
    "'takeover_started', 'takeover_completed', 'recovery_blocked', "
    "'manual_review_required'"
)
_NEW_EVENT_TYPES = _OLD_EVENT_TYPES + ", 'approval_granted'"


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
