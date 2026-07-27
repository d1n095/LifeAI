"""Chat context-status classification (2026-07-27 incident): `app/routers/chat.py`'s
`_attempt_assistant_reply()` used to collapse EVERY zero-retrieval-hit condition into the
single fixed string "Ingen relevant kunskap hittades." — whether the real cause was a worker
process that isn't running, a file still queued, a document paused on a missing embedding
provider, indexing that failed outright, a search-time provider failure, genuinely no matching
content, or simply no uploaded files at all. A generative provider with no real reason to give
then improvised a plausible-sounding excuse ("I have no access to your upload history...") —
a confirmed production incident (see docs/BRANCH_REGISTRY.md's file-ingestion audit), not a
hypothetical.

This module classifies WHY retrieval found nothing using ONLY signals that already exist
elsewhere:
  - `app/models/document.py`'s `IndexStatus` (per-document pipeline state),
  - the same worker-heartbeat freshness check `app/routers/library.py`'s `ops_status()` uses
    (`ImportJob.last_heartbeat_at` within `3 * worker_lease_seconds`),
  - `classify_provider_exception()` for a search-time provider failure (never a raw exception
    string — see that function's own docstring on why).

It is a labeling/reporting layer over those already-existing states, not a new parallel status
system — it never invents a new document/job lifecycle state of its own, and every count it
returns is a real row count for the requesting owner, never a guess.
"""

import enum
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.document import Document, IndexStatus
from app.models.import_job import ImportJob


class ContextStatusReason(str, enum.Enum):
    worker_unavailable = "worker_unavailable"
    files_processing = "files_processing"
    awaiting_provider = "awaiting_provider"
    search_provider_unavailable = "search_provider_unavailable"
    indexing_failed = "indexing_failed"
    no_relevant_match = "no_relevant_match"
    no_documents = "no_documents"


# See IndexStatus's own docstring for the full pipeline — these groupings only classify
# existing values, they don't add new ones.
_PROCESSING_STATUSES = {
    IndexStatus.pending,  # legacy value, kept only for old rows — still "in flight"
    IndexStatus.received,
    IndexStatus.original_storing,
    IndexStatus.original_stored,
    IndexStatus.extracting,
    IndexStatus.extracted,
    IndexStatus.awaiting_classification,
    IndexStatus.classifying,
    IndexStatus.embedding,
    IndexStatus.indexing,  # legacy value, kept only for old rows
}
_AWAITING_PROVIDER_STATUSES = {IndexStatus.awaiting_provider, IndexStatus.blocked_provider}
_FAILED_STATUSES = {IndexStatus.failed, IndexStatus.storage_failed, IndexStatus.extraction_failed, IndexStatus.indexing_failed}


@dataclass
class ContextStatus:
    reason: ContextStatusReason
    message: str
    pending_count: int = 0
    awaiting_provider_count: int = 0
    failed_count: int = 0
    indexed_count: int = 0
    total_document_count: int = 0
    worker_reachable: bool | None = None


def _worker_reachable(db: Session, owner_id: uuid.UUID) -> bool:
    """Identical freshness check to `app/routers/library.py`'s `ops_status()` — a worker that
    claimed a job and renewed its lease recently enough that it can't have silently died.
    Reads the same `ImportJob.last_heartbeat_at` column directly rather than importing from
    the router, since routers stay a thin HTTP layer per `docs/MAINAI_ARCHITECTURE.md` §3."""
    settings = get_settings()
    row = (
        db.query(ImportJob.last_heartbeat_at)
        .filter(ImportJob.owner_id == owner_id, ImportJob.last_heartbeat_at.isnot(None))
        .order_by(ImportJob.last_heartbeat_at.desc())
        .first()
    )
    if row is None or row[0] is None:
        return False
    return (datetime.utcnow() - row[0]) < timedelta(seconds=settings.worker_lease_seconds * 3)


def _plural(count: int, singular: str, plural: str) -> str:
    return singular if count == 1 else plural


def build_context_status(
    db: Session,
    owner_id: uuid.UUID,
    *,
    hit_count: int,
    retrieval_degraded_reason: str | None,
) -> ContextStatus | None:
    """Called from `chat.py` right after `retrieve_context()`. Returns `None` when hits were
    actually found — there is nothing to explain. Otherwise returns exactly one reason, in the
    priority order a founder would want to hear it: a search-time provider failure (freshest,
    most direct signal) outranks a stalled worker, which outranks a provider paused at
    ingestion time, which outranks files still normally processing, which outranks an outright
    indexing failure, which outranks "your files are fine, this question just didn't match."
    """
    if hit_count > 0:
        return None

    if retrieval_degraded_reason is not None:
        return ContextStatus(
            reason=ContextStatusReason.search_provider_unavailable,
            message=f"Jag kan inte söka i ditt bibliotek just nu: {retrieval_degraded_reason}",
        )

    statuses = [
        row[0]
        for row in db.query(Document.status).filter(Document.uploaded_by == owner_id, Document.deleted_at.is_(None)).all()
    ]
    total = len(statuses)
    if total == 0:
        return ContextStatus(reason=ContextStatusReason.no_documents, message="Det finns inga uppladdade filer i det här projektet ännu.")

    pending_count = sum(1 for s in statuses if s in _PROCESSING_STATUSES)
    awaiting_provider_count = sum(1 for s in statuses if s in _AWAITING_PROVIDER_STATUSES)
    failed_count = sum(1 for s in statuses if s in _FAILED_STATUSES)
    indexed_count = sum(1 for s in statuses if s == IndexStatus.indexed)

    counts = dict(
        pending_count=pending_count,
        awaiting_provider_count=awaiting_provider_count,
        failed_count=failed_count,
        indexed_count=indexed_count,
        total_document_count=total,
    )

    if pending_count > 0:
        worker_reachable = _worker_reachable(db, owner_id)
        if not worker_reachable:
            return ContextStatus(
                reason=ContextStatusReason.worker_unavailable,
                message=(
                    f"Jag hittar inget underlag ännu eftersom {pending_count} uppladdad{_plural(pending_count, '', 'e')} "
                    f"{_plural(pending_count, 'fil väntar', 'filer väntar')} på indexering. Worker-processen är inte tillgänglig."
                ),
                worker_reachable=False,
                **counts,
            )

    if awaiting_provider_count > 0:
        return ContextStatus(
            reason=ContextStatusReason.awaiting_provider,
            message=(
                f"{_plural(awaiting_provider_count, 'Filen är', f'{awaiting_provider_count} filer är')} lagrad"
                f"{_plural(awaiting_provider_count, '', 'e')} men väntar på en fungerande embedding-leverantör."
            ),
            worker_reachable=True,
            **counts,
        )

    if pending_count > 0:
        return ContextStatus(
            reason=ContextStatusReason.files_processing,
            message=(
                f"{pending_count} {_plural(pending_count, 'fil bearbetas', 'filer bearbetas')} fortfarande — "
                "försök igen om en liten stund."
            ),
            worker_reachable=True,
            **counts,
        )

    if indexed_count == 0 and failed_count > 0:
        return ContextStatus(
            reason=ContextStatusReason.indexing_failed,
            message=f"{failed_count} {_plural(failed_count, 'fil kunde', 'filer kunde')} inte indexeras. Kontrollera biblioteket för detaljer.",
            **counts,
        )

    if indexed_count > 0:
        return ContextStatus(
            reason=ContextStatusReason.no_relevant_match,
            message="Dina filer är indexerade, men jag hittade ingen relevant träff för den här frågan.",
            **counts,
        )

    # Every document for this owner is in a state outside the four buckets above (e.g. all
    # cancelled) — honest fallback: there is no live file to search, but it would be false to
    # say "no files uploaded" when rows do exist, so this is its own message rather than
    # reusing `no_documents`'s.
    return ContextStatus(
        reason=ContextStatusReason.no_documents,
        message="De uppladdade filerna är inte längre tillgängliga för sökning (borttagna eller avbrutna).",
        **counts,
    )
