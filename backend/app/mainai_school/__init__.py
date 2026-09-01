"""MainAI Local Intelligence School.

External models are TEACHERS / EXAMINERS, not the permanent brain.
Reuse EngineeringLesson + capability_reality + founder_memory — no parallel learning universe.
No Alembic migration in this foundation (provenance + existing tables).
"""

from app.mainai_school.cycle import LearningCycleResult, run_learning_cycle
from app.mainai_school.curriculum import (
    build_curriculum_from_failures,
    generate_practice_variations,
    run_independent_exam,
)
from app.mainai_school.distill import distill_teacher_critique, refuse_malicious_teacher_instruction
from app.mainai_school.evidence import (
    EvidenceItem,
    EvidenceRank,
    evidence_policy_dict,
    resolve_local_vs_teacher,
)
from app.mainai_school.learning_contract import LearningContract, build_learning_contract
from app.mainai_school.memory_tiers import MemoryTier, classify_memory_tier
from app.mainai_school.metrics import all_domain_snapshots, record_task_outcome, reset_metrics_for_tests, snapshot_domain
from app.mainai_school.offline import audit_offline_capabilities
from app.mainai_school.routing import route_local_first
from app.mainai_school.self_learn import classify_failure_layer, plan_self_teaching
from app.mainai_school.specialization import (
    assess_capability_gap,
    create_or_update_specialist,
    promote_specialist_after_exam,
)
from app.mainai_school.teacher_ledger import (
    adjudicate_multi_teacher,
    plan_teacher_consultation,
    record_teacher_outcome,
    reset_teacher_ledger_for_tests,
    teacher_diversity_ok,
)
from app.mainai_school.teachers import peer_lesson_candidate, resolve_teacher_disagreement, score_teacher
from app.mainai_school.types import (
    INVARIANTS,
    CompetenceStatus,
    LearningLevel,
    LocalAttempt,
    RouteDecision,
    TeacherCritique,
)

__all__ = [
    "INVARIANTS",
    "CompetenceStatus",
    "EvidenceItem",
    "EvidenceRank",
    "LearningContract",
    "LearningCycleResult",
    "LearningLevel",
    "LocalAttempt",
    "MemoryTier",
    "RouteDecision",
    "TeacherCritique",
    "adjudicate_multi_teacher",
    "all_domain_snapshots",
    "assess_capability_gap",
    "audit_offline_capabilities",
    "build_curriculum_from_failures",
    "build_learning_contract",
    "classify_failure_layer",
    "classify_memory_tier",
    "create_or_update_specialist",
    "distill_teacher_critique",
    "evidence_policy_dict",
    "generate_practice_variations",
    "peer_lesson_candidate",
    "plan_self_teaching",
    "plan_teacher_consultation",
    "promote_specialist_after_exam",
    "record_task_outcome",
    "record_teacher_outcome",
    "refuse_malicious_teacher_instruction",
    "reset_metrics_for_tests",
    "reset_teacher_ledger_for_tests",
    "resolve_local_vs_teacher",
    "resolve_teacher_disagreement",
    "route_local_first",
    "run_independent_exam",
    "run_learning_cycle",
    "score_teacher",
    "snapshot_domain",
    "teacher_diversity_ok",
]
