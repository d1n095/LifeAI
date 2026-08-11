"""V0.2 approval gate for recovery/takeover -- mirrors app/mainai_execution/approval.py's
"enforced as code, not prompt text" pattern exactly, but for a different decision: not "is
this task's CONTENT allowed to run" (that gate already applies unchanged -- dispatch_ready_task()
inside execute_takeover() still calls require_task_approval() for the task itself), but "is it
safe to let an autonomous recovery pass take over a DEAD job's attempt without a founder
looking at it first".

AUTO_SALVAGEABLE_CLASSIFICATIONS (app/models/mainai_recovery.py) documents that its seven
classifications are meant to be auto-continued "without founder approval" -- true for five of
them (NOTHING_DONE, CHECKPOINTED_WORK, LOCAL_UNCOMMITTED_WORK, LOCAL_COMMITTED_NOT_PUSHED,
VERIFIED_WORK): nothing has left this system's own local/durable state yet, so taking over is
purely internal bookkeeping. PUSHED_NO_PR and PR_EXISTS are different in kind -- the dead
attempt's code is ALREADY visible on GitHub, a real external, shared-state artifact -- exactly
the class of action the project's own standing git-safety principles single out for
confirmation before proceeding. `standard_recovery` (the only policy V0.2 ships, matching
approval.py's own "exactly one policy" precedent) requires an explicit `approval_granted`
MainAIRecoveryEvent before execute_takeover() continues past those two classifications.

CONFLICTED_STATE/UNSAFE_TO_AUTO_RECOVER never reach this gate at all -- execute_takeover()'s
own classification check refuses those first, and no approval click is meant to bypass that
(per the founder's explicit "ingen destructive recovery"; those require a human to actually
resolve the underlying git divergence out-of-band, then a fresh inspection pass, never an
"approve past it" shortcut)."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_recovery import MainAIRecoveryEvent, MainAIRecoveryEventType, MainAIRecoveryRecord, RecoveryClassification


class RecoveryApprovalError(Exception):
    """Base for recovery-approval-gate failures."""


class RecoveryApprovalRequiredError(RecoveryApprovalError):
    """Raised by require_recovery_approval() when a classification needs approval and none has
    been recorded. The caller (execute_takeover()) must treat this as a hard stop -- never a
    warning to log and proceed past."""

    def __init__(self, record_id: uuid.UUID, classification: RecoveryClassification):
        super().__init__(
            f"MainAIRecoveryRecord {record_id} (classification={classification.value}) requires founder "
            "approval before takeover can proceed -- the dead attempt's work is already visible on GitHub."
        )
        self.record_id = record_id
        self.classification = classification


# classification value -> default approval requirement, for the ONE policy V0.2 ships. Only
# AUTO_SALVAGEABLE_CLASSIFICATIONS ever reach this table in practice (execute_takeover()
# refuses anything else before this gate is even checked) -- CONFLICTED_STATE/
# UNSAFE_TO_AUTO_RECOVER are listed anyway for an explicit, fail-closed True rather than
# relying on the "unknown classification" fallback to carry that meaning implicitly.
RECOVERY_APPROVAL_POLICIES: dict[str, dict[str, bool]] = {
    "standard_recovery": {
        "NOTHING_DONE": False,
        "CHECKPOINTED_WORK": False,
        "LOCAL_UNCOMMITTED_WORK": False,
        "LOCAL_COMMITTED_NOT_PUSHED": False,
        "VERIFIED_WORK": False,
        "PUSHED_NO_PR": True,
        "PR_EXISTS": True,
        "CONFLICTED_STATE": True,
        "UNSAFE_TO_AUTO_RECOVER": True,
    }
}


def recovery_requires_approval(*, policy_name: str, classification: RecoveryClassification) -> bool:
    """An unknown policy name or classification fails closed (requires approval) rather than
    guessing AUTO -- same discipline app/mainai_execution/approval.py's requires_approval()
    already applies to task-level approval."""
    policy = RECOVERY_APPROVAL_POLICIES.get(policy_name)
    if policy is None:
        return True
    return policy.get(classification.value, True)


def require_recovery_approval(db: Session, *, record: MainAIRecoveryRecord, policy_name: str = "standard_recovery") -> None:
    """THE enforcement point -- called at the top of execute_takeover() (recovery_takeover.py),
    before any mutation. Raises RecoveryApprovalRequiredError, a real exception the caller
    cannot route around, if approval is required and no `approval_granted` event exists yet
    for this recovery record."""
    if record.classification is None:
        return  # not yet classified -- execute_takeover()'s own status check raises first
    if not recovery_requires_approval(policy_name=policy_name, classification=record.classification):
        return
    granted = db.execute(
        select(MainAIRecoveryEvent).where(
            MainAIRecoveryEvent.recovery_record_id == record.id,
            MainAIRecoveryEvent.event_type == MainAIRecoveryEventType.approval_granted,
        )
    ).first()
    if granted is None:
        raise RecoveryApprovalRequiredError(record.id, record.classification)


def grant_recovery_approval(db: Session, *, record: MainAIRecoveryRecord, approved_by: str) -> MainAIRecoveryEvent:
    """Records the ONE event require_recovery_approval() looks for. Has no other side effect
    (it does not take over anything itself) -- only ever called from a genuine founder-driven
    action (a router, or a test standing in for the founder's own click), never from inside
    execute_takeover()'s own code path, which is what keeps this a real gate rather than
    something the recovery pipeline could grant to itself."""
    event = MainAIRecoveryEvent(
        recovery_record_id=record.id, task_id=record.task_id, owner_id=record.owner_id,
        event_type=MainAIRecoveryEventType.approval_granted, detail={"approved_by": approved_by},
    )
    db.add(event)
    db.flush()
    return event
