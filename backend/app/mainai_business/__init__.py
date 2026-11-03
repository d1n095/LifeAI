"""MainAI executive business-workflow layer.

Composes the existing workforce substrate (registry, selector, broker, task-scoped
authority, independent verification, performance ledger, failure/recovery) into one
end-to-end, Founder-facing path that turns a business objective into a verified, persisted
assessment produced by a team of specialist agents. No new tables; no provider invocation;
consequential actions are Founder-gated and fail-closed.
"""

from app.mainai_business.engine import (
    BusinessWorkflowError,
    BusinessWorkflowReport,
    provision_business_team,
    run_business_workflow,
)
from app.mainai_business.execution import (
    ExecutionResult,
    InProcessRunner,
    ModelBackedExecutionError,
    ModelBackedRunner,
)
from app.mainai_business.objective import (
    ECOMMERCE_OPPORTUNITY_ASSESSMENT,
    BusinessObjectiveSpec,
    RoleSpec,
    TaskSpec,
    topological_order,
)
from app.mainai_business.reference import run_ecommerce_opportunity_assessment
from app.mainai_business.view import assemble_business_project_view

__all__ = [
    "BusinessWorkflowError",
    "BusinessWorkflowReport",
    "provision_business_team",
    "run_business_workflow",
    "ExecutionResult",
    "InProcessRunner",
    "ModelBackedRunner",
    "ModelBackedExecutionError",
    "ECOMMERCE_OPPORTUNITY_ASSESSMENT",
    "BusinessObjectiveSpec",
    "RoleSpec",
    "TaskSpec",
    "topological_order",
    "run_ecommerce_opportunity_assessment",
    "assemble_business_project_view",
]
