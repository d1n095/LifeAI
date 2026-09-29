"""Busy-agent protection and frozen-candidate assignment guards.

AGENT_RUNNING → NO_NEW_ASSIGNMENT unless the agent supports parallel workers AND a
separate execution slot is explicitly created. Occupancy is a fact on the ledger,
not something inferred from chat.

This module never grants security permissions. A successful assignment is a ledger
decision, not merge/deploy/Recall authority.
"""

from __future__ import annotations

from app.mainai_orchestration_ledger.types import (
    FOUNDER_ALPHA_FINAL_BRANCH,
    FOUNDER_ALPHA_FINAL_SHA,
    AgentOccupancy,
    AgentRole,
    AssignmentDecision,
    AssignmentRefusal,
    OccupancyStatus,
    OrchestrationWorld,
    ProposedAssignment,
    SlotStatus,
    TaskStatus,
)


def decide_assignment(
    agent: AgentOccupancy | None,
    proposal: ProposedAssignment,
    *,
    world: OrchestrationWorld | None = None,
    occupied_slot_keys: tuple[str, ...] = (),
) -> AssignmentDecision:
    if agent is None:
        return AssignmentDecision(
            False,
            AssignmentRefusal.MISSING_AGENT,
            f"no occupancy record for agent {proposal.agent_key}",
        )

    frozen_block = _frozen_candidate_block(proposal, world)
    if frozen_block is not None:
        return frozen_block

    if world is not None:
        duplicate = _duplicate_or_conflict(proposal, world)
        if duplicate is not None:
            return duplicate

    running = agent.occupancy is OccupancyStatus.RUNNING or agent.occupied_slots > 0
    if running and not agent.supports_parallel_workers:
        return AssignmentDecision(
            False,
            AssignmentRefusal.AGENT_RUNNING,
            f"{agent.agent_key} is RUNNING — NO_NEW_ASSIGNMENT without parallel workers",
        )

    if running and agent.supports_parallel_workers:
        if not proposal.create_new_slot and not proposal.slot_key:
            return AssignmentDecision(
                False,
                AssignmentRefusal.NO_FREE_SLOT,
                (
                    f"{agent.agent_key} is RUNNING and supports parallel workers, but no separate "
                    "execution slot/session was explicitly created"
                ),
            )
        if proposal.slot_key and proposal.slot_key in occupied_slot_keys:
            return AssignmentDecision(
                False,
                AssignmentRefusal.NO_FREE_SLOT,
                f"slot {proposal.slot_key} is already {SlotStatus.OCCUPIED.value}",
            )
        if not proposal.create_new_slot and agent.free_slots <= 0:
            return AssignmentDecision(
                False,
                AssignmentRefusal.NO_FREE_SLOT,
                f"{agent.agent_key} has no free slot (occupied={agent.occupied_slots}, max={agent.max_slots})",
            )
        slot_key = proposal.slot_key or f"slot-{agent.occupied_slots + 1}"
        return AssignmentDecision(True, None, "parallel slot explicitly created", slot_key=slot_key)

    return AssignmentDecision(
        True,
        None,
        f"{agent.agent_key} is idle — assignment allowed",
        slot_key=proposal.slot_key or "default",
    )


def _frozen_candidate_block(
    proposal: ProposedAssignment,
    world: OrchestrationWorld | None,
) -> AssignmentDecision | None:
    frozen_sha = world.frozen_sha if world is not None else FOUNDER_ALPHA_FINAL_SHA
    frozen_branch = world.frozen_branch if world is not None else FOUNDER_ALPHA_FINAL_BRANCH
    modifies = proposal.modifies_branch or (
        proposal.working_branch if proposal.role is AgentRole.BUILDER else None
    )
    if proposal.role is AgentRole.BUILDER and modifies == frozen_branch:
        return AssignmentDecision(
            False,
            AssignmentRefusal.FROZEN_CANDIDATE,
            f"builder work must not modify frozen branch {frozen_branch} at {frozen_sha}",
        )
    if proposal.working_branch == frozen_branch and proposal.role is AgentRole.BUILDER:
        return AssignmentDecision(
            False,
            AssignmentRefusal.FROZEN_CANDIDATE,
            f"builder working_branch cannot be the frozen final candidate {frozen_branch}",
        )
    return None


def _duplicate_or_conflict(
    proposal: ProposedAssignment,
    world: OrchestrationWorld,
) -> AssignmentDecision | None:
    active = [
        task
        for task in world.tasks
        if task.status in {TaskStatus.QUEUED, TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.AWAITING_EXTERNAL}
    ]
    for task in active:
        if task.title == proposal.title and task.agent_key:
            return AssignmentDecision(
                False,
                AssignmentRefusal.DUPLICATE_WORK,
                f"task {proposal.title!r} already {task.status.value} on {task.agent_key} — do not duplicate",
            )
        if (
            proposal.role is AgentRole.BUILDER
            and task.role is AgentRole.BUILDER
            and task.working_branch
            and task.working_branch == proposal.working_branch
        ):
            return AssignmentDecision(
                False,
                AssignmentRefusal.BRANCH_CONFLICT,
                f"builder already running on {proposal.working_branch}",
            )
    return None
