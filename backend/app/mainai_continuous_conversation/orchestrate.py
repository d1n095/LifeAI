"""Turn the classified founder utterance into internal machine work.

Does not assign a second job to a busy agent. Does not ask the founder to relay
GitHub-discoverable facts. Does not grant security permissions.
"""

from __future__ import annotations

from app.mainai_continuous_conversation.classify import classify_inbound
from app.mainai_continuous_conversation.outbound import filter_outbound
from app.mainai_continuous_conversation.types import (
    ConversationTurnResult,
    InboundKind,
    InternalAction,
    InternalActionKind,
    RelayCategory,
    SoftwareTruth,
)


def handle_founder_message(
    text: str,
    *,
    busy_agents: tuple[str, ...] = (),
    idle_agents: tuple[str, ...] = (),
    draft_outbound: str | None = None,
    software_truth: SoftwareTruth | None = None,
) -> ConversationTurnResult:
    inbound = classify_inbound(text)
    actions: list[InternalAction] = []
    notes = [
        "continuous conversation state is advisory — it does not grant security permissions",
    ]

    if inbound.kind is InboundKind.RELAY_REQUEST:
        if RelayCategory.SHA in inbound.relay_categories or RelayCategory.CI_STATE in inbound.relay_categories:
            actions.append(
                InternalAction(
                    InternalActionKind.DISCOVER_FROM_GITHUB,
                    "exact SHA/branch/CI are read from GitHub, never asked of the founder",
                )
            )
        if RelayCategory.AGENT_MESSAGE in inbound.relay_categories or RelayCategory.NEXT_AGENT in inbound.relay_categories:
            actions.append(
                InternalAction(
                    InternalActionKind.COORDINATE_INTERNALLY,
                    "agent messages stay inside MainAI — founder is not a relay",
                )
            )
        notes.append(inbound.reason)

    if inbound.kind in {InboundKind.WORK_COMMAND, InboundKind.STATUS_QUESTION, InboundKind.RELAY_REQUEST}:
        for agent in busy_agents:
            actions.append(
                InternalAction(
                    InternalActionKind.HOLD_BUSY_AGENT,
                    f"{agent} is already running — do not duplicate work",
                    agent_key=agent,
                )
            )
        for agent in idle_agents:
            actions.append(
                InternalAction(
                    InternalActionKind.ASSIGN_IDLE_AGENT,
                    f"{agent} is idle — assign a safe independent lane, never the frozen candidate",
                    agent_key=agent,
                )
            )

    outbound = filter_outbound(draft_outbound, discovered=software_truth) if draft_outbound else None
    interrupt = inbound.interrupt_founder
    founder_message = None
    if interrupt:
        founder_message = inbound.reason
    elif software_truth is not None and software_truth.sha:
        notes.append(f"discovered {software_truth.branch}@{software_truth.sha} from {software_truth.source}")
    elif outbound is not None and outbound.asks_founder_to_relay:
        notes.append(outbound.reason)

    return ConversationTurnResult(
        inbound=inbound,
        outbound=outbound,
        internal_actions=tuple(actions),
        interrupt_founder=interrupt,
        founder_message=founder_message if interrupt else None,
        notes=tuple(notes),
        software_truth=software_truth,
    )


def founder_alpha_continuous_turn(text: str, *, draft_outbound: str | None = None) -> ConversationTurnResult:
    """The live occupancy pattern: Claude examining, Cursor and Codex idle, frozen SHA
    discoverable from GitHub. Founder must not be asked to relay that SHA or reassign Claude."""

    return handle_founder_message(
        text,
        busy_agents=("claude",),
        idle_agents=("cursor", "codex"),
        draft_outbound=draft_outbound,
    )
