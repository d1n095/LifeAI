"""Fail-closed strategic orchestration kernel for MainAI.

The package plans and validates work; it never dispatches, merges, deploys, activates,
or turns model output into authority.
"""

from app.mainai_grandmaster.kernel import GrandmasterKernel
from app.mainai_grandmaster.planner import GrandmasterPlanner

__all__ = ["GrandmasterKernel", "GrandmasterPlanner"]
