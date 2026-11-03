"""Reference business workflow: synthetic e-commerce opportunity assessment.

A thin entry point over the generic engine + the reference objective. Other businesses reuse
`run_business_workflow` with a different `BusinessObjectiveSpec`; nothing here is special-cased.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.mainai_business.engine import BusinessWorkflowReport, run_business_workflow
from app.mainai_business.objective import ECOMMERCE_OPPORTUNITY_ASSESSMENT


def run_ecommerce_opportunity_assessment(
    db: Session,
    *,
    owner_id: uuid.UUID,
    approve_publish: bool = False,
    founder_approval_ref: str | None = None,
    workflow_id: str | None = None,
) -> BusinessWorkflowReport:
    """Run the reference assessment. The consequential `publish_assessment` step runs only
    when `approve_publish` is True (fail-closed otherwise)."""
    approvals: dict[str, str] = {}
    if approve_publish:
        approvals["publish_assessment"] = founder_approval_ref or f"founder-approval:{uuid.uuid4()}"
    return run_business_workflow(
        db,
        owner_id=owner_id,
        spec=ECOMMERCE_OPPORTUNITY_ASSESSMENT,
        approvals=approvals,
        workflow_id=workflow_id,
    )
