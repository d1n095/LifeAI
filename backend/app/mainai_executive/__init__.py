"""Composed MainAI executive loop — glue across memory, context, workforce, continuity.

This package does NOT replace existing subsystems. It wires them.
Provider activation remains disabled. Claude-owned gates are never self-verified.
"""

from app.mainai_executive.attention import AttentionAction, decide_attention
from app.mainai_executive.bounds import ExecutiveScanBounds
from app.mainai_executive.completion import assess_completion
from app.mainai_executive.continuity import load_continuity_checkpoint, resume_summary
from app.mainai_executive.internal_start import (
    InternalBootReport,
    run_first_real_internal_boot,
    startup_status_surface,
)
from app.mainai_executive.loop import resume_executive_cycle, run_executive_cycle
from app.mainai_executive.missing_piece import detect_missing_pieces
from app.mainai_executive.multi_session import run_multi_session_program
from app.mainai_executive.observability import executive_status_snapshot
from app.mainai_executive.priority import PriorityFactors, score_priority
from app.mainai_executive.safe_composed_run import (
    ComposedSafeInternalReport,
    run_composed_safe_internal_mainai_run,
)
from app.mainai_executive.soak import run_executive_soak
from app.mainai_executive.types import ExecutiveCycleResult, ExecutivePhase, PlanningHorizon

__all__ = [
    "AttentionAction",
    "ComposedSafeInternalReport",
    "ExecutiveCycleResult",
    "ExecutivePhase",
    "ExecutiveScanBounds",
    "InternalBootReport",
    "PlanningHorizon",
    "PriorityFactors",
    "assess_completion",
    "decide_attention",
    "detect_missing_pieces",
    "executive_status_snapshot",
    "load_continuity_checkpoint",
    "resume_executive_cycle",
    "resume_summary",
    "run_composed_safe_internal_mainai_run",
    "run_executive_cycle",
    "run_executive_soak",
    "run_first_real_internal_boot",
    "run_multi_session_program",
    "score_priority",
    "startup_status_surface",
]
