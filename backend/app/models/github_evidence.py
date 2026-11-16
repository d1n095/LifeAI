import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class GithubEvidenceCandidate(Base):
    """A claim that a piece of REAL, VERIFIED GitHub evidence (an open pull request or issue)
    MIGHT be worth turning into governed MainAI work -- never a claim that it is authorized.
    Mirrors app.models.work_candidate.WorkCandidate's exact "signal producer != truth writer"
    pattern (status starts 'unreviewed'; authorize_github_evidence_candidate() is the ONLY
    path to a real MainAIGoal, and it always requires an explicit authorized_by) -- but
    deliberately does NOT reuse that table or its FK.

    `WorkCandidate.source_entity_id` is a composite FK into `project_entities`, itself only
    ever reached from founder knowledge (`KnowledgeClaim` -> `InterpretationProposal` ->
    `ProjectEntity`, migrations 0054-0056). A GitHub PR/issue is live EXTERNAL SYSTEM STATE,
    not founder knowledge -- fabricating a fake `ProjectEntity` just to satisfy that FK would
    reuse the table's shape while defeating the invariant it exists to enforce (see PR #396's
    own investigation, and `docs/BRANCH_REGISTRY.md`'s 2026-11-14 entries for the full
    reasoning this table resolves). Built per the Founder's own 2026-11-14 directive
    ("prefer a dedicated, narrowly scoped evidence staging projection only if existing
    durable task/evidence tables cannot represent the data without violating founder-
    knowledge invariants") -- that condition is met here, so this is that projection, kept as
    small and reversible as the directive asked.

    `source_repo`/`source_kind`/`source_number` together identify the real GitHub item --
    never a bare FK into a table this app does not own (GitHub's own PRs/issues aren't rows
    in this database). `idempotency_key` is derived from exactly those three fields (see
    app.github_evidence.service.candidate_idempotency_key()), so recording the SAME PR/issue
    again is an UPDATE of its own mutable observation fields (title/signals/source_url/
    observed_at), never a duplicate row -- and, unlike app.work_candidates.service's stricter
    `_same()` (which REJECTS a reused key whose fields differ), this intentionally allows
    those fields to change: live CI/merge/staleness signals for the SAME PR are expected to
    change between scans, unlike a WorkCandidate's one-time classifier output. `status`/
    `authorized_goal_id`/`dismissed_reason` are a founder decision, never touched by the
    recorder -- only by authorize_github_evidence_candidate()/dismiss_github_evidence_
    candidate()."""

    __tablename__ = "github_evidence_candidates"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    source_repo: Mapped[str] = mapped_column(String(256))
    source_kind: Mapped[str] = mapped_column(String(32))  # "open_pull_request" | "open_issue"
    source_number: Mapped[int] = mapped_column(Integer)
    source_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text)
    # Deterministic signals from app.safe_planner.github_work_queue.WorkQueueItem.signals
    # (draft/stale_Nd/ci_red/ci_green/merge_conflict) -- facts, never a priority judgment.
    signals: Mapped[list] = mapped_column(JSONB, default=list)
    status: Mapped[str] = mapped_column(String(24), default="unreviewed")
    authorized_goal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mainai_goals.id", ondelete="SET NULL"), nullable=True
    )
    dismissed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSONB, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    observed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
