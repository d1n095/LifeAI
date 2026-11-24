"""Composed MainAI executive loop — glue across memory, context, workforce, continuity.

This package does NOT replace existing subsystems. It wires them.
Provider activation remains disabled. Claude-owned gates are never self-verified.
"""

from app.mainai_executive.assumption_review import ReviewEvidence, review_objection
from app.mainai_executive.attention import AttentionAction, decide_attention
from app.mainai_executive.bottleneck import ResourceSample, assess_bottleneck, sample_host_resources
from app.mainai_executive.bounds import ExecutiveScanBounds
from app.mainai_executive.engineering_decision import (
    CRITERIA,
    OPTION_KINDS,
    SolutionOption,
    compare_solutions,
)
from app.mainai_executive.evidence_state import (
    EVIDENCE_STATES,
    ModuleEvidence,
    assess_module,
    assess_modules,
)
from app.mainai_executive.investigation import investigate_engineering_request
from app.mainai_executive.completion import assess_completion
from app.mainai_executive.continuity import load_continuity_checkpoint, resume_summary
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
    "CRITERIA",
    "ComposedSafeInternalReport",
    "EVIDENCE_STATES",
    "ModuleEvidence",
    "OPTION_KINDS",
    "ResourceSample",
    "ReviewEvidence",
    "SolutionOption",
    "ExecutiveCycleResult",
    "ExecutivePhase",
    "ExecutiveScanBounds",
    "PlanningHorizon",
    "PriorityFactors",
    "assess_bottleneck",
    "assess_completion",
    "assess_module",
    "assess_modules",
    "compare_solutions",
    "decide_attention",
    "detect_missing_pieces",
    "investigate_engineering_request",
    "review_objection",
    "sample_host_resources",
    "executive_status_snapshot",
    "load_continuity_checkpoint",
    "resume_executive_cycle",
    "resume_summary",
    "run_composed_safe_internal_mainai_run",
    "run_executive_cycle",
    "run_executive_soak",
    "run_multi_session_program",
    "score_priority",
]
