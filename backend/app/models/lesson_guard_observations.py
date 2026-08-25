"""Append-only observations of what happened to an applied lesson's guard (migration 0058).

GUARD EVIDENCE != LESSON EFFECTIVENESS, and the naming here is deliberate rather than cautious
phrasing around a stronger claim. Applying a lesson today means `apply_lessons_to_verification_
plan()` appends the lesson's `regression_test` to the task's verification plan and records
`lessons_applied`. It does not alter the implementation strategy or the work itself. So a later
passing target proves only that this lesson's named guard was exercised and held in this
execution context — not that the lesson changed behavior or caused the outcome. Every enum value
below is therefore named for the observation, and `evidence_strength` describes how directly the
evidence speaks about the GUARD, never how confidently an outcome can be attributed to the
lesson. Nothing in this module may be read as "the lesson worked".

SIGNAL PRODUCER != TRUTH WRITER: a row is an OBSERVATION about one task finalize. It never
rewrites `EngineeringLesson.confidence`/`evidence`/`root_cause`/`status`. Turning many
observations into a founder-reviewable confidence signal is a deliberately separate, later step
— and a real effectiveness claim needs an edge that does not exist yet: durable provenance for
HOW a lesson altered the later plan or execution.

Fail-closed about relevance, the real risk being manufactured evidence out of coincidence:
  - the lesson was not recorded as applied to this task at plan time -> no row at all
  - the lesson's own target is absent from the verification evidence -> guard_not_exercised
    (an unrelated later success is NOT evidence about a lesson)
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


class LessonGuardOutcome(str, enum.Enum):
    """What was observed about the lesson's own regression guard — NOT a verdict on the lesson.

    Every value has a real producer in `lesson_guard_observations.py` — deliberately, since a
    state with no writer is the exact defect class this whole lane exists to remove. See
    `classify_guard_outcome()` for the evidence that produces each one.
    """

    # The lesson's own regression target ran and passed, and the task's verification passed.
    # Read this as "the guard held here", never as "the lesson caused this to pass": the guard
    # would also have passed on a task the lesson had no influence over.
    guard_held = "guard_held"
    # The target ran and passed, but the task failed on OTHER steps — the guard held; this
    # finalize says nothing about the rest.
    guard_held_task_failed_elsewhere = "guard_held_task_failed_elsewhere"
    # The target ran and genuinely failed (pytest exit 1) — the guard the lesson itself named
    # did not hold where the lesson said it would. This is the one direction where the evidence
    # IS about the lesson: the lesson's own claim about its guard was contradicted here.
    guard_failed = "guard_failed"
    # The target could not be used as a guard at all (pytest exit 4/5: usage error / nothing
    # collected) — the lesson's named regression_test is not a valid guard as written.
    guard_unusable = "guard_unusable"
    # Applied, but this finalize cannot speak to the guard: target absent from the evidence,
    # lesson has no regression_test, the run was interrupted, or the lesson is under unresolved
    # conflict review.
    guard_not_exercised = "guard_not_exercised"
    # The lesson was superseded by a newer one before this evidence arrived.
    lesson_superseded = "lesson_superseded"


class LessonGuardEvidenceStrength(str, enum.Enum):
    """How directly this observation speaks about the GUARD. Deliberately NOT a causal
    attribution confidence: no value here — including `direct` — licenses the claim that the
    lesson affected the outcome. `direct` means the guard itself ran and its own result was
    observed; `partial` means the guard's result is known but the surrounding context limits
    what it says; `none` means the guard was not exercised at all."""

    direct = "direct"
    partial = "partial"
    none = "none"


class EngineeringLessonGuardObservation(Base):
    """One append-only guard observation for one (goal, task, job, lesson) finalize.

    Idempotent on `source_ref` so a replayed or retried finalize cannot inflate a lesson's
    evidence by re-observing the same outcome.
    """

    __tablename__ = "engineering_lesson_guard_observations"
    __table_args__ = (
        UniqueConstraint("source_ref", name="uq_engineering_lesson_guard_observations_source_ref"),
        ForeignKeyConstraint(
            ["task_id", "owner_id"],
            ["mainai_tasks.id", "mainai_tasks.owner_id"],
            name="fk_engineering_lesson_guard_observations_task_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["goal_id", "owner_id"],
            ["mainai_goals.id", "mainai_goals.owner_id"],
            name="fk_engineering_lesson_guard_observations_goal_owner",
            ondelete="CASCADE",
        ),
        # Column-specific SET NULL: owner_id is NOT NULL and participates in this composite FK,
        # so a plain SET NULL would null it too and make every mainai_jobs delete fail. Only the
        # job pointer may be dropped; the observation stays, still owned. SQLAlchemy renders no
        # column list, so the constraint is authored in migration 0058 -- this declaration must
        # stay in sync with it, which `test_job_fk_nulls_only_the_job_column` enforces against
        # the real catalog.
        ForeignKeyConstraint(
            ["job_id", "owner_id"],
            ["mainai_jobs.id", "mainai_jobs.owner_id"],
            name="fk_engineering_lesson_guard_observations_job_owner",
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
    outcome: Mapped[str] = mapped_column(String(40))
    evidence_strength: Mapped[str] = mapped_column(String(8))
    # Why this lesson was considered relevant here — derived from durable plan-time
    # provenance (`lessons_applied`), never re-inferred at read time.
    relevance_reason: Mapped[str] = mapped_column(Text)
    # The lesson's own `regression_test` as it appeared in the verification plan, or NULL when
    # the lesson named no guard.
    guard_target: Mapped[str | None] = mapped_column(String(256), nullable=True)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    source_ref: Mapped[str] = mapped_column(String(320))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
