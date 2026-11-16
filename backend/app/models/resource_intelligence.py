"""MainAI Resource Intelligence + Context Lifecycle + Cost/Quota + Agent Efficiency — Part 1.

See docs/mainai_v2/MAINAI_RESOURCE_CONTEXT_COST_RECONCILIATION.md for the full architecture
decision. One new, additive table (migration 0071): `agent_resource_telemetry_samples`, keyed
to the REAL `AgentDispatchExecution.attempt_id` (app.models.agent_coordination) — never a
parallel session identity. A caller (the CLI harness itself, or a future adapter) records what
it actually knows about one point-in-time observation of a running agent session; nothing here
infers or fabricates a token count the caller did not actually report.

Structurally an ordinary, owner-scoped, RLS-isolated, mutable-row table — matching
`AgentDispatchExecution`'s own precedent (migration 0047) exactly, NOT
`AgentWorkAssignmentEvent`'s append-only-with-deny-mutation-trigger shape: this table's own
`app.resource_intelligence.telemetry` service layer only ever INSERTs (never UPDATEs/DELETEs)
by convention, but no DB-level deny-mutation trigger is added here on purpose — this table's
own `(assignment_id, owner_id)` FK cascades from `agent_work_assignments`, which is itself
cleaned up by the EXISTING, untouched `erase_own_agent_coordination_children()` (migration
0046/0047, owned by `app.agent_coordination`, out of this package's scope to modify). A
deny-mutation trigger on THIS table would fire during that cascade and abort the existing
erasure function's own `DELETE FROM agent_work_assignments` — a real regression risk for a
package this round is expressly forbidden from touching. `AgentDispatchExecution` avoids this
exact same trap by carrying no deny-mutation trigger of its own; this table follows that
precedent deliberately, not by oversight."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, ForeignKeyConstraint, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AgentResourceTelemetrySample(Base):
    """One point-in-time observation of ONE agent dispatch attempt's resource usage —
    correlates with `AgentDispatchExecution.attempt_id`, never a second session identity.
    Every numeric field is nullable and stays `None` when the caller did not actually observe
    it — `app.resource_intelligence.types.unknown_metric()` is the canonical downstream
    consumer of an absent value; nothing in this model or its service layer defaults a missing
    field to zero."""

    __tablename__ = "agent_resource_telemetry_samples"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    assignment_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    # Correlates with AgentDispatchExecution.attempt_id -- deliberately NOT a foreign key
    # (that column itself carries no FK anywhere else in this codebase either; it is a
    # correlation id, not an enforced reference -- see app.models.agent_coordination
    # .AgentDispatchExecution's own docstring).
    attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    sampled_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    context_used_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    context_window_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cached_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)

    __table_args__ = (
        ForeignKeyConstraint(
            ["assignment_id", "owner_id"], ["agent_work_assignments.id", "agent_work_assignments.owner_id"], ondelete="CASCADE"
        ),
    )
