"""Durable, agent-session-scoped continuity -- the sibling
`app.mainai_executive.continuity.{save,load}_continuity_checkpoint()` itself calls for
(reconciliation doc §0's "real checkpoint precedent"): SAME real mechanism (`FounderMemoryNote`
+ `supersedes_note_id` chain via `app.founder_memory.record_founder_memory()`), NO new table,
scoped to an external agent CLI session (`agent_id`/`attempt_id`) instead of MainAI's own
internal executive-loop `session_id`/`phase`.

PROCESS MEMORY != AUTHORITY. ORM SESSION MEMORY != AUTHORITY. Recovery reads only durable
checkpoints; never invents continuation -- same doctrine `continuity.py`'s own module
docstring states, reused verbatim here."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.founder_memory import record_founder_memory
from app.models.founder_memory import FounderMemoryNote

CHECKPOINT_NOTE_TYPE = "observation"
CHECKPOINT_MARKER = "resource_intelligence_agent_session_checkpoint_v1"


@dataclass(frozen=True)
class AgentSessionCheckpoint:
    """Durable restart state for ONE external agent CLI session (Claude Code/Cursor/Codex/...)
    -- the founder's own named fields. Mirrors `ContinuityCheckpoint`'s mechanism, not its
    shape: this dataclass is scoped to `agent_id`/`attempt_id`, never MainAI's own internal
    `session_id`/`phase`."""

    agent_id: uuid.UUID
    attempt_id: uuid.UUID | str
    current_objective: str
    current_status: str
    exact_sha: str | None
    current_branch: str | None
    current_worktree: str | None
    open_p0: list[str]
    open_p1: list[str]
    what_was_tried: list[str]
    what_failed: list[str]
    what_passed: list[str]
    important_findings: list[str]
    current_test_evidence: list[str]
    next_action: str
    do_not_repeat: list[str]
    authority_boundaries: list[str]
    unresolved_questions: list[str]
    critical_session_only_facts: list[str]
    provenance: dict[str, Any] = field(default_factory=dict)


def checkpoint_to_dict(cp: AgentSessionCheckpoint) -> dict[str, Any]:
    return {
        "marker": CHECKPOINT_MARKER,
        "agent_id": str(cp.agent_id),
        "attempt_id": str(cp.attempt_id),
        # Preserves the exact caller-supplied type of attempt_id (uuid.UUID | str) across a
        # save/load round trip -- see checkpoint_from_dict()'s own use of this flag.
        "attempt_id_is_uuid": isinstance(cp.attempt_id, uuid.UUID),
        "current_objective": cp.current_objective,
        "current_status": cp.current_status,
        "exact_sha": cp.exact_sha,
        "current_branch": cp.current_branch,
        "current_worktree": cp.current_worktree,
        "open_p0": list(cp.open_p0),
        "open_p1": list(cp.open_p1),
        "what_was_tried": list(cp.what_was_tried),
        "what_failed": list(cp.what_failed),
        "what_passed": list(cp.what_passed),
        "important_findings": list(cp.important_findings),
        "current_test_evidence": list(cp.current_test_evidence),
        "next_action": cp.next_action,
        "do_not_repeat": list(cp.do_not_repeat),
        "authority_boundaries": list(cp.authority_boundaries),
        "unresolved_questions": list(cp.unresolved_questions),
        "critical_session_only_facts": list(cp.critical_session_only_facts),
        "provenance": dict(cp.provenance),
    }


def checkpoint_from_dict(data: dict[str, Any]) -> AgentSessionCheckpoint:
    if data.get("marker") != CHECKPOINT_MARKER:
        raise ValueError("not a resource_intelligence agent session checkpoint")
    raw_attempt_id = data["attempt_id"]
    attempt_id: uuid.UUID | str = uuid.UUID(raw_attempt_id) if data.get("attempt_id_is_uuid") else raw_attempt_id
    return AgentSessionCheckpoint(
        agent_id=uuid.UUID(data["agent_id"]),
        attempt_id=attempt_id,
        current_objective=str(data.get("current_objective") or ""),
        current_status=str(data.get("current_status") or ""),
        exact_sha=data.get("exact_sha"),
        current_branch=data.get("current_branch"),
        current_worktree=data.get("current_worktree"),
        open_p0=list(data.get("open_p0") or []),
        open_p1=list(data.get("open_p1") or []),
        what_was_tried=list(data.get("what_was_tried") or []),
        what_failed=list(data.get("what_failed") or []),
        what_passed=list(data.get("what_passed") or []),
        important_findings=list(data.get("important_findings") or []),
        current_test_evidence=list(data.get("current_test_evidence") or []),
        next_action=str(data.get("next_action") or ""),
        do_not_repeat=list(data.get("do_not_repeat") or []),
        authority_boundaries=list(data.get("authority_boundaries") or []),
        unresolved_questions=list(data.get("unresolved_questions") or []),
        critical_session_only_facts=list(data.get("critical_session_only_facts") or []),
        provenance=dict(data.get("provenance") or {}),
    )


def _latest_checkpoint_note(db: Session, *, owner_id: uuid.UUID, agent_id: uuid.UUID, attempt_id: uuid.UUID | str) -> FounderMemoryNote | None:
    rows = db.execute(
        select(FounderMemoryNote)
        .where(
            FounderMemoryNote.owner_id == owner_id,
            FounderMemoryNote.note_type == CHECKPOINT_NOTE_TYPE,
            FounderMemoryNote.status == "active",
        )
        .order_by(FounderMemoryNote.observed_at.desc())
    ).scalars()
    for note in rows:
        prov = note.provenance or {}
        if prov.get("kind") == CHECKPOINT_MARKER and prov.get("agent_id") == str(agent_id) and prov.get("attempt_id") == str(attempt_id):
            return note
    return None


def save_agent_session_checkpoint(db: Session, *, owner_id: uuid.UUID, checkpoint: AgentSessionCheckpoint) -> FounderMemoryNote:
    """Append-only checkpoint, mirroring `continuity.save_continuity_checkpoint()`'s exact
    mechanism: a later save for the SAME `(agent_id, attempt_id)` supersedes the prior note via
    `supersedes_note_id`, never mutates the old note's own `content`."""

    payload = checkpoint_to_dict(checkpoint)
    prior_note = _latest_checkpoint_note(db, owner_id=owner_id, agent_id=checkpoint.agent_id, attempt_id=checkpoint.attempt_id)
    supersedes = prior_note.id if prior_note is not None else None

    content = (
        f"[agent session checkpoint] agent_id={checkpoint.agent_id} attempt_id={checkpoint.attempt_id} "
        f"status={checkpoint.current_status} objective={checkpoint.current_objective}"
    )
    return record_founder_memory(
        db,
        owner_id=owner_id,
        note_type=CHECKPOINT_NOTE_TYPE,
        content=content,
        # FounderMemoryNote.idempotency_key is varchar(128) -- agent_id/attempt_id can each be
        # a full UUID, so this key intentionally does NOT embed either verbatim (that alone
        # can exceed 128 chars); uniqueness/idempotent-replay is via the trailing uuid4()
        # only, exactly matching continuity.save_continuity_checkpoint()'s own "always a fresh
        # key per save" behavior -- lookup by (agent_id, attempt_id) always goes through
        # `_latest_checkpoint_note()`'s own provenance scan, never through this key.
        idempotency_key=f"ri-sess-cp:{uuid.uuid4()}",
        authority="deterministic_source",
        basis="deterministic",
        supersedes_note_id=supersedes,
        source="resource_intelligence.session_checkpoint",
        provenance={
            "kind": CHECKPOINT_MARKER,
            "agent_id": str(checkpoint.agent_id),
            "attempt_id": str(checkpoint.attempt_id),
            "checkpoint": payload,
        },
    )


def load_agent_session_checkpoint(db: Session, *, owner_id: uuid.UUID, agent_id: uuid.UUID, attempt_id: uuid.UUID | str) -> AgentSessionCheckpoint | None:
    note = _latest_checkpoint_note(db, owner_id=owner_id, agent_id=agent_id, attempt_id=attempt_id)
    if note is None:
        return None
    raw = (note.provenance or {}).get("checkpoint")
    if not isinstance(raw, dict):
        return None
    return checkpoint_from_dict(raw)
