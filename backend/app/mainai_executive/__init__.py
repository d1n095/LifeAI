"""Composed MainAI executive loop — glue across memory, context, workforce, continuity.

This package does NOT replace existing subsystems. It wires them.
Provider activation remains disabled. Claude-owned gates are never self-verified.
"""

from app.mainai_executive.bounds import ExecutiveScanBounds
from app.mainai_executive.completion import assess_completion
from app.mainai_executive.continuity import load_continuity_checkpoint, resume_summary
from app.mainai_executive.loop import resume_executive_cycle, run_executive_cycle
from app.mainai_executive.missing_piece import detect_missing_pieces
from app.mainai_executive.observability import executive_status_snapshot
from app.mainai_executive.types import ExecutiveCycleResult, ExecutivePhase, PlanningHorizon

__all__ = [
    "ExecutiveCycleResult",
    "ExecutivePhase",
    "ExecutiveScanBounds",
    "PlanningHorizon",
    "assess_completion",
    "detect_missing_pieces",
    "executive_status_snapshot",
    "load_continuity_checkpoint",
    "resume_executive_cycle",
    "resume_summary",
    "run_executive_cycle",
]
