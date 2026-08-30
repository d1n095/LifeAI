"""Executive look-around: active_context → lessons → bounded WorkCandidates.

Composes existing primitives. Does NOT authorize work.
PLAN != AUTHORITY.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.active_context.service import create_context_set, current_members, refresh_context
from app.mainai_execution.lessons import lookup_lessons
from app.mainai_executive.bounds import ExecutiveScanBounds
from app.mainai_executive.types import (
    EXECUTIVE_PRIORITY,
    HORIZON_TO_PRIORITY,
    HorizonItem,
    PlanningHorizon,
)
from app.models.active_context import ActiveContextSet
from app.work_candidates.service import record_work_candidate


def _stable_candidate_key(*, owner_id: uuid.UUID, horizon: str, title: str) -> str:
    """Owner+horizon+title — avoids minting duplicate candidates across sessions."""
    digest = hashlib.sha256(f"{owner_id}:{horizon}:{title}".encode()).hexdigest()[:24]
    return f"exec-scan:{digest}"


@dataclass
class LookaroundResult:
    context_set_id: uuid.UUID
    member_count: int
    lesson_ids: list[uuid.UUID]
    work_candidate_ids: list[uuid.UUID]
    horizon_items: list[HorizonItem]
    scan_bound_reached: bool
    lesson_tags: list[str] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


def _derive_lesson_tags(*, founder_request: str, member_kinds: list[str]) -> list[str]:
    tags = set(member_kinds)
    # Lightweight keyword tags — deterministic, not LLM. Expands lookaround recall.
    lower = founder_request.lower()
    for needle, tag in (
        ("auth", "auth"),
        ("memory", "memory"),
        ("workforce", "workforce"),
        ("provider", "provider"),
        ("restart", "recovery"),
        ("recover", "recovery"),
        ("plan", "planning"),
        ("test", "testing"),
        ("security", "security"),
        ("rls", "security"),
    ):
        if needle in lower:
            tags.add(tag)
    tags.add("executive_scan")
    return sorted(tags)


def _build_horizons(
    *,
    founder_request: str,
    lesson_ids: list[uuid.UUID],
    member_count: int,
    bounds: ExecutiveScanBounds,
) -> list[HorizonItem]:
    """Long-horizon plan slots. FUTURE PLAN != AUTHORITY — authorized always False."""
    items: list[HorizonItem] = [
        HorizonItem(
            horizon=PlanningHorizon.NOW,
            title=f"Resolve current ask: {founder_request[:120]}",
            rationale="Directly required for the triggering instruction",
            confidence=0.7,
            provenance={"step": "NOW", "authorized": False},
        ),
        HorizonItem(
            horizon=PlanningHorizon.NEAR,
            title="Inspect adjacent modules and unfinished work",
            rationale=f"Look-around found {member_count} active-context members",
            confidence=0.6,
            provenance={"step": "NEAR", "member_count": member_count},
        ),
        HorizonItem(
            horizon=PlanningHorizon.MID,
            title="Integrate verification, memory update, and lesson extraction",
            rationale="VERIFY → STORE → LEARN before claiming completion",
            confidence=0.55,
            dependencies=["NEAR"],
            provenance={"step": "MID"},
        ),
        HorizonItem(
            horizon=PlanningHorizon.LONG,
            title="Replan and continue across restarts",
            rationale="Durable continuity checkpoint; no hallucinated continuation",
            confidence=0.5,
            dependencies=["MID"],
            provenance={"step": "LONG"},
        ),
    ]
    for lid in lesson_ids[:3]:
        items.append(
            HorizonItem(
                horizon=PlanningHorizon.NEAR,
                title=f"Apply prior lesson {lid}",
                rationale="History scan surfaced an active engineering lesson",
                confidence=0.65,
                provenance={"lesson_id": str(lid), "authorized": False},
            )
        )
    return items[: bounds.max_horizon_items]


def run_executive_lookaround(
    db: Session,
    *,
    owner_id: uuid.UUID,
    founder_request: str,
    session_id: str,
    source_entity_id: uuid.UUID | None = None,
    anchor_type: str = "explicit_topic",
    anchor_ref: str | None = None,
    bounds: ExecutiveScanBounds | None = None,
) -> LookaroundResult:
    bounds = bounds or ExecutiveScanBounds()
    started = time.monotonic()
    topic = (anchor_ref or founder_request[:200] or "executive-topic").strip()
    ctx_key = f"exec-ctx:{session_id}"
    existing_ctx = db.execute(
        select(ActiveContextSet).where(
            ActiveContextSet.owner_id == owner_id,
            ActiveContextSet.idempotency_key == ctx_key,
        )
    ).scalar_one_or_none()
    if existing_ctx is not None:
        # Continuity: same session keeps the original context set even if the
        # founder restates the ask (correction / resume). Do not fight idempotency.
        context = existing_ctx
    else:
        context = create_context_set(
            db,
            owner_id=owner_id,
            anchor_type=anchor_type,
            anchor_ref=topic if anchor_type == "explicit_topic" else str(anchor_ref or topic),
            idempotency_key=ctx_key,
            label=f"executive:{session_id[:12]}",
            subject_basis="manual" if anchor_type == "explicit_topic" else "unknown",
        )
    members = refresh_context(
        db,
        owner_id=owner_id,
        context_set_id=context.id,
        max_depth=bounds.max_scan_depth,
        max_members=100,
    )
    # Prefer current_members for stable read after refresh.
    live = current_members(db, owner_id=owner_id, context_set_id=context.id)
    member_kinds = sorted({m.object_type for m in live})
    tags = _derive_lesson_tags(founder_request=founder_request, member_kinds=member_kinds)
    lessons = lookup_lessons(db, applies_to_any=tags)
    lesson_ids = [lesson.id for lesson in lessons]

    horizon_items = _build_horizons(
        founder_request=founder_request,
        lesson_ids=lesson_ids,
        member_count=len(live) if live else len(members),
        bounds=bounds,
    )

    work_candidate_ids: list[uuid.UUID] = []
    scan_bound_reached = False
    if source_entity_id is not None:
        for item in horizon_items:
            if time.monotonic() - started > bounds.max_elapsed_seconds:
                scan_bound_reached = True
                break
            if len(work_candidate_ids) >= bounds.max_candidates_per_scan:
                scan_bound_reached = True
                break
            priority = HORIZON_TO_PRIORITY[item.horizon]
            assert priority in EXECUTIVE_PRIORITY
            cand = record_work_candidate(
                db,
                owner_id=owner_id,
                source_entity_id=source_entity_id,
                title=item.title[:200],
                rationale=item.rationale,
                idempotency_key=_stable_candidate_key(
                    owner_id=owner_id,
                    horizon=item.horizon.value,
                    title=item.title[:200],
                ),
                priority=priority,
                classifier_strategy="executive_lookaround_v1",
                classifier_confidence=item.confidence,
                dependencies=list(item.dependencies),
                provenance={
                    "horizon": item.horizon.value,
                    "authorized": False,
                    "future_plan_is_not_authority": True,
                    "scan_step": "bounded_candidate_generation",
                    "classifier_strategy": "executive_lookaround_v1",
                    **{k: v for k, v in dict(item.provenance).items() if k != "executive_session_id"},
                },
            )
            work_candidate_ids.append(cand.id)

        if scan_bound_reached and source_entity_id is not None:
            # Evidence preserved even when generation stops — never silently truncate.
            bound_cand = record_work_candidate(
                db,
                owner_id=owner_id,
                source_entity_id=source_entity_id,
                title="[executive scan] bound reached before full confidence",
                rationale="ExecutiveScanBounds stopped candidate generation; remaining items retained in continuity only",
                idempotency_key=_stable_candidate_key(
                    owner_id=owner_id,
                    horizon="OPTIONAL",
                    title="[executive scan] bound reached before full confidence",
                ),
                priority="OPTIONAL",
                classifier_strategy="executive_lookaround_v1",
                classifier_confidence=0.2,
                provenance={
                    "scan_bound_reached": True,
                    "authorized": False,
                    "future_plan_is_not_authority": True,
                },
            )
            work_candidate_ids.append(bound_cand.id)

    return LookaroundResult(
        context_set_id=context.id,
        member_count=len(live) if live else len(members),
        lesson_ids=lesson_ids,
        work_candidate_ids=work_candidate_ids,
        horizon_items=horizon_items,
        scan_bound_reached=scan_bound_reached,
        lesson_tags=tags,
        provenance={"session_id": session_id, "anchor_type": anchor_type},
    )
