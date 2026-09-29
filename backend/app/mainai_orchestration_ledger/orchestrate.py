"""High-level orchestration decisions over the truth ledger.

Pure: given occupancy + GitHub snapshot + tasks, emit the next safe actions.
Does not merge, deploy, activate Recall, or grant security permissions.
"""

from __future__ import annotations

from app.mainai_orchestration_ledger.assignment import decide_assignment
from app.mainai_orchestration_ledger.founder_interrupt import (
    FounderAttentionKind,
    classify_founder_attention,
)
from app.mainai_orchestration_ledger.types import (
    AgentRole,
    AssignmentDecision,
    NextActionKind,
    OccupancyStatus,
    OrchestrationPlan,
    OrchestrationWorld,
    PlannedAction,
    ProposedAssignment,
    TaskStatus,
)


def plan_orchestration(world: OrchestrationWorld) -> OrchestrationPlan:
    actions: list[PlannedAction] = []
    refused: list[AssignmentDecision] = []
    notes: list[str] = []

    notes.append("ledger state is advisory occupancy — it does not grant security permissions")
    if world.github is not None:
        actions.append(
            PlannedAction(
                NextActionKind.DISCOVER_FROM_GITHUB,
                None,
                (
                    f"GitHub branch {world.github.branch} exists={world.github.exists_remotely} "
                    f"sha={world.github.commit_sha} — do not ask the founder to relay this SHA"
                ),
            )
        )
        notes.append("software truth bound to GitHub snapshot, not conversation text")

    for task in world.tasks:
        if task.frozen or (task.protects_sha and task.protects_sha == world.frozen_sha):
            actions.append(
                PlannedAction(
                    NextActionKind.HOLD_FROZEN_CANDIDATE,
                    None,
                    f"frozen candidate {world.frozen_sha} on {world.frozen_branch} must not be modified",
                )
            )
            break

    for agent in world.agents:
        running_tasks = world.running_tasks_for(agent.agent_key)
        if agent.occupancy is OccupancyStatus.RUNNING or running_tasks:
            actions.append(
                PlannedAction(
                    NextActionKind.WAIT_FOR_RUNNING_AGENT,
                    agent.agent_key,
                    f"{agent.agent_key} is busy — do not duplicate work",
                )
            )
            for task in running_tasks:
                if task.role is AgentRole.EXAMINER:
                    notes.append(f"{agent.agent_key} is examining — do not reassign")

    for task in world.tasks:
        if task.role is AgentRole.BUILDER and task.status is TaskStatus.COMPLETED:
            if not _has_independent_exam(world, task):
                examiner = _idle_agent_for_role(world, AgentRole.EXAMINER, exclude={task.agent_key})
                actions.append(
                    PlannedAction(
                        NextActionKind.REQUEST_INDEPENDENT_EXAMINATION,
                        examiner,
                        f"builder {task.agent_key} finished {task.title!r} — request independent examination",
                    )
                )
        if task.role is AgentRole.EXAMINER and task.status is TaskStatus.FAILED:
            builder = _builder_for_examined_task(world, task)
            actions.append(
                PlannedAction(
                    NextActionKind.RETURN_DEFECT_TO_BUILDER,
                    builder,
                    f"examiner {task.agent_key} found a defect — return exact finding to builder {builder}",
                )
            )
        if task.status is TaskStatus.FAILED and not task.blockers:
            actions.append(
                PlannedAction(
                    NextActionKind.CONTINUE_OR_REASSIGN,
                    task.agent_key,
                    f"{task.agent_key} stopped without a real blocker — continue or reassign",
                )
            )

    for proposal in world.queued_assignments:
        decision = decide_assignment(world.agent(proposal.agent_key), proposal, world=world)
        if decision.allowed:
            actions.append(
                PlannedAction(
                    NextActionKind.ASSIGN_INDEPENDENT_LANE,
                    proposal.agent_key,
                    decision.reason,
                    assignment=proposal,
                )
            )
        else:
            refused.append(decision)

    founder_sha = classify_founder_attention(FounderAttentionKind.SHA_OR_BRANCH_RELAY)
    actions.append(
        PlannedAction(
            NextActionKind.NO_FOUNDER_RELAY,
            None,
            founder_sha.reason,
            interrupt_founder=founder_sha.interrupt,
        )
    )

    founder_messages = tuple(action.reason for action in actions if action.interrupt_founder)
    return OrchestrationPlan(
        actions=tuple(_dedupe_actions(actions)),
        refused=tuple(refused),
        founder_messages=founder_messages,
        notes=tuple(notes),
    )


def _has_independent_exam(world: OrchestrationWorld, builder_task) -> bool:
    return any(
        task.role is AgentRole.EXAMINER
        and task.status in {TaskStatus.ASSIGNED, TaskStatus.RUNNING, TaskStatus.COMPLETED, TaskStatus.AWAITING_EXTERNAL}
        and (
            builder_task.task_id in task.depends_on
            or task.exact_input_sha == (builder_task.output_sha or builder_task.exact_input_sha)
        )
        for task in world.tasks
    )


def _idle_agent_for_role(world: OrchestrationWorld, role: AgentRole, *, exclude: set[str]) -> str | None:
    for agent in world.agents:
        if agent.agent_key in exclude:
            continue
        if agent.occupancy is OccupancyStatus.IDLE and (agent.role is None or agent.role is role):
            return agent.agent_key
    return None


def _builder_for_examined_task(world: OrchestrationWorld, examiner_task) -> str | None:
    for task_id in examiner_task.depends_on:
        match = next((item for item in world.tasks if str(item.task_id) == str(task_id)), None)
        if match is not None:
            return match.agent_key
    for task in world.tasks:
        if task.role is AgentRole.BUILDER and task.output_sha and task.output_sha == examiner_task.exact_input_sha:
            return task.agent_key
    return None


def _dedupe_actions(actions: list[PlannedAction]) -> list[PlannedAction]:
    seen: set[tuple] = set()
    out: list[PlannedAction] = []
    for action in actions:
        key = (action.kind, action.agent_key, action.reason)
        if key in seen:
            continue
        seen.add(key)
        out.append(action)
    return out


def founder_alpha_regression_world(
    *,
    github,
    cursor_proposal: ProposedAssignment,
    codex_proposal: ProposedAssignment,
) -> OrchestrationWorld:
    """The real post-Founder-Alpha occupancy: Claude examining, Cursor/Codex idle,
    frozen SHA present, GitHub is source of truth, builder+validator done."""

    from app.mainai_orchestration_ledger.types import (
        FOUNDER_ALPHA_FINAL_BRANCH,
        FOUNDER_ALPHA_FINAL_SHA,
        AgentOccupancy,
        TaskRecord,
    )

    return OrchestrationWorld(
        agents=[
            AgentOccupancy("claude", OccupancyStatus.RUNNING, AgentRole.EXAMINER, current_task_id="examine-fa"),
            AgentOccupancy("cursor", OccupancyStatus.IDLE, AgentRole.VALIDATOR),
            AgentOccupancy("codex", OccupancyStatus.IDLE, AgentRole.BUILDER),
        ],
        tasks=[
            TaskRecord(
                task_id="build-fa",
                title="Founder Alpha composed candidate",
                owner_id="founder",
                role=AgentRole.BUILDER,
                status=TaskStatus.COMPLETED,
                agent_key="codex",
                exact_input_sha=FOUNDER_ALPHA_FINAL_SHA,
                working_branch=FOUNDER_ALPHA_FINAL_BRANCH,
                output_sha=FOUNDER_ALPHA_FINAL_SHA,
                remote_sha=FOUNDER_ALPHA_FINAL_SHA,
                remote_pushed=True,
                frozen=True,
                protects_sha=FOUNDER_ALPHA_FINAL_SHA,
                protects_branch=FOUNDER_ALPHA_FINAL_BRANCH,
                last_verified_source="github",
            ),
            TaskRecord(
                task_id="validate-fa",
                title="Founder Alpha independent validation",
                owner_id="founder",
                role=AgentRole.VALIDATOR,
                status=TaskStatus.COMPLETED,
                agent_key="cursor",
                exact_input_sha=FOUNDER_ALPHA_FINAL_SHA,
                working_branch=FOUNDER_ALPHA_FINAL_BRANCH,
                output_sha=FOUNDER_ALPHA_FINAL_SHA,
                remote_sha=FOUNDER_ALPHA_FINAL_SHA,
                remote_pushed=True,
                frozen=True,
                protects_sha=FOUNDER_ALPHA_FINAL_SHA,
                last_verified_source="github",
                depends_on=("build-fa",),
            ),
            TaskRecord(
                task_id="examine-fa",
                title="Founder Alpha independent examination",
                owner_id="founder",
                role=AgentRole.EXAMINER,
                status=TaskStatus.RUNNING,
                agent_key="claude",
                exact_input_sha=FOUNDER_ALPHA_FINAL_SHA,
                working_branch=FOUNDER_ALPHA_FINAL_BRANCH,
                frozen=True,
                protects_sha=FOUNDER_ALPHA_FINAL_SHA,
                depends_on=("build-fa",),
            ),
        ],
        github=github,
        queued_assignments=[cursor_proposal, codex_proposal],
    )
