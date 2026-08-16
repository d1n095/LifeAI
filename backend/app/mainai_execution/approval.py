"""The APPROVAL GATE — enforced as code, not prompt text (the founder's explicit V0.1
requirement). A task flagged `approval_required` cannot be dispatched (see
app/mainai_execution/executor.py's dispatch_ready_task()) until an explicit
`approval_granted` MainAITaskEvent exists for it — checked structurally, inside the same
function that would otherwise create the execution job, so there is no code path that skips
straight to execution on a system prompt's say-so. This is the actual stop, not a request that
an LLM merely be asked to respect.

APPROVAL_POLICIES is a NAMED registry (MainAIGoal.approval_policy points at one by name) —
never inline logic scattered across call sites. `standard_repo_work` (the only policy V0.1
ships) marks every task_type the planner can currently propose
(app/mainai_execution/planner.py's KNOWN_TASK_TYPES) as AUTO by default: read-only analysis,
local branch/edit/tests/commit, and PR creation never structurally require the founder's
explicit sign-off, matching the founder's own stated default. merge/deploy/production/
migration/backfill/destructive-data/security-boundary/irreversible-external actions are not
merely "require approval" here — they have NO task_type at all for the planner to propose in
the first place (see KNOWN_TASK_TYPES), so this policy's job for V0.1 is enforcing the
PER-TASK override: the planner (or a founder reviewing a plan) can still mark any individual
task `approval_required=True` regardless of its task_type's default, and that flag always
wins over the policy default -- see requires_approval()."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution import MainAITask, MainAITaskEvent, MainAITaskEventType


class ApprovalError(Exception):
    """Base for approval-gate failures."""


class ApprovalRequiredError(ApprovalError):
    """Raised by require_task_approval() when a task needs approval and none has been
    recorded. The caller (dispatch_ready_task()) must treat this as a hard stop — never a
    warning to log and proceed past."""

    def __init__(self, task_id: uuid.UUID):
        super().__init__(f"MainAITask {task_id} requires founder approval before it can be dispatched.")
        self.task_id = task_id


# task_type -> default approval requirement. A per-task `approval_required=True` set by the
# planner always overrides this default (see requires_approval()) -- this table only supplies
# what happens when the planner didn't set an explicit opinion.
#
# `standard_repo_work` is the founder-facing default: a human reviewed and created the goal
# (see app/routers/mainai_execution.py's create_goal(), behind require_founder), so every
# task_type the planner can currently propose is AUTO, matching the founder's own stated
# default for work they already looked at and asked for.
#
# `autonomous_development_work` is for goals a founder-authorized SCOPE (see
# app/development_supervisor/service.py's SupervisorScope) runs without a human reviewing each
# individual task -- the founder authorized the GOAL, not each write. Unlike standard_repo_work,
# repo_edit (which is also how a commit happens -- there is no separate commit task_type; see
# app/development_operator/service.py's commit_scoped_changes capability, dispatched from a
# repo_edit task) and open_pr default to requiring approval: an autonomous loop must not
# silently inherit the human-reviewed default just because it targets the same task_type
# vocabulary. read_only_audit and run_tests stay AUTO -- they are deterministic, reversible,
# and provider-independent, so gating them would only slow down harmless work without closing
# any real gap (the founder's own P1 review finding this policy fixes).
APPROVAL_POLICIES: dict[str, dict[str, bool]] = {
    "standard_repo_work": {
        "read_only_audit": False,
        "repo_edit": False,
        "run_tests": False,
        "open_pr": False,
    },
    "autonomous_development_work": {
        "read_only_audit": False,
        "repo_edit": True,
        "run_tests": False,
        "open_pr": True,
    },
}


def requires_approval(*, policy_name: str, task_type: str, task_approval_required: bool) -> bool:
    """The per-task flag always wins when True -- a task the planner (or a founder editing a
    plan) explicitly marked as needing approval is never silently downgraded by a permissive
    policy default. When the flag is False (the common case), the named policy's per-task_type
    default applies; an unknown policy name or task_type fails closed (requires approval)
    rather than guessing AUTO."""
    if task_approval_required:
        return True
    policy = APPROVAL_POLICIES.get(policy_name)
    if policy is None:
        return True
    return policy.get(task_type, True)


def require_task_approval(db: Session, *, task: MainAITask, goal_approval_policy: str) -> None:
    """THE enforcement point -- called at the top of dispatch_ready_task() (executor.py),
    before any mainai_jobs row is created. Raises ApprovalRequiredError, a real exception the
    caller cannot route around, if approval is required and no `approval_granted` event
    exists yet for this task. This is what "stopped by the system, not by convention" means in
    practice: an executor calling dispatch_ready_task() directly on an unapproved task gets an
    exception, never a job."""
    if not requires_approval(policy_name=goal_approval_policy, task_type=task.task_type, task_approval_required=task.approval_required):
        return
    granted = db.execute(
        select(MainAITaskEvent).where(MainAITaskEvent.task_id == task.id, MainAITaskEvent.event_type == MainAITaskEventType.approval_granted)
    ).first()
    if granted is None:
        raise ApprovalRequiredError(task.id)


def grant_task_approval(db: Session, *, task: MainAITask, approved_by: str) -> MainAITaskEvent:
    """Records the ONE event require_task_approval() looks for. This function has no other
    side effect (it does not dispatch anything itself) -- it only ever gets called from a
    genuine founder-driven action (a router, or a demo/test standing in for the founder's own
    click), never from inside the executor's own code path, which is exactly what keeps this a
    real gate rather than something the executor could grant to itself."""
    event = MainAITaskEvent(
        task_id=task.id, owner_id=task.owner_id, event_type=MainAITaskEventType.approval_granted, detail={"approved_by": approved_by}
    )
    db.add(event)
    db.flush()
    return event
