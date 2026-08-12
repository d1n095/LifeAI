"""Corpus manifest domain service (docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md §E/§P) —
create/track/reconcile a `source_import_batches` row. No AI, no provider, no network: this is
pure bookkeeping over counters, the same "deterministic first" discipline as
app/rag/zip_import.py's own safety checks.

Every recording function here is a thin, explicit increment — callers (the corpus-ingest job,
or library_import.py's per-file pipeline when a batch_id is attached) decide WHEN a file was
discovered/stored/parsed/duplicated/failed/unsupported; this module only ever adds 1 to the
right counter and never infers an outcome on its own, matching §12's "no semantic truth in
bootstrap" rule applied to bookkeeping: a count is a fact, not an interpretation.
"""

import datetime
import uuid

from sqlalchemy.orm import Session

from app.models.source_import_batch import SourceImportBatch, SourceImportBatchFailure, SourceImportBatchStatus


def create_batch(db: Session, owner_id: uuid.UUID, *, label: str, source_description: str | None = None) -> SourceImportBatch:
    batch = SourceImportBatch(owner_id=owner_id, label=label, source_description=source_description)
    db.add(batch)
    db.flush()
    return batch


def record_discovery_totals(
    db: Session,
    batch: SourceImportBatch,
    *,
    files: int = 0,
    archives: int = 0,
    conversations: int = 0,
    messages: int = 0,
    attachments: int = 0,
    total_bytes: int = 0,
) -> None:
    """Called once discovery has walked the whole intake root — populates the denominators
    everything else in this batch is measured against. `stored_originals_total`/`parsed_total`
    mirror `discovered_files` by default (every discovered file is expected to be stored and
    parsed); a caller with a different expectation (e.g. attachments counted separately) may
    adjust them afterward, but the default keeps §P's reconciliation check meaningful without
    every caller having to restate it."""
    batch.discovered_files += files
    batch.discovered_archives += archives
    batch.discovered_conversations += conversations
    batch.discovered_messages += messages
    batch.discovered_attachments += attachments
    batch.discovered_bytes += total_bytes
    batch.stored_originals_total += files
    batch.parsed_total += files
    batch.status = SourceImportBatchStatus.importing


def record_stored_original(db: Session, batch: SourceImportBatch) -> None:
    batch.stored_originals_done += 1


def record_parsed(db: Session, batch: SourceImportBatch) -> None:
    batch.parsed_done += 1


def record_duplicate(db: Session, batch: SourceImportBatch) -> None:
    """A duplicate (identical content already stored, per app/storage/local_fs.py's
    content-addressed dedup) still counts as a stored original -- the content genuinely exists
    in the Source Vault, just under a blob this batch didn't need to write itself -- but is
    tallied separately so the founder can see how much of a corpus was already present."""
    batch.stored_originals_done += 1
    batch.duplicate_count += 1


def record_unsupported(db: Session, batch: SourceImportBatch) -> None:
    """A file with no matching parser (docs/LIFE_SOURCE_FOUNDATION_BOOTSTRAP.md §F/§J) -- still
    stored (record_stored_original is called separately), just never parsed. Counts toward
    `unsupported_count`, one of the three buckets `parsed_done` must reconcile against."""
    batch.unsupported_count += 1


def record_failed(db: Session, batch: SourceImportBatch, *, source_ref: str, reason: str, retryable: bool = False) -> None:
    """A source that could not even be stored (hash/write failure) or could not be parsed
    despite having a matching parser (corrupt content). Both cases increment `failed_count` --
    the distinction between "never stored" and "stored but unparseable" is exactly what
    `source_import_batch_failures.reason` records in free text for a human to read, not a
    separate counter axis; §P's reconciliation check only needs the aggregate."""
    batch.failed_count += 1
    db.add(
        SourceImportBatchFailure(
            owner_id=batch.owner_id,
            batch_id=batch.id,
            source_ref=source_ref,
            reason=reason,
            retryable=retryable,
        )
    )


def try_mark_completed(db: Session, batch: SourceImportBatch) -> bool:
    """Attempts to mark the batch completed. Returns False (and leaves status as `importing`)
    if the counters don't yet reconcile per §P -- this is the Python-side pre-check for
    migration 0037's `ck_sib_completed_reconciles` CHECK, so a caller gets a clear "not done
    yet" instead of a database error. Returns True once genuinely complete."""
    if not batch.reconciles():
        return False
    batch.status = SourceImportBatchStatus.completed
    batch.completed_at = datetime.datetime.now(datetime.timezone.utc)
    return True


def mark_partial_or_failed(db: Session, batch: SourceImportBatch) -> None:
    """Called when a batch's processing run ends (job completes/fails/is cancelled) without
    every discovered source accounted for. `partial` if SOME sources succeeded, `failed` if
    none did — never silently left as `importing` forever, which would make the batch
    invisible to a founder checking "is my corpus import still running or did it die"."""
    if batch.stored_originals_done > 0 or batch.parsed_done > 0:
        batch.status = SourceImportBatchStatus.partial
    else:
        batch.status = SourceImportBatchStatus.failed
