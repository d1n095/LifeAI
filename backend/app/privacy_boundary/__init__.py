"""MainAI V2 -- Privacy Boundary Engine (Stage V2-C) foundation.

Standalone, isolated, NOT imported by any production runtime path. See
docs/mainai_v2/MAINAI_V2_PRIVACY_BOUNDARY_ENGINE.md for the design.
"""

from app.privacy_boundary.advocate import (
    AggregateResult,
    CategoryAggregateRequest,
    DisclosureRequestRejected,
    evaluate_advocate_request,
)
from app.privacy_boundary.pipeline import (
    PrivacyBoundaryError,
    classify,
    evaluate_policy,
    generalize,
    minimize,
    run_privacy_pipeline,
)
from app.privacy_boundary.receipts import ReceiptLog
from app.privacy_boundary.sanitize import sanitize_text
from app.privacy_boundary.types import (
    ClassifiedSignal,
    DataClassification,
    DataFlag,
    EgressableSignal,
    GeneralizedSignal,
    MinimizedSignal,
    OutboundPurpose,
    PrivacyPolicyDecision,
    PrivacyReceipt,
    RawLocalSignal,
    TelemetryMode,
)

__all__ = [
    "AggregateResult",
    "CategoryAggregateRequest",
    "ClassifiedSignal",
    "DataClassification",
    "DataFlag",
    "DisclosureRequestRejected",
    "EgressableSignal",
    "GeneralizedSignal",
    "MinimizedSignal",
    "OutboundPurpose",
    "PrivacyBoundaryError",
    "PrivacyPolicyDecision",
    "PrivacyReceipt",
    "RawLocalSignal",
    "ReceiptLog",
    "TelemetryMode",
    "classify",
    "evaluate_advocate_request",
    "evaluate_policy",
    "generalize",
    "minimize",
    "run_privacy_pipeline",
    "sanitize_text",
]
