"""Action risk model + preview contract + authority policy seam (MainAI V2, Stage V2-I1/I5).

ACTION REQUEST != AUTHORITY: evaluate_action_authority() never returns a default-allow
when no real policy is supplied -- it raises PolicyNotWiredError. This is the seam a future
Guardian integration plugs into (this module does NOT import app.guardian).
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Protocol

from app.operating_shell.types import (
    CONSEQUENTIAL_AND_ABOVE,
    DEFAULT_ACTION_RISK,
    ActionPreview,
    ActionReceipt,
    ActionRiskLevel,
    ConsequentialActionRequiresPreviewError,
    PolicyNotWiredError,
    WorkspaceAction,
    WorkspaceActionResult,
)


def classify_action_risk(action: WorkspaceAction) -> ActionRiskLevel:
    """The action's own `.risk` field is authoritative if set; otherwise falls back to the
    DEFAULT_ACTION_RISK table for its action_type. A caller cannot understate risk merely by
    omitting `.risk` -- the fallback is the SAME table used to construct new actions
    elsewhere in this package, never a separately-drifting copy."""
    return action.risk or DEFAULT_ACTION_RISK[action.command.action_type]


class PolicyDecision(str):
    """String subclass so callers can compare/log it directly; not an Enum because this
    seam intentionally makes no closed claim about what a real future policy might return."""


ALLOWED = PolicyDecision("allowed")
DENIED = PolicyDecision("denied")


class ActionAuthorityPolicy(Protocol):
    """The seam a future Guardian integration implements. This package supplies NO real
    implementation -- see evaluate_action_authority()."""

    def evaluate(self, action: WorkspaceAction, risk: ActionRiskLevel) -> PolicyDecision: ...


def evaluate_action_authority(
    action: WorkspaceAction, *, policy: ActionAuthorityPolicy | None
) -> PolicyDecision:
    """ACTION REQUEST != AUTHORITY. Raises PolicyNotWiredError if `policy` is None --
    NEVER a silent default-allow. A real policy (e.g. a future Guardian-backed one) must be
    supplied explicitly by the caller."""
    if policy is None:
        raise PolicyNotWiredError(
            f"no ActionAuthorityPolicy supplied for action {action.action_id} "
            f"(risk={classify_action_risk(action).value}) -- ACTION REQUEST != AUTHORITY, "
            "this seam refuses to default-allow"
        )
    return policy.evaluate(action, classify_action_risk(action))


def require_root_sensitive_policy(action: WorkspaceAction, *, policy: ActionAuthorityPolicy | None) -> PolicyDecision:
    """ROOT_SECURITY_SENSITIVE actions must ALWAYS route through evaluate_action_authority()
    -- this wrapper exists so a caller cannot special-case "just this once" past the seam.
    There is no code path in this function that returns ALLOWED without a real policy call."""
    risk = classify_action_risk(action)
    if risk != ActionRiskLevel.ROOT_SECURITY_SENSITIVE:
        raise ValueError(f"require_root_sensitive_policy() called on a non-root-sensitive action (risk={risk.value})")
    return evaluate_action_authority(action, policy=policy)


def build_action_preview(
    action: WorkspaceAction,
    *,
    description: str,
    target_ref: uuid.UUID | None,
    data_affected: tuple[str, ...] = (),
    external_effect: bool = False,
    external_effect_description: str = "",
    reversible: bool = True,
    reversal_description: str = "",
    confirmation_required: bool = True,
    rollback_possible: bool = True,
    rollback_description: str = "",
) -> ActionPreview:
    return ActionPreview(
        preview_id=uuid.uuid4(),
        action_id=action.action_id,
        description=description,
        target_ref=target_ref,
        data_affected=data_affected,
        external_effect=external_effect,
        external_effect_description=external_effect_description,
        reversible=reversible,
        reversal_description=reversal_description,
        authority_required=classify_action_risk(action),
        confirmation_required=confirmation_required,
        rollback_possible=rollback_possible,
        rollback_description=rollback_description,
    )


def require_preview_for_consequential_action(
    action: WorkspaceAction, *, preview: ActionPreview | None
) -> None:
    """Real, tested gate: REVERSIBLE_CONSEQUENTIAL-or-above actions may not be marked
    executed without an attached ActionPreview generated for THIS action_id specifically
    (not merely any preview) -- raises ConsequentialActionRequiresPreviewError otherwise."""
    risk = classify_action_risk(action)
    if risk not in CONSEQUENTIAL_AND_ABOVE:
        return
    if preview is None or preview.action_id != action.action_id:
        raise ConsequentialActionRequiresPreviewError(
            f"action {action.action_id} has risk={risk.value} (consequential or above) and "
            "requires a matching ActionPreview before it may be marked executed"
        )


def build_action_result(
    action: WorkspaceAction,
    *,
    policy: ActionAuthorityPolicy | None,
    preview: ActionPreview | None,
    outcome_summary: str,
) -> WorkspaceActionResult:
    """The only path in this package that produces an `executed=True` WorkspaceActionResult.
    Always calls evaluate_action_authority() (ACTION REQUEST != AUTHORITY) and, for
    consequential-or-above risk, always calls require_preview_for_consequential_action()
    first. A DENIED policy decision produces executed=False, never an exception swallowed
    into a false "success" result."""
    decision = evaluate_action_authority(action, policy=policy)
    if decision != ALLOWED:
        return WorkspaceActionResult(
            result_id=uuid.uuid4(),
            action_id=action.action_id,
            executed=False,
            preview_ref=preview.preview_id if preview else None,
            outcome_summary=f"not executed: policy decision was {decision!r}",
        )
    require_preview_for_consequential_action(action, preview=preview)
    return WorkspaceActionResult(
        result_id=uuid.uuid4(),
        action_id=action.action_id,
        executed=True,
        preview_ref=preview.preview_id if preview else None,
        outcome_summary=outcome_summary,
    )


# --- Action receipts (hash-chained, append-only). -----------------------------------------


def _receipt_hash(receipt: ActionReceipt) -> str:
    payload = f"{receipt.receipt_id}:{receipt.action_id}:{receipt.risk.value}:{receipt.decision}:{receipt.prev_hash}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_action_receipt(
    receipts: list[ActionReceipt], *, action: WorkspaceAction, decision: PolicyDecision | str
) -> ActionReceipt:
    prev_hash = receipts[-1].this_hash if receipts else "0" * 64
    receipt = ActionReceipt(
        receipt_id=uuid.uuid4(), action_id=action.action_id, risk=classify_action_risk(action), decision=str(decision), prev_hash=prev_hash
    )
    receipt.this_hash = _receipt_hash(receipt)
    receipts.append(receipt)
    return receipt


def verify_receipt_chain_intact(receipts: list[ActionReceipt]) -> bool:
    prev_hash = "0" * 64
    for receipt in receipts:
        expected = _receipt_hash(ActionReceipt(
            receipt_id=receipt.receipt_id, action_id=receipt.action_id, risk=receipt.risk, decision=receipt.decision,
            prev_hash=prev_hash, created_at=receipt.created_at,
        ))
        if receipt.this_hash != expected or receipt.prev_hash != prev_hash:
            return False
        prev_hash = receipt.this_hash
    return True
