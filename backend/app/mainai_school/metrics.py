"""Independence metrics — EXTERNAL DEPENDENCY RATIO by domain.

Evidence counters only; no self-congratulation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.mainai_school.types import IndependenceSnapshot


@dataclass
class DomainCounters:
    local_attempts: int = 0
    local_successes: int = 0
    teacher_helps: int = 0
    teacher_corrections: int = 0
    exams_taken: int = 0
    exams_passed: int = 0
    external_doer_uses: int = 0  # should trend down


# Process-local ledger keyed by owner+domain — never leak cross-owner metrics.
_COUNTERS: dict[tuple[str, str], DomainCounters] = {}


def reset_metrics_for_tests() -> None:
    _COUNTERS.clear()


def record_task_outcome(
    *,
    domain: str,
    local_attempted: bool,
    local_success: bool | None,
    teacher_helped: bool,
    teacher_corrected: bool,
    exam_taken: bool = False,
    exam_passed: bool = False,
    external_as_doer: bool = False,
    owner_id: str | None = None,
) -> IndependenceSnapshot:
    key = (str(owner_id or "_unset"), domain)
    c = _COUNTERS.setdefault(key, DomainCounters())
    if local_attempted:
        c.local_attempts += 1
        if local_success:
            c.local_successes += 1
    if teacher_helped:
        c.teacher_helps += 1
    if teacher_corrected:
        c.teacher_corrections += 1
    if exam_taken:
        c.exams_taken += 1
        if exam_passed:
            c.exams_passed += 1
    if external_as_doer:
        c.external_doer_uses += 1
    return snapshot_domain(domain, owner_id=owner_id)


def snapshot_domain(domain: str, *, owner_id: str | None = None) -> IndependenceSnapshot:
    key = (str(owner_id or "_unset"), domain)
    c = _COUNTERS.get(key, DomainCounters())
    attempts = max(1, c.local_attempts + c.external_doer_uses)
    dep = (c.teacher_helps + c.external_doer_uses) / attempts
    return IndependenceSnapshot(
        domain=domain,
        local_attempt_rate=c.local_attempts / attempts,
        local_success_rate=(c.local_successes / c.local_attempts) if c.local_attempts else 0.0,
        teacher_help_rate=c.teacher_helps / attempts,
        teacher_correction_rate=(c.teacher_corrections / c.teacher_helps) if c.teacher_helps else 0.0,
        exam_success_rate=(c.exams_passed / c.exams_taken) if c.exams_taken else 0.0,
        external_dependency_ratio=dep,
        evidence={
            "counters": c.__dict__.copy(),
            "owner_scoped": True,
            "owner_id": owner_id,
            "goal": "MINIMUM_EXTERNAL_DEPENDENCY_NECESSARY_FOR_QUALITY",
            "cheaper_is_not_better_if_wrong": True,
        },
    )


def all_domain_snapshots() -> dict[str, Any]:
    return {
        f"{oid}:{domain}": snapshot_domain(domain, owner_id=oid).__dict__
        for (oid, domain) in sorted(_COUNTERS)
    }
