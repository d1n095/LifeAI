"""Extract durable pytest failure tails so Path A retry/replan can see them."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.mainai_execution import MainAITaskEvent, MainAITaskEventType


def latest_verification_failure_text(db: Session, task_id: uuid.UUID) -> str | None:
    event = (
        db.execute(
            select(MainAITaskEvent)
            .where(
                MainAITaskEvent.task_id == task_id,
                MainAITaskEvent.event_type == MainAITaskEventType.verification_failed,
            )
            .order_by(MainAITaskEvent.created_at.desc())
        )
        .scalars()
        .first()
    )
    if event is None:
        return None
    detail = event.detail or {}
    chunks: list[str] = []
    for step in detail.get("steps") or []:
        if not isinstance(step, dict) or step.get("passed"):
            continue
        step_detail = step.get("detail") or {}
        target = step_detail.get("target")
        stdout = str(step_detail.get("stdout_tail") or "")[-2000:]
        stderr = str(step_detail.get("stderr_tail") or "")[-1000:]
        chunks.append(f"target={target}\nstdout:\n{stdout}\nstderr:\n{stderr}")
    if chunks:
        return "\n\n".join(chunks)
    rendered = str(detail)
    return rendered[:3000] if rendered and rendered != "{}" else None
