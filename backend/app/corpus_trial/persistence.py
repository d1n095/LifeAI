"""Durable history for `app.corpus_trial.harness.run_trial()` results. See migration 0052's
own module docstring for why this is a separate, append-only table rather than a new column
on an existing one, and why it does NOT reuse migration 0042's authority/basis vocabulary.

Kept deliberately separate from `harness.py`: `run_trial()` stays pure (no DB side effect
beyond the corpus recording itself already required to run it) -- a caller decides whether a
given run is worth persisting by calling `record_trial_run()` explicitly, never automatically."""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.corpus_trial.harness import TrialReport
from app.models.corpus_trial_run import CorpusTrialRun


class CorpusTrialRunError(ValueError):
    pass


def record_trial_run(
    db: Session, *, owner_id: uuid.UUID, report: TrialReport, idempotency_key: str, corpus_label: str = "bootstrap"
) -> CorpusTrialRun:
    """Persists a snapshot of one `TrialReport` -- never the corpus or the underlying
    `founder_memory_notes`/`diagnosis_records` rows the trial exercised, those remain
    independently queryable in their own tables. Idempotent by construction: replaying the
    same `idempotency_key` with the SAME summary returns the existing row; replaying it with a
    DIFFERENT summary is a caller bug and raises, never silently picks a winner."""

    values = dict(
        corpus_label=corpus_label, record_count=report.record_count, passed=report.passed,
        dimension_summary=report.summary, violation_counts={dim: len(v) for dim, v in report.dimension_violations.items()},
    )
    existing = db.execute(
        select(CorpusTrialRun).where(CorpusTrialRun.owner_id == owner_id, CorpusTrialRun.idempotency_key == idempotency_key)
    ).scalar_one_or_none()
    if existing:
        differing = [key for key, value in values.items() if getattr(existing, key) != value]
        if differing:
            raise CorpusTrialRunError(f"idempotency key reused with a different trial summary: {', '.join(sorted(differing))}")
        return existing

    row = CorpusTrialRun(owner_id=owner_id, idempotency_key=idempotency_key, **values)
    db.add(row)
    db.flush()
    return row


def list_trial_runs(db: Session, *, owner_id: uuid.UUID, corpus_label: str | None = None) -> list[CorpusTrialRun]:
    stmt = select(CorpusTrialRun).where(CorpusTrialRun.owner_id == owner_id)
    if corpus_label is not None:
        stmt = stmt.where(CorpusTrialRun.corpus_label == corpus_label)
    return list(db.execute(stmt.order_by(CorpusTrialRun.run_at)).scalars().all())
