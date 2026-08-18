"""Life Corpus Trial Run History -- see migration 0052's own module docstring for the full
rationale. A durable, append-only record of what one `app.corpus_trial.harness.run_trial()`
call measured, never a copy of the corpus or the underlying provenance rows it exercised."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class CorpusTrialRun(Base):
    """One owner-scoped, immutable row per trial run -- `record_count`/`passed`/`dimension_
    summary`/`violation_counts` are a snapshot of the `TrialReport` the run produced, never
    the corpus or the recorded facts themselves (those remain in `founder_memory_notes`/
    `diagnosis_records`, queryable independently). A re-run, even of the identical corpus,
    is always a NEW row -- this table is never updated in place (DB trigger enforces this,
    same pattern as `CapabilityObservationEvent`)."""

    __tablename__ = "corpus_trial_runs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    corpus_label: Mapped[str] = mapped_column(String(64), default="bootstrap")
    record_count: Mapped[int] = mapped_column(Integer)
    passed: Mapped[bool] = mapped_column(Boolean)
    dimension_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    violation_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    run_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
