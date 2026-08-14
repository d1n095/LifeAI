"""Corpus manifest domain service (PR #60 provisional proposal §E/§P) —
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

from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session

from app.models.source_import_batch import (
    SourceImportBatch,
    SourceImportBatchFailure,
    SourceImportBatchStatus,
    SourceImportFailureStage,
)


class InvalidCorpusBatchTransition(RuntimeError):
    pass


def create_batch(db: Session, owner_id: uuid.UUID, *, label: str, source_description: str | None = None) -> SourceImportBatch:
    batch = SourceImportBatch(owner_id=owner_id, label=label, source_description=source_description)
    db.add(batch)
    db.flush()
    return batch


def _atomic_increment(
    db: Session,
    batch: SourceImportBatch,
    *,
    also_set: dict | None = None,
    required_status: SourceImportBatchStatus | None = None,
    **deltas: int,
) -> None:
    """SQL-level `col = col + :delta` against the row directly, NOT a Python read-modify-write
    on the ORM-loaded object. Hardening pass, Section 6 (founder mandate, PR #61 pre-merge):
    every `record_*` function below used to do `batch.x += 1`, which SQLAlchemy flushes as
    `SET x = <python-computed literal>` -- two sessions that both load the row before either
    commits silently lose one increment under READ COMMITTED (Postgres's default), no error,
    no conflict, just a wrong final count. Proven empirically
    (tests/backend/rag/test_bootstrap_hardening.py: 20 concurrent increments produced 4, not
    20) before this fix, and used as a durable engineering lesson afterward. This directly
    threatened the N/N completeness proof §P exists to guarantee.

    `also_set` (plain column=value overwrites, e.g. status) is folded into the SAME UPDATE
    statement rather than left as a separate ORM attribute assignment -- a real bug this fix's
    own first draft hit: `db.expire(batch)` does NOT flush pending changes first (unlike a
    query's autoflush), so a plain `batch.status = x` set between two `_atomic_increment` calls
    was silently discarded by the second call's expire() before ever reaching the database.
    Keeping every write to this row inside one atomic statement per call avoids that class of
    bug entirely, not just the counter race it was originally written for.

    `db.expire(batch)` after the UPDATE discards the object's now-stale in-memory attributes
    without registering a pending write for them -- the next attribute read (e.g.
    `batch.reconciles()`) transparently re-SELECTs the row's true, current, atomically-updated
    state within the same transaction. Not `session.refresh()`: that issues the SELECT
    immediately (in the middle of a bookkeeping call that may not need it yet); expire() is
    lazy, one round-trip only when a caller actually reads an attribute again."""
    set_parts = [f"{col} = {col} + :{col}" for col in deltas]
    params: dict = dict(deltas)
    for col, value in (also_set or {}).items():
        set_parts.append(f"{col} = :{col}")
        params[col] = value
    set_clause = ", ".join(set_parts)
    params["id"] = str(batch.id)
    where = "id = :id"
    if required_status is not None:
        where += " AND status = :required_status"
        params["required_status"] = required_status.value
    result = db.execute(
        sa_text(f"UPDATE source_import_batches SET {set_clause} WHERE {where}"),  # noqa: S608 -- identifiers are fixed by this module
        params,
    )
    if result.rowcount != 1:
        raise InvalidCorpusBatchTransition(
            f"batch {batch.id} is not in required status {required_status.value!r}"
        )
    db.expire(batch)


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
    _atomic_increment(
        db,
        batch,
        also_set={"status": SourceImportBatchStatus.importing.value},
        required_status=SourceImportBatchStatus.discovering,
        discovered_files=files,
        discovered_archives=archives,
        discovered_conversations=conversations,
        discovered_messages=messages,
        discovered_attachments=attachments,
        discovered_bytes=total_bytes,
        stored_originals_total=files,
        parsed_total=files,
    )


def record_stored_original(db: Session, batch: SourceImportBatch) -> None:
    _atomic_increment(db, batch, stored_originals_done=1)


def record_parsed(db: Session, batch: SourceImportBatch) -> None:
    _atomic_increment(db, batch, parsed_done=1)


def record_duplicate(db: Session, batch: SourceImportBatch) -> None:
    """A duplicate (identical content already stored, per app/storage/local_fs.py's
    content-addressed dedup) still counts as a stored original -- the content genuinely exists
    in the Source Vault, just under a blob this batch didn't need to write itself -- but is
    tallied separately so the founder can see how much of a corpus was already present."""
    _atomic_increment(db, batch, stored_originals_done=1, duplicate_count=1)


def record_unsupported(db: Session, batch: SourceImportBatch) -> None:
    """A file with no matching parser (PR #60 provisional proposal §F/§J) -- still
    stored (record_stored_original is called separately), just never parsed. Counts toward
    `unsupported_count`, one of the terminal buckets stored originals reconcile against."""
    _atomic_increment(db, batch, unsupported_count=1)


def record_failed(
    db: Session,
    batch: SourceImportBatch,
    *,
    source_ref: str,
    reason: str,
    stage: SourceImportFailureStage,
    retryable: bool = False,
) -> None:
    """Records an explicitly classified failure.

    Storage failures account for a discovered source that never became a stored original.
    Parse failures account for an already-stored original that reached a terminal parser
    failure. Keeping those axes separate prevents one file from being double-counted in the
    two completion equations.
    """
    if stage is SourceImportFailureStage.storage:
        # Discovery initially expects every file to reach parsing. Once a source definitively
        # fails before storage, it cannot be a parse candidate; reduce that denominator in the
        # same atomic statement that records the terminal storage failure.
        _atomic_increment(db, batch, failed_count=1, storage_failed_count=1, parsed_total=-1)
    else:
        _atomic_increment(db, batch, failed_count=1, parse_failed_count=1)
    db.add(
        SourceImportBatchFailure(
            owner_id=batch.owner_id,
            batch_id=batch.id,
            source_ref=source_ref,
            reason=reason,
            retryable=retryable,
            failure_stage=stage,
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
