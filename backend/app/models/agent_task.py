import enum
import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AgentRole(str, enum.Enum):
    """Which role a dispatched provider call is playing — "börja med kodagent och
    granskningsagent" (CLAUDE.md's 2026-07-26 MainAI Core direction). Both reuse the same
    swappable provider adapters (app/providers/registry.py); this only labels the prompt/intent,
    not a different execution engine."""

    code = "code"
    review = "review"


class AgentTaskStatus(str, enum.Enum):
    """A task's current stage. Never skips backward silently — e.g. `needs_correction` always
    comes from an explicit review verdict, not an inferred guess."""

    created = "created"
    dispatched = "dispatched"
    result_recorded = "result_recorded"
    reviewed_approved = "reviewed_approved"
    reviewed_needs_correction = "reviewed_needs_correction"
    reviewed_rejected = "reviewed_rejected"
    pr_prepared = "pr_prepared"
    pr_opened = "pr_opened"
    ready_for_human = "ready_for_human"


class AgentTaskEventType(str, enum.Enum):
    """One entry in an agent task's append-only history — never overwritten, same
    never-delete-only-append pattern as ProjectNote/ProjectBranchPRStatus (see
    app/models/project_memory.py). This is "granskningshistoriken" CLAUDE.md's MainAI Core
    direction requires MainAI to own."""

    dispatched = "dispatched"
    result_recorded = "result_recorded"
    test_results_recorded = "test_results_recorded"
    reviewed = "reviewed"
    github_pr_proposed = "github_pr_proposed"
    github_branch_created = "github_branch_created"
    github_commit_pushed = "github_commit_pushed"
    github_pr_opened = "github_pr_opened"
    merge_blocked = "merge_blocked"
    merge_completed = "merge_completed"


class AgentTask(Base):
    """One scoped work order MainAI created for a code or review agent — always derived from a
    real ProjectNote (a blocker/next_step), always with explicit acceptance criteria and
    constraints, never a vague "go fix things" instruction (CLAUDE.md's 2026-07-26 MainAI Core
    direction, section 6). The task itself never contains the agent's output — that lives in
    AgentTaskEvent rows, so the full history (dispatch -> result -> review -> GitHub) is always
    reconstructable, not just the latest state.

    Not RLS-protected — founder-wide project state, same rationale as ProjectNote."""

    __tablename__ = "agent_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text)
    target_files: Mapped[list] = mapped_column(JSON, default=list)
    constraints: Mapped[str | None] = mapped_column(Text, nullable=True)  # what must NOT change
    acceptance_criteria: Mapped[str] = mapped_column(Text)
    required_tests: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[AgentTaskStatus] = mapped_column(Enum(AgentTaskStatus), default=AgentTaskStatus.created)
    source_note_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("project_notes.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AgentTaskEvent(Base):
    """One append-only step in a task's lifecycle — a dispatch, a recorded diff/result, a
    recorded test run, a review verdict, or a GitHub operation. `payload` holds the
    event-specific structured detail (provider/model + raw response text for `dispatched`;
    verdict + findings for `reviewed`; branch/commit/PR refs for the `github_*` events).

    This table alone is enough to answer "vilka agenter arbetar med vad" and "vilka resultat
    är verifierade" (CLAUDE.md's MainAI Core success metric) without re-deriving anything."""

    __tablename__ = "agent_task_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agent_tasks.id"), index=True)
    event_type: Mapped[AgentTaskEventType] = mapped_column(Enum(AgentTaskEventType))
    role: Mapped[AgentRole | None] = mapped_column(Enum(AgentRole), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
