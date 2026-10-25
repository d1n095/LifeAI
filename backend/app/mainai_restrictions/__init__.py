"""Provider-neutral limitation transparency.

MainAI must not silently circumvent restrictions or automatically exploit
workarounds. External providers cannot suppress these records or mint
Founder authorization.
"""

from app.mainai_restrictions.service import (
    CLASSIFICATIONS,
    WORKAROUND_CLASSES,
    encounter_restriction,
    lookup_restriction_lessons,
    redact_secrets,
    restriction_public_view,
)

__all__ = [
    "CLASSIFICATIONS",
    "WORKAROUND_CLASSES",
    "encounter_restriction",
    "lookup_restriction_lessons",
    "redact_secrets",
    "restriction_public_view",
]
