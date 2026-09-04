"""Local-first, source-agnostic personal recall foundation.

This package is intentionally not wired into chat or HTTP routes.  It defines the common
language adapters can use without becoming a second canonical memory store.
"""

from app.personal_recall.query import understand_query
from app.personal_recall.retrieval import PersonalRecallEngine
from app.personal_recall.locator import validate_open_locator
from app.personal_recall.types import *  # noqa: F401,F403

__all__ = ["PersonalRecallEngine", "understand_query", "validate_open_locator"]
