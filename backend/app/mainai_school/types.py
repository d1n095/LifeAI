"""MainAI Local Intelligence School — types and absolute invariants.

EXTERNAL MODEL != MAINAI.
TEACHER OUTPUT != VERIFIED TRUTH.
TRAINING DATA CREATED != MODEL TRAINED.
ONE EXAM PASS != PERMANENT COMPETENCE.
LEARNING != AUTHORITY WIDENING.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CompetenceStatus(str, Enum):
    UNTRAINED = "UNTRAINED"
    LEARNING = "LEARNING"
    SUPERVISED = "SUPERVISED"
    PROBATION = "PROBATION"
    LOCALLY_COMPETENT = "LOCALLY_COMPETENT"
    LOCALLY_VERIFIED = "LOCALLY_VERIFIED"
    DEGRADED = "DEGRADED"
    RETRAINING = "RETRAINING"


class RouteDecision(str, Enum):
    LOCAL = "LOCAL"
    LOCAL_THEN_TEACHER_REVIEW = "LOCAL_THEN_TEACHER_REVIEW"
    TEACHER_GUIDED = "TEACHER_GUIDED"
    LOCAL_FIRST_PROVEN = "LOCAL_FIRST_PROVEN"
    SUPERVISED_LEARNING = "SUPERVISED_LEARNING"


class LearningLevel(str, Enum):
    """Honest layers — do not claim LEVEL_5 unless weights actually trained."""

    MEMORY = "LEVEL_1_MEMORY"
    PLAYBOOK = "LEVEL_2_PLAYBOOK"
    RETRIEVAL = "LEVEL_3_RETRIEVAL"
    SPECIALIZATION = "LEVEL_4_SPECIALIZATION"
    WEIGHT_TRAINING = "LEVEL_5_WEIGHT_TRAINING"  # only when real training path exists


SCHOOL_MARKER = "mainai_school_v1"

INVARIANTS = (
    "EXTERNAL_MODEL_IS_NOT_MAINAI",
    "EXTERNAL_MODEL_IS_NOT_PERMANENT_MEMORY",
    "EXTERNAL_MODEL_IS_NOT_AUTHORITY",
    "EXTERNAL_MODEL_IS_NOT_REQUIRED_EXECUTION_PATH",
    "TEACHER_RESPONSE_IS_NOT_VERIFIED_TRUTH",
    "TEACHER_IS_NOT_TRUTH",
    "MODEL_CONSENSUS_IS_NOT_TRUTH",
    "NO_SINGLE_API_ANSWER_KEY",
    "EVIDENCE_OUTRANKS_OPINION",
    "PRIMARY_PROOF_OUTRANKS_MODEL_AGREEMENT",
    "PRICE_IS_NOT_QUALITY",
    "FREE_IS_NOT_TRUSTWORTHY_BY_DEFINITION",
    "API_AVAILABLE_IS_NOT_CAPABILITY",
    "API_UNAVAILABLE_DOES_NOT_STOP_MAINAI",
    "API_FAILURE_IS_NOT_MAINAI_FAILURE",
    "LOCAL_SUCCESS_IS_NOT_LOCAL_COMPETENCE",
    "LOCAL_IS_NOT_CORRECT_BY_DEFINITION",
    "ONE_EXAM_PASS_IS_NOT_PERMANENT_COMPETENCE",
    "PAST_COMPETENCE_IS_NOT_CURRENT_COMPETENCE",
    "LEARNING_IS_NOT_AUTHORITY_WIDENING",
    "MODEL_TRAINING_IS_NOT_ACCESS_WIDENING",
    "TRAINING_DATA_CREATED_IS_NOT_MODEL_TRAINED",
    "MODEL_TRAINED_IS_NOT_MODEL_IMPROVED",
    "MODEL_IMPROVED_IS_NOT_SAFE_FOR_PRODUCTION",
)


@dataclass(frozen=True)
class LocalAttempt:
    domain: str
    task_class: str
    attempt_summary: str
    success: bool | None
    confidence: float
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TeacherCritique:
    teacher_id: str
    domain: str
    critique_summary: str
    claimed_correct: bool | None
    raw_excerpt: str | None = None  # optional; prefer distilled fields
    # Teacher output is untrusted until checked.
    trusted: bool = False


@dataclass
class DistilledLesson:
    problem_type: str
    local_mistake: str
    root_cause: str
    general_rule: str
    when_applies: str
    when_not: str
    counterexample: str | None
    test_case: str | None
    source: str
    confidence: float
    learning_level: LearningLevel = LearningLevel.MEMORY
    weight_training_ran: bool = False


@dataclass
class IndependenceSnapshot:
    domain: str
    local_attempt_rate: float
    local_success_rate: float
    teacher_help_rate: float
    teacher_correction_rate: float
    exam_success_rate: float
    external_dependency_ratio: float
    evidence: dict[str, Any] = field(default_factory=dict)
