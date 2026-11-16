"""Founder Anti-Repetition / Relevance Filter. See
docs/mainai_v2/MAINAI_COGNITIVE_OPS_RECONCILIATION.md.

ALREADY REPORTED != REPORT AGAIN. NO MATERIAL STATUS CHANGE != FOUNDER NOTIFICATION.
SAME RECOMMENDATION + NO NEW EVIDENCE != NEW MESSAGE. KNOWN BACKGROUND != REPEAT BACKGROUND.

Pure: takes the latest ledger row (as already read via
`founder_communication_ledger.latest_communication_for_topic()`) and a candidate new
communication, and decides whether founder output is warranted. Never writes anything itself."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.mainai_cognitive_ops.types import CommunicationDeltaVerdict


@dataclass(frozen=True)
class CommunicationNecessity:
    verdict: CommunicationDeltaVerdict
    should_notify: bool
    reason: str


def assess_communication_necessity(
    *,
    previous: dict[str, Any] | None,
    candidate_status: str,
    candidate_material_facts: dict[str, Any] | None = None,
    decision_now_required: bool = False,
    risk_changed: bool = False,
    cost_changed: bool = False,
    blocker_appeared: bool = False,
) -> CommunicationNecessity:
    """First match wins:

    1. A decision is now required that was not previously requested/received -> REPORT
       (founder input is, by construction, always material).
    2. Risk changed, cost changed, or a blocker appeared -> REPORT (each is material by
       definition even if the headline status string is unchanged).
    3. No previous communication exists for this topic -> REPORT (first report is never a
       repeat).
    4. `candidate_status`/`candidate_material_facts` are identical to the previous
       communication's own fields -> SUPPRESS (ALREADY REPORTED != REPORT AGAIN).
    5. Otherwise -> REPORT (status text or facts changed in some other way).
    """

    if decision_now_required:
        return CommunicationNecessity(CommunicationDeltaVerdict.REPORT_DECISION_REQUIRED, True, "a founder decision is now required")

    if risk_changed or cost_changed or blocker_appeared:
        changed = [name for name, flag in (("risk", risk_changed), ("cost", cost_changed), ("blocker", blocker_appeared)) if flag]
        return CommunicationNecessity(CommunicationDeltaVerdict.REPORT_MATERIAL_CHANGE, True, f"material change: {', '.join(changed)}")

    if previous is None:
        return CommunicationNecessity(CommunicationDeltaVerdict.REPORT_MATERIAL_CHANGE, True, "no previous communication exists for this topic -- first report is never a repeat")

    prev_status = previous.get("status_communicated")
    prev_facts = previous.get("material_facts") or {}
    if prev_status == candidate_status and prev_facts == (candidate_material_facts or {}):
        return CommunicationNecessity(
            CommunicationDeltaVerdict.SUPPRESS_ALREADY_REPORTED, False,
            "identical status and material facts already communicated for this topic -- ALREADY REPORTED != REPORT AGAIN",
        )

    return CommunicationNecessity(CommunicationDeltaVerdict.REPORT_MATERIAL_CHANGE, True, "status or material facts differ from the last communication on this topic")
