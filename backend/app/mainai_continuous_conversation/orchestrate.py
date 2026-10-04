"""Turn the classified founder utterance into internal machine work.

Classification gates behavior. Founder-only authority is refused, not executed.
Caller-supplied busy/idle lists are not occupancy authority. Assignment execution
is not wired on this surface — occupancy observations stay observational.
"""

from __future__ import annotations

from app.mainai_continuous_conversation.capability import capability_disclaimer, unknown_lookup_reply
from app.mainai_continuous_conversation.classify import classify_inbound
from app.mainai_continuous_conversation.occupancy import caller_supplied_occupancy, founder_alpha_pattern_snapshot
from app.mainai_continuous_conversation.outbound import filter_outbound
from app.mainai_continuous_conversation.types import (
    ConversationTurnResult,
    InboundKind,
    InternalAction,
    InternalActionKind,
    OccupancySnapshot,
    OccupancyState,
    RelayCategory,
    SoftwareTruth,
)


def _occupancy_actions(occupancy: OccupancySnapshot | None) -> list[InternalAction]:
    if occupancy is None:
        return []
    actions: list[InternalAction] = []
    for item in occupancy.observations:
        actions.append(
            InternalAction(
                InternalActionKind.OBSERVE_OCCUPANCY,
                (
                    f"{item.agent_key} state={item.state.value} source={item.source} "
                    f"authoritative={item.authoritative} assignment={item.assignment_id} "
                    f"task={item.task_id} execution={item.execution_id}"
                ),
                agent_key=item.agent_key,
            )
        )
        pattern = occupancy.source == "founder_alpha_pattern"
        if not pattern and (not occupancy.authoritative or not item.authoritative):
            continue
        if item.state is OccupancyState.RUNNING:
            actions.append(
                InternalAction(
                    InternalActionKind.HOLD_BUSY_AGENT,
                    f"{item.agent_key} is RUNNING on assignment {item.assignment_id} — do not duplicate work",
                    agent_key=item.agent_key,
                )
            )
        elif item.state is OccupancyState.IDLE:
            actions.append(
                InternalAction(
                    InternalActionKind.ASSIGN_IDLE_AGENT,
                    (
                        f"{item.agent_key} is observed IDLE. Assignment execution is not wired "
                        "on this chat surface — this is not an assignment grant."
                    ),
                    agent_key=item.agent_key,
                )
            )
    return actions


def handle_founder_message(
    text: str,
    *,
    busy_agents: tuple[str, ...] = (),
    idle_agents: tuple[str, ...] = (),
    draft_outbound: str | None = None,
    software_truth: SoftwareTruth | None = None,
    occupancy: OccupancySnapshot | None = None,
    accept_pattern_occupancy: bool = False,
) -> ConversationTurnResult:
    inbound = classify_inbound(text)
    notes = [
        "continuous conversation state is advisory — it does not grant security permissions",
        capability_disclaimer(),
    ]

    if occupancy is None and accept_pattern_occupancy and (busy_agents or idle_agents):
        occupancy = founder_alpha_pattern_snapshot()
    elif occupancy is None and (busy_agents or idle_agents):
        occupancy = caller_supplied_occupancy(busy_agents=busy_agents, idle_agents=idle_agents)
        notes.append("caller-supplied busy/idle agents are not occupancy authority")

    if inbound.interrupt_founder:
        refuse = InternalAction(InternalActionKind.REFUSE_AUTHORITY, inbound.reason)
        outbound = filter_outbound(draft_outbound, discovered=software_truth) if draft_outbound else None
        return ConversationTurnResult(
            inbound=inbound,
            outbound=outbound,
            internal_actions=(refuse,),
            interrupt_founder=True,
            founder_message=inbound.reason,
            notes=tuple(notes + ["interrupt gated behavior — requested action was not executed"]),
            software_truth=software_truth,
            occupancy=occupancy,
            gated=True,
        )

    actions: list[InternalAction] = []
    if inbound.kind is InboundKind.RELAY_REQUEST:
        if software_truth is None or not software_truth.sha:
            actions.append(
                InternalAction(
                    InternalActionKind.LOOKUP_UNKNOWN,
                    unknown_lookup_reply(software_truth.entity_key if software_truth is not None else "requested fact"),
                )
            )
        if RelayCategory.SHA in inbound.relay_categories or RelayCategory.CI_STATE in inbound.relay_categories:
            actions.append(
                InternalAction(
                    InternalActionKind.DISCOVER_FROM_GITHUB,
                    "exact SHA/branch/CI are bound to the requested entity, then read from the authoritative source",
                )
            )
        if RelayCategory.AGENT_MESSAGE in inbound.relay_categories or RelayCategory.NEXT_AGENT in inbound.relay_categories:
            actions.append(
                InternalAction(
                    InternalActionKind.COORDINATE_INTERNALLY,
                    "agent reports are looked up internally when available; otherwise UNKNOWN. Founder is not a relay.",
                )
            )
        notes.append(inbound.reason)

    if inbound.kind in {InboundKind.WORK_COMMAND, InboundKind.STATUS_QUESTION, InboundKind.RELAY_REQUEST}:
        actions.extend(_occupancy_actions(occupancy))

    outbound = filter_outbound(draft_outbound, discovered=software_truth) if draft_outbound else None
    return ConversationTurnResult(
        inbound=inbound,
        outbound=outbound,
        internal_actions=tuple(actions),
        interrupt_founder=False,
        founder_message=None,
        notes=tuple(notes),
        software_truth=software_truth,
        occupancy=occupancy,
        gated=False,
    )


def founder_alpha_continuous_turn(text: str, *, draft_outbound: str | None = None) -> ConversationTurnResult:
    """Documented Founder Alpha occupancy *pattern* helper. Not live occupancy authority."""

    return handle_founder_message(
        text,
        occupancy=founder_alpha_pattern_snapshot(),
        draft_outbound=draft_outbound,
        accept_pattern_occupancy=True,
    )
