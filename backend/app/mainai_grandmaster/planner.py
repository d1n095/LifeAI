"""Deterministic multi-move and contingency planning over validated board state."""

from __future__ import annotations

from app.mainai_grandmaster.kernel import GrandmasterKernel
from app.mainai_grandmaster.types import Contingency, PlannedMove, ProposedMove


class GrandmasterPlanner:
    def __init__(self, kernel: GrandmasterKernel) -> None:
        self.kernel = kernel

    def plan(
        self, candidates: list[ProposedMove], *, limit: int | None = None
    ) -> tuple[PlannedMove, ...]:
        decisions = [
            self._rank(decision) for decision in self.kernel.validate_moves(candidates)
        ]
        legal = sorted(
            (item for item in decisions if item.legal),
            key=lambda item: item.rank or (),
            reverse=True,
        )
        blocked = [item for item in decisions if not item.legal]
        chosen: list[PlannedMove] = []
        occupied_agents: set[str] = set()
        occupied_workspaces: set[str] = set()
        for item in legal:
            if (
                item.move.agent_key in occupied_agents
                or item.move.workspace_key in occupied_workspaces
            ):
                blocked.append(
                    PlannedMove(
                        item.move,
                        False,
                        "BLOCKED",
                        ("plan_batch_collision",),
                        item.rank,
                    )
                )
                continue
            chosen.append(item)
            occupied_agents.add(item.move.agent_key)
            occupied_workspaces.add(item.move.workspace_key)
            if limit is not None and len(chosen) >= limit:
                break
        return tuple(chosen + blocked)

    def _rank(self, decision: PlannedMove) -> PlannedMove:
        if not decision.legal:
            return decision
        move = decision.move
        # Lexicographic priority follows policy order: safety and readiness are already
        # hard gates. Lower collision risk is represented by a validated unique workspace.
        rank = (
            1,  # safe
            1,  # dependencies ready
            1,  # no collision
            move.critical_path_impact,
            1,  # capacity available
            move.reversibility,
            move.expected_value,
            move.unblock_potential,
        )
        return PlannedMove(move, True, "ASSIGNABLE", (), rank)

    def resolve_contingency(self, contingency: Contingency, event: str) -> PlannedMove:
        mapping = {
            "PASS": contingency.on_pass,
            "RETURN_TO_BUILDER": contingency.on_return_to_builder,
            "BLOCK": contingency.on_block,
            "AGENT_IDLE": contingency.on_agent_idle,
        }
        move = mapping.get(event)
        if move is None:
            raise ValueError("no contingency configured for event")
        return self.kernel.validate_move(move)

    def next_builder_child(
        self, examination_id: str, move: ProposedMove
    ) -> PlannedMove:
        return self.kernel.validate_builder_child(examination_id, move)

    def can_use_idle_capacity(self, agent_key: str) -> bool:
        return self.kernel.agent_is_idle(agent_key)
