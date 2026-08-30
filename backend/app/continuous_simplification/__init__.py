"""Stage F continuous simplification."""

from app.continuous_simplification.service import (
    PROTECTED_DOMAINS,
    SimplificationKind,
    SimplificationProposal,
    SimplificationReport,
    propose_simplifications,
)

__all__ = [
    "PROTECTED_DOMAINS",
    "SimplificationKind",
    "SimplificationProposal",
    "SimplificationReport",
    "propose_simplifications",
]
