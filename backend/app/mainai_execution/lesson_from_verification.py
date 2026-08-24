"""Bounded EngineeringLesson writer from exhausted verification failures.

SIGNAL PRODUCER != TRUTH WRITER: this module only accepts durable structured verification
evidence already persisted (or about to be persisted) as MainAITaskEvent.detail via
`VerificationResult.evidence()` — never `str(exc)`, provider prose, or free-form logs.

Production chain this closes (when wired from `_finalize_task_outcome`):
  verification failure event (structured steps)
  → attempts exhausted → task `failed`
  → record_lesson (provenance: goal/task/job + evidence)
  → conflict tick / plan-time apply already exist

Deliberately does NOT write on:
  - `retryable_failed` (recovery not exhausted)
  - exception-path finalize (`evidence` with only `{"error": ...}`)
  - empty verification_plan trivial pass/fail without failed steps
  - CI-wait timeout/failure (separate source; not verification step evidence)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.mainai_execution.lessons import record_lesson
from app.models.mainai_execution import (
    EngineeringLesson,
    EngineeringLessonConfidence,
    EngineeringLessonSeverity,
    MainAITask,
)


_SOURCE_TYPE = "verification_exhausted"


def _source_ref(*, goal_id: uuid.UUID, task_id: uuid.UUID, job_id: uuid.UUID | None) -> str:
    job_part = str(job_id) if job_id is not None else "no_job"
    return f"goal:{goal_id}/task:{task_id}/job:{job_part}"


def is_structured_verification_evidence(evidence: dict[str, Any] | None) -> bool:
    """True only for the shape `VerificationResult.evidence()` produces (plus optional
    `work_result` added by execution_job). Rejects exception-path `{error: str}` payloads."""
    if not isinstance(evidence, dict):
        return False
    if "steps" not in evidence or not isinstance(evidence["steps"], list):
        return False
    if evidence.get("passed") is not False:
        return False
    # Fail closed: bare exception finalize uses {"error": str(exc)} with no steps.
    if set(evidence.keys()) <= {"error"}:
        return False
    failed_steps = [s for s in evidence["steps"] if isinstance(s, dict) and s.get("passed") is False]
    return bool(failed_steps)


def _failed_step_summaries(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for step in evidence.get("steps") or []:
        if not isinstance(step, dict) or step.get("passed") is not False:
            continue
        detail = step.get("detail") if isinstance(step.get("detail"), dict) else {}
        # Keep only closed keys — never dump full stdout/stderr into lesson prose.
        compact_detail = {
            k: detail[k]
            for k in ("kind", "target", "returncode", "error", "timeout_seconds")
            if k in detail
        }
        out.append({"kind": step.get("kind"), "detail": compact_detail})
    return out


def maybe_record_lesson_from_exhausted_verification(
    db: Session,
    *,
    task: MainAITask,
    evidence: dict[str, Any],
    job_id: uuid.UUID | None = None,
) -> EngineeringLesson | None:
    """Record at most one active lesson per (goal, task, job) source_ref when verification
    evidence is structured and this finalize is the attempts-exhausted terminal failure.

    Caller must only invoke when `task.status` is (or is about to be) `failed` after a
    verification_failed path — never from cancel, success, or retryable_failed."""
    if not is_structured_verification_evidence(evidence):
        return None

    failed_steps = _failed_step_summaries(evidence)
    if not failed_steps:
        return None

    source_ref = _source_ref(goal_id=task.goal_id, task_id=task.id, job_id=job_id)
    existing = db.execute(
        select(EngineeringLesson).where(
            EngineeringLesson.source_type == _SOURCE_TYPE,
            EngineeringLesson.source_ref == source_ref,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    kinds = sorted({str(s.get("kind") or "unknown") for s in failed_steps})
    task_type = task.task_type or "unknown"
    problem = (
        f"Task {task.id} ({task_type}) exhausted verification attempts "
        f"({task.attempts}/{task.max_attempts}); failed step kinds: {', '.join(kinds)}."
    )
    evidence_text = (
        f"Durable MainAITask verification_failed evidence (structured steps only). "
        f"source_ref={source_ref}. failed_steps={failed_steps!r}."
    )
    applies_to = sorted({task_type, "verification", "task_execution", *kinds})

    return record_lesson(
        db,
        problem=problem,
        root_cause=(
            "Verification plan steps reported durable failure after all scheduled attempts; "
            "automatic retry/backoff did not recover. Exact failing step kinds and targets are "
            "in evidence (not free-form exception text)."
        ),
        affected_component=f"mainai_execution.verification.{task_type}",
        severity=EngineeringLessonSeverity.medium,
        evidence=evidence_text,
        fix=(
            "Inspect the named verification targets/regression coverage for this task_type; "
            "harden the failing step or the work that precedes it before re-dispatch."
        ),
        general_rule=(
            "Only promote exhausted, structured verification failures into EngineeringLesson — "
            "never one-shot provider/exception strings. Provenance must name goal/task/job."
        ),
        applies_to=applies_to,
        source_type=_SOURCE_TYPE,
        source_ref=source_ref,
        created_by="mainai_execution.lesson_from_verification",
        first_seen_at=datetime.utcnow(),
        regression_test=None,
        confidence=EngineeringLessonConfidence.likely,
    )
