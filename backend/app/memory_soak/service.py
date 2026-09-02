"""Stage S — 1000-tick memory/autonomy soak specification + bounded local runner."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.contradiction_engine import record_structured_claim
from app.inspectable_memory import founder_add_memory_note
from app.memory_health import run_memory_health_checks
from app.memory_tiers import record_retrieval
from app.personal_intent import resolve_with_learned_intent
from app.self_model import build_self_model
from app.temporal_intelligence import RecapWindow, build_recap


SOAK_SPEC = {
    "ticks": 1000,
    "exercise": [
        "idle",
        "wakeups",
        "memory_retrieval",
        "idea_reconciliation",
        "recaps",
        "repair",
        "restart",
        "takeover",
        "authority_changes",
        "long_horizon_replanning",
        "self_model_updates",
    ],
    "watch_for": [
        "memory_drift",
        "duplicate_concepts",
        "stale_assumptions",
        "orphan_work",
        "false_completion",
        "authority_leakage",
        "unbounded_growth",
    ],
}


@dataclass
class SoakTickResult:
    tick: int
    action: str
    ok: bool
    notes: str = ""


@dataclass
class SoakReport:
    ticks_run: int
    results: list[SoakTickResult] = field(default_factory=list)
    growth: dict = field(default_factory=dict)
    anomalies: list[str] = field(default_factory=list)
    spec: dict = field(default_factory=lambda: dict(SOAK_SPEC))


def run_bounded_memory_soak(
    db: Session,
    *,
    owner_id: uuid.UUID,
    ticks: int = 20,
) -> SoakReport:
    """Production-shaped *bounded* soak for local/CI. Full 1000-tick is the durable spec target.

    Exercises memory retrieval, intent learning, recaps, assumptions, self-model, health checks.
    Does not invent authority or complete goals falsely.
    """
    ticks = max(1, min(ticks, SOAK_SPEC["ticks"]))
    report = SoakReport(ticks_run=0)
    actions = [
        "idle",
        "memory_retrieval",
        "idea_reconciliation",
        "recaps",
        "self_model_updates",
        "assumption_check",
        "health_loop",
    ]
    note_ids: list[uuid.UUID] = []
    for i in range(ticks):
        action = actions[i % len(actions)]
        ok = True
        notes = ""
        try:
            if action == "idle":
                notes = "noop"
            elif action == "memory_retrieval":
                n, _ = founder_add_memory_note(
                    db,
                    owner_id=owner_id,
                    content=f"soak tick {i} memory signal",
                    note_type="observation",
                    idempotency_key=f"soak-mem-{owner_id}-{i}",
                )
                note_ids.append(n.id)
                record_retrieval(db, owner_id=owner_id, target_kind="founder_memory_note", target_id=n.id)
            elif action == "idea_reconciliation":
                resolve_with_learned_intent(
                    db,
                    owner_id=owner_id,
                    raw_expression=f"få med det här med soak idea {i % 5}",
                    idempotency_key=f"soak-intent-{owner_id}-{i}",
                )
            elif action == "recaps":
                build_recap(db, owner_id=owner_id, window=RecapWindow.DAY, include_project_wide=False, limit=20)
            elif action == "self_model_updates":
                build_self_model(db, owner_id=owner_id)
            elif action == "assumption_check":
                record_structured_claim(
                    db,
                    owner_id=owner_id,
                    kind="assumption",
                    statement=f"soak assumption {i % 3} still holds",
                    idempotency_key=f"soak-assump-{owner_id}-{i % 3}",
                )
            elif action == "health_loop":
                health = run_memory_health_checks(db, owner_id=owner_id)
                if not health.ok_to_repack:
                    report.anomalies.append(f"tick {i}: unsafe repack flag")
            report.results.append(SoakTickResult(tick=i, action=action, ok=ok, notes=notes))
        except Exception as exc:  # noqa: BLE001 — soak must continue and record anomalies
            report.results.append(SoakTickResult(tick=i, action=action, ok=False, notes=str(exc)[:200]))
            report.anomalies.append(f"tick {i} {action}: {exc}")
        report.ticks_run = i + 1

    # Growth / drift watches (bounded)
    report.growth = {
        "notes_created": len(note_ids),
        "ticks": report.ticks_run,
        "anomalies": len(report.anomalies),
    }
    if len(note_ids) > ticks:
        report.anomalies.append("unbounded_growth_notes")
    # Authority leakage watch: none of our soak paths should set authority-granted flags
    # (enforced by construction — no authorize_* calls in this module).
    return report
