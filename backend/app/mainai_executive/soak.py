"""Authentic executive soak — labels reflect ACTUAL cycle counts only."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.mainai_executive.loop import run_executive_cycle
from app.work_candidates import list_work_candidates


@dataclass
class SoakReport:
    requested_cycles: int
    actual_cycles: int
    started_at: float
    ended_at: float
    unique_session_ids: int
    unique_work_candidate_ids: int
    candidate_count_start: int
    candidate_count_end: int
    duplicate_rate: float
    replan_signals: int
    authority_violations: int
    provider_invokes: int
    label: str
    healthy: bool
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_cycles": self.requested_cycles,
            "actual_cycles": self.actual_cycles,
            "elapsed_seconds": round(self.ended_at - self.started_at, 3),
            "unique_session_ids": self.unique_session_ids,
            "unique_work_candidate_ids": self.unique_work_candidate_ids,
            "candidate_count_start": self.candidate_count_start,
            "candidate_count_end": self.candidate_count_end,
            "candidate_growth": self.candidate_count_end - self.candidate_count_start,
            "duplicate_rate": self.duplicate_rate,
            "replan_signals": self.replan_signals,
            "authority_violations": self.authority_violations,
            "provider_invokes": self.provider_invokes,
            "label": self.label,
            "healthy": self.healthy,
            "notes": list(self.notes),
            # Hard rule: label never exceeds actual_cycles.
            "label_is_honest": self.label.startswith(f"{self.actual_cycles}-cycle")
            or self.label == f"{self.actual_cycles}_cycles_authenticated",
        }


def _honest_label(actual: int) -> str:
    return f"{actual}_cycles_authenticated"


def run_executive_soak(
    db: Session,
    *,
    owner_id: uuid.UUID,
    source_entity_id: uuid.UUID,
    cycles: int,
    shared_session: bool = True,
    run_workforce_dry: bool = False,
) -> SoakReport:
    """Execute exactly `cycles` executive cycles and record evidence.

    Never returns a label claiming more cycles than actually ran.
    """
    if cycles < 1 or cycles > 5000:
        raise ValueError("cycles out of supported soak bounds (1..5000)")

    started = time.time()
    before = list_work_candidates(db, owner_id=owner_id)
    start_count = len(before)
    session_ids: set[str] = set()
    candidate_ids: set[str] = set()
    seen_titles: dict[str, int] = {}
    replan = 0
    auth_viol = 0
    provider = 0
    actual = 0
    base_session = f"soak-{uuid.uuid4()}"

    for i in range(cycles):
        sid = base_session if shared_session else f"{base_session}-{i}"
        session_ids.add(sid)
        result = run_executive_cycle(
            db,
            owner_id=owner_id,
            founder_request=f"soak cycle {i}: continue composed executive work",
            session_id=sid,
            source_entity_id=source_entity_id,
            run_workforce_dry=run_workforce_dry,
        )
        actual += 1
        for cid in result.work_candidate_ids:
            candidate_ids.add(str(cid))
        for h in result.horizon_items:
            seen_titles[h.title] = seen_titles.get(h.title, 0) + 1
            if h.authorized:
                auth_viol += 1
        if any("replan" in u.lower() or "assumption" in u for u in (result.observability.get("last_recovery") or {}).get("uncertain", []) if isinstance(u, str)):
            replan += 1
        # uncertain list from checkpoint via observability open risks / assumption scan
        scan = (result.observability or {}).get("assumption_scan") or {}
        if scan.get("assumption_invalidation_requires_replan"):
            replan += 1
        if result.workforce_dry_run and result.workforce_dry_run.get("provider_invoked"):
            provider += 1

    after = list_work_candidates(db, owner_id=owner_id)
    end_count = len(after)
    dup_hits = sum(1 for n in seen_titles.values() if n > 1)
    dup_rate = (dup_hits / max(1, len(seen_titles))) if seen_titles else 0.0
    ended = time.time()
    healthy = auth_viol == 0 and provider == 0 and actual == cycles
    notes: list[str] = []
    if not healthy:
        notes.append("unhealthy_soak")
    if end_count - start_count > cycles * 2:
        notes.append("candidate_growth_high")
        healthy = False

    return SoakReport(
        requested_cycles=cycles,
        actual_cycles=actual,
        started_at=started,
        ended_at=ended,
        unique_session_ids=len(session_ids),
        unique_work_candidate_ids=len(candidate_ids),
        candidate_count_start=start_count,
        candidate_count_end=end_count,
        duplicate_rate=dup_rate,
        replan_signals=replan,
        authority_violations=auth_viol,
        provider_invokes=provider,
        label=_honest_label(actual),
        healthy=healthy,
        notes=notes,
    )
