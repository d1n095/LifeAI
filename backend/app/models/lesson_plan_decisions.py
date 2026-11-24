import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class LessonPlanDecisionType(str, enum.Enum):
    """The closed vocabulary of HOW a lesson is allowed to change a plan. Today the lesson
    layer has exactly one real lever — appending its regression_test as a targeted_tests
    step (apply_lessons_to_verification_plan) — and the enum exists so the rollup can treat
    future levers (constraint additions, strategy notes) as distinct evidence classes
    instead of overloading the one value."""

    verification_step_added = "verification_step_added"


class EngineeringLessonPlanDecision(Base):
    """One durable row: THIS lesson changed THAT task's plan in THIS way, written at the
    moment of the change (planner.create_plan, after apply_lessons_to_verification_plan
    appended the lesson's regression target). The other half of migration 0058's
    guard-observation table: observations say what a lesson's guard DID at finalize; this
    says how the lesson got INTO that task's plan in the first place. Together they let
    lesson_effectiveness() measure a rule's standing from applied-count to guard outcome
    without re-deriving provenance from MainAITaskEvent detail strings.

    Append-only (SELECT/INSERT narrowing in app/rls.py): a decision is history. Idempotent
    on (task_id, lesson_id) — a replayed planner pass cannot inflate a lesson's applied
    count. Re-planning a goal creates a NEW task row, so a genuinely new decision is a new
    row, never a conflict.

    Owner anchoring mirrors migrations 0019/0056/0058: composite (task_id, owner_id) /
    (goal_id, owner_id) FKs make cross-owner references foreign-key violations at the
    database. lesson_id is necessarily a bare FK — engineering_lessons is deliberately
    founder-wide (app/models/mainai_execution.py); many owners' decisions may reference one
    founder-wide lesson, which is the correct direction, not a hole.
    """

    __tablename__ = "engineering_lesson_plan_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["task_id", "owner_id"],
            ["mainai_tasks.id", "mainai_tasks.owner_id"],
            ondelete="CASCADE",
            name="fk_engineering_lesson_plan_decisions_task_owner",
        ),
        ForeignKeyConstraint(
            ["goal_id", "owner_id"],
            ["mainai_goals.id", "mainai_goals.owner_id"],
            ondelete="CASCADE",
            name="fk_engineering_lesson_plan_decisions_goal_owner",
        ),
        UniqueConstraint("task_id", "lesson_id", name="uq_engineering_lesson_plan_decisions_source_ref"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("engineering_lessons.id", ondelete="CASCADE"), index=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    goal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    decision_type: Mapped[str] = mapped_column(String(40), default=LessonPlanDecisionType.verification_step_added.value)
    affected_step: Mapped[dict] = mapped_column(JSONB, default=dict)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
