"""MainAI Dynamic Workforce Orchestration + Capability Learning / External-Independence. See
docs/mainai_v2/MAINAI_COVERAGE_WORKFORCE_CAPABILITY_RECONCILIATION.md for the architecture
decision."""

from __future__ import annotations

from app.mainai_workforce.capability_learning_loop import ReusableProcedure, apply_teacher_observation, extract_reusable_procedure
from app.mainai_workforce.continuation_policy import ContinuationResult, decide_continue_or_handoff
from app.mainai_workforce.demotion_policy import DemotionAssessment, assess_demotion_trigger
from app.mainai_workforce.external_dependence import DependenceRecommendation, recommend_dependence_reduction
from app.mainai_workforce.founder_output import (
    build_capability_stage_message,
    build_coverage_delta_message,
    build_workforce_wait_message,
    evaluate_message_necessity,
)
from app.mainai_workforce.mastery_ledger import (
    demote,
    get_mastery_record,
    get_or_create_mastery_record,
    list_mastery_events,
    promote,
    record_observation,
)
from app.mainai_workforce.promotion_policy import PromotionAssessment, assess_promotion_eligibility
from app.mainai_workforce.reservation import ReservationAssessment, assess_reservation
from app.mainai_workforce.situational_snapshot import agent_runtime_view_to_agent_state, assignment_to_work_item, real_agent_states_snapshot
from app.mainai_workforce.teacher_value import TeacherValueAssessment, assess_teacher_value
from app.mainai_workforce.types import AutonomyStage, ProviderDependenceRecommendation, TeacherObservation, WaitOrAssignDecision, WorkforceError
from app.mainai_workforce.wait_or_assign import WaitOrAssignResult, decide_wait_or_assign
from app.mainai_workforce.workforce_scheduler import WorkforceRecommendation, recommend_for_task

__all__ = [
    "AutonomyStage",
    "ContinuationResult",
    "DemotionAssessment",
    "DependenceRecommendation",
    "ProviderDependenceRecommendation",
    "PromotionAssessment",
    "ReservationAssessment",
    "ReusableProcedure",
    "TeacherObservation",
    "TeacherValueAssessment",
    "WaitOrAssignDecision",
    "WaitOrAssignResult",
    "WorkforceError",
    "WorkforceRecommendation",
    "agent_runtime_view_to_agent_state",
    "apply_teacher_observation",
    "assess_demotion_trigger",
    "assess_promotion_eligibility",
    "assess_reservation",
    "assess_teacher_value",
    "assignment_to_work_item",
    "build_capability_stage_message",
    "build_coverage_delta_message",
    "build_workforce_wait_message",
    "decide_continue_or_handoff",
    "decide_wait_or_assign",
    "demote",
    "evaluate_message_necessity",
    "extract_reusable_procedure",
    "get_mastery_record",
    "get_or_create_mastery_record",
    "list_mastery_events",
    "promote",
    "real_agent_states_snapshot",
    "recommend_dependence_reduction",
    "recommend_for_task",
    "record_observation",
]
