"""Truthful capability surface for continuous conversation.

Do not claim unimplemented machine-coordination capability. Occupancy is observed.
Assignment execution, merge, deploy, and Recall are not wired on this surface.
"""

from __future__ import annotations

IMPLEMENTED: dict[str, bool] = {
    "canonical_thread": True,
    "subject_binding": True,
    "github_or_registry_lookup": True,
    "occupancy_observe": True,
    "context_compaction": True,
    "decision_supersession": True,
    "workspace_ownership": True,
    "founder_interrupt_gating": True,
    "machine_assignment_execution": False,
    "merge": False,
    "deploy": False,
    "recall_activation": False,
}


def capability_disclaimer() -> str:
    return (
        "I look up machine-discoverable facts myself when an authoritative source exists. "
        "I will not ask you to relay SHAs, branches, CI, or agent reports. "
        "Occupancy is observed from task/session state. "
        "Agent assignment execution is not wired on this chat surface. "
        "Conversation state does not grant merge, deploy, or Recall authority."
    )


def unknown_lookup_reply(entity_key: str = "requested fact") -> str:
    return (
        f"UNKNOWN: {entity_key} is not available from an authoritative internal source. "
        "I will not ask you to relay it and I will not fabricate it."
    )


def assert_not_claiming_unimplemented(text: str) -> None:
    lowered = text.lower()
    if IMPLEMENTED["machine_assignment_execution"]:
        return
    if "mainai handles machine coordination internally" in lowered:
        raise AssertionError("must not claim unimplemented machine-coordination capability")
