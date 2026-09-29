"""MainAI orchestration truth ledger.

Canonical occupancy + GitHub-backed software truth for coordinating external coding
agents. Agent text is stored as claims, never as GitHub or certification authority.

LEDGER STATE != SECURITY PERMISSION.
"""

from app.mainai_orchestration_ledger.assignment import decide_assignment
from app.mainai_orchestration_ledger.claims import (
    apply_github_snapshot,
    bind_test_run,
    examiner_evidence_not_certification,
    ingest_agent_claim,
)
from app.mainai_orchestration_ledger.founder_interrupt import (
    FounderAttentionKind,
    classify_founder_attention,
    sha_relay_is_internal,
)
from app.mainai_orchestration_ledger.github_truth import (
    FakeSoftwareTruthSource,
    GitHubSoftwareTruthSource,
    answers_without_founder_relay,
)
from app.mainai_orchestration_ledger.orchestrate import founder_alpha_regression_world, plan_orchestration
from app.mainai_orchestration_ledger.types import (
    FOUNDER_ALPHA_FINAL_BRANCH,
    FOUNDER_ALPHA_FINAL_SHA,
    GITHUB_BACKED_FIELDS,
    AgentRole,
    AssignmentRefusal,
    OccupancyStatus,
    TaskStatus,
)

__all__ = [
    "FOUNDER_ALPHA_FINAL_BRANCH",
    "FOUNDER_ALPHA_FINAL_SHA",
    "GITHUB_BACKED_FIELDS",
    "AgentRole",
    "AssignmentRefusal",
    "FakeSoftwareTruthSource",
    "FounderAttentionKind",
    "GitHubSoftwareTruthSource",
    "OccupancyStatus",
    "TaskStatus",
    "answers_without_founder_relay",
    "apply_github_snapshot",
    "bind_test_run",
    "classify_founder_attention",
    "decide_assignment",
    "examiner_evidence_not_certification",
    "founder_alpha_regression_world",
    "ingest_agent_claim",
    "plan_orchestration",
    "sha_relay_is_internal",
]
