"""Local-first, source-agnostic personal recall foundation.

This package is intentionally not wired into chat or HTTP routes.  It defines the common
language adapters can use without becoming a second canonical memory store.
"""

from app.personal_recall.query import understand_query
from app.personal_recall.retrieval import PersonalRecallEngine
from app.personal_recall.service import PersonalRecallService
from app.personal_recall.locator import validate_open_locator
from app.personal_recall.serialization import serialize_for_local_client
from app.personal_recall.snapshot_sync import synchronize_authoritative_sources
from app.personal_recall.outbox import consume_outbox, outbox_status, process_outbox_batch, retry_status
from app.personal_recall.reconciliation import reconcile_sources
from app.personal_recall.types import *  # noqa: F401,F403

__all__ = ["PersonalRecallEngine", "PersonalRecallService", "serialize_for_local_client", "synchronize_authoritative_sources", "consume_outbox", "process_outbox_batch", "retry_status", "outbox_status", "reconcile_sources", "understand_query", "validate_open_locator"]
