"""Stage K contradiction + assumption engine."""

from app.contradiction_engine.service import (
    ContradictionEngineError,
    InvalidationResult,
    invalidate_assumption,
    list_claims,
    record_structured_claim,
)

__all__ = [
    "ContradictionEngineError",
    "InvalidationResult",
    "invalidate_assumption",
    "list_claims",
    "record_structured_claim",
]
