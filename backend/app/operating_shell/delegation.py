"""Internal agent delegation boundary (MainAI V2, Stage V2-I6).

Operating shell talks only to MainAI externally -- the user should not need to manage
agents. This module models the boundary shape only (USER -> MainAI -> understanding ->
internal delegation -> MainAI aggregation -> workspace action); it does NOT implement real
agent orchestration, which already exists elsewhere in this codebase.

AGENT RESULT != USER-FACING TRUTH: aggregate_for_user() is the ONLY function in this
package that produces a UserFacingAnswer. Nothing else exposes an InternalDelegationResult
as if it were already user-facing.
"""

from __future__ import annotations

import uuid

from app.operating_shell.types import InternalDelegationResult, UserFacingAnswer


def record_delegation_result(
    *, specialist_key: str, finding_summary: str, evidence_refs: tuple[str, ...] = (), confidence: float | None = None
) -> InternalDelegationResult:
    """A specialist's own bounded finding. Deliberately NOT the same type as anything
    user-facing -- see module docstring."""
    return InternalDelegationResult(
        delegation_id=uuid.uuid4(), specialist_key=specialist_key, finding_summary=finding_summary,
        evidence_refs=evidence_refs, confidence=confidence,
    )


def aggregate_for_user(
    results: tuple[InternalDelegationResult, ...], *, owner_facing_text: str, intent_id: uuid.UUID | None = None
) -> UserFacingAnswer:
    """The ONLY path from InternalDelegationResult(s) to something user-facing.
    `owner_facing_text` must be supplied by the caller's own synthesis (this function does
    not attempt to auto-generate prose from raw findings) -- it exists to make the boundary
    explicit and auditable (source_delegation_ids), not to do the synthesis itself."""
    if not results:
        raise ValueError("aggregate_for_user() requires at least one delegation result to aggregate")
    return UserFacingAnswer(
        text=owner_facing_text, source_delegation_ids=tuple(r.delegation_id for r in results), intent_id=intent_id
    )
