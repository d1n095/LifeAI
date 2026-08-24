"""Append-only EngineeringLesson effectiveness evidence (migration 0058).

SIGNAL PRODUCER != TRUTH WRITER: a row here is an OBSERVATION of what happened to an
already-applied lesson's own `regression_test` on one task finalize. It never rewrites
`EngineeringLesson.confidence`/`evidence`/`root_cause`/`status`. Turning many observations into
a founder-reviewable confidence signal is a deliberately separate, later step.

Fail-closed about causality — the real risk in a table like this is manufacturing evidence out
of coincidence:
  - the lesson was not recorded as applied to this task at plan time -> no row at all
  - the lesson's own target is absent from the verification evidence -> insufficient_evidence,
    never reinforced (an unrelated later success is NOT evidence a lesson worked)
  - the outcome is derived from the lesson's OWN target's step result, never from the task's
    overall pass/fail alone

Owner-scoped (RLS, composite owner-anchored FKs) even though `EngineeringLesson` itself is
founder-wide: the row carries owner-scoped facts (task/goal/job ids and a verification target
path out of that owner's plan), so it inherits the sensitivity of its evidence, not of its
subject. See migration 0058's docstring.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LessonEffectivenessOutcome(str, enum.Enum):
    """Every value here has a real producer in `lesson_effectiveness.py` — deliberately, since
    a state with no writer is the exact defect class this whole lane exists to remove. See
    `classify_lesson_outcome()` for the evidence that produces each one."""

    # The lesson's own regression target ran and passed, and the task's verification passed.
    reinforced = "reinforced"
    # The target ran and passed, but the task failed on OTHER steps — the lesson's guard held;
    # this finalize says nothing about the rest.
    context_specific = "context_specific"
    # The target ran and genuinely failed (pytest exit 1) — the lesson's prescribed guard did
    # not hold where the lesson itself said it would.
    weakened = "weakened"
    # The target could not be used as a guard at all (pytest exit 4/5: usage error / nothing
    # collected) — the lesson's own named regression_test is not a valid guard as written.
    contradicted = "contradicted"
    # Applied, but this finalize cannot speak to it: target absent from the evidence, lesson
    # has no regression_test, the run was interrupted, or the lesson is under unresolved
    # conflict review.
    insufficient_evidence = "insufficient_evidence"
    # The lesson was superseded by a newer one before this evidence arrived.
    superseded = "superseded"


class LessonEffectivenessAttributionConfidence(str, enum.Enum):
    high = "high"
    medium = "medium"
    low = "low"


class EngineeringLessonEffectiveness(Base):
    """One append-only effectiveness observation for one (goal, task, job, lesson) finalize.

    Idempotent on `source_ref` so a replayed or retried finalize cannot inflate a lesson's
    evidence by re-observing the same outcome.
    """

    __tablename__ = "engineering_lesson_effectiveness"
    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_engineering_lesson_effectiveness_source_ref"),
        ForeignKeyConstraint(
            ["task_id", "owner_id"],
            ["mainai_tasks.id", "mainai_tasks.owner_id"],
            name="fk_engineering_lesson_effectiveness_task_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["goal_id", "owner_id"],
            ["mainai_goals.id", "mainai_goals.owner_id"],
            name="fk_engineering_lesson_effectiveness_goal_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["job_id", "owner_id"],
            ["mainai_jobs.id", "mainai_jobs.owner_id"],
            name="fk_engineering_lesson_effectiveness_job_owner",
            ondelete="SET NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Bare FK by necessity, not oversight: engineering_lessons is founder-wide and has no
    # owner_id to anchor against. Many owners' evidence may legitimately cite one lesson.
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("engineering_lessons.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # Plain varchar + DB CHECK (migration 0058), the same convention migrations 0055/0057's
    # models use — the enums above are the single source of the allowed values in Python.
    outcome: Mapped[str] = mapped_column(String(24))
    attribution_confidence: Mapped[str] = mapped_column(String(8))
    # Why this lesson was considered relevant here — derived from durable plan-time
    # provenance (`lessons_applied`), never re-inferred at read time.
    relevance_reason: Mapped[str] = mapped_column(Text)
    verification_target: Mapped[str | None] = mapped_column(String(256), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_ref: Mapped[str] = mapped_column(String(320))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
