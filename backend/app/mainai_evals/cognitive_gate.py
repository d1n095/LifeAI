"""Round 10 M3: the COGNITIVE REGRESSION GATE — a reproducible behavioral evaluation
framework measuring MainAI's actual reasoning capabilities across model versions.

Design contract (mission M3):
  - FIXED, VERSIONED cases (evals/cognitive_gate/cases_v1.json) executed through
    PRODUCTION call paths (real routers, real PostgreSQL/Redis, real middleware) —
    never through isolated helpers.
  - Objective assertions are AUTO-ENFORCED: a deterministic case that fails is a
    capability regression, full stop.
  - Rubric-scored dimensions (judgment unavoidably required) are RECORDED with the run
    and explicitly EXCLUDED from the automated verdict — subjective quality cannot gate
    a deployment; a human or the authorized operational system reviews them.
  - compare_gate_runs() implements the repository-side comparison + policy interface:
    an objective regression on a CRITICAL capability produces
    verdict="blocked_requires_approval". This module NEVER switches an active model —
    the provider registry (app/providers/registry.py) is untouched; deployment
    enforcement and final approval belong to the authorized operational system
    (Freebuff's lane). The gate makes regressions VISIBLE and BINDING AS EVIDENCE.

Deterministic cases run with simulated providers (the established test pattern: real
PG/Redis, real routers, captured system prompt). Model cases additionally need a
configured provider; without one they report skipped_no_model honestly instead of
fabricating results.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

DATASET_DIR = Path(__file__).resolve().parents[3] / "evals" / "cognitive_gate"
CURRENT_DATASET_VERSION = "cognitive_gate_v1"

# The capabilities whose objective regression blocks a candidate model from approval.
# Judgment-only dimensions are NEVER in this set by construction (they live outside
# objective verdicts entirely).
CRITICAL_CAPABILITIES = frozenset(
    {
        "injection_resistance",
        "citation_integrity",
        "uncertainty_calibration",
        "contradiction_detection",
        "owner_boundary_preservation",
        "causal_reasoning",
        "methodological_criticism",
        "decision_recall",
        "long_context_continuity",
    }
)


@dataclass
class ObjectiveAssertion:
    assertion: str
    detail: str
    passed: bool | None = None  # None = not executed (skipped)


@dataclass
class RubricScore:
    name: str
    scale: str
    value: str | int | None = None  # recorded only when a real model ran the case


@dataclass
class CaseResult:
    case_id: str
    capability: str
    execution: str  # deterministic | model
    status: str  # passed | failed | skipped_no_model
    objective: list[ObjectiveAssertion] = field(default_factory=list)
    rubric: list[RubricScore] = field(default_factory=list)
    notes: str = ""

    @property
    def objective_failed(self) -> bool:
        return any(a.passed is False for a in self.objective)


@dataclass
class GateRun:
    run_id: uuid.UUID
    dataset_version: str
    provider: str
    model: str
    executed_at: datetime
    case_results: list[CaseResult]

    @property
    def objective_passed(self) -> bool:
        return all(not r.objective_failed for r in self.case_results)

    def failed_critical_capabilities(self) -> set[str]:
        return {
            r.capability
            for r in self.case_results
            if r.objective_failed and r.capability in CRITICAL_CAPABILITIES
        }


def load_dataset(version: str = CURRENT_DATASET_VERSION) -> dict[str, Any]:
    """Load and validate a pinned dataset version. Version mismatch is an ERROR, not a
    silent fallback: a gate run against an unexpected dataset is not evidence."""
    path = DATASET_DIR / f"cases_{version.split('_', 2)[2]}.json"
    if version == CURRENT_DATASET_VERSION:
        path = DATASET_DIR / "cases_v1.json"
    if not path.exists():
        raise FileNotFoundError(f"cognitive gate dataset {version} not found at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("dataset_version") != version:
        raise ValueError(
            f"dataset file {path.name} declares version {data.get('dataset_version')!r}, "
            f"expected {version!r} — refusing to run a mislabeled dataset"
        )
    declared = data.get("capabilities") or {}
    covered = {c.get("capability") for c in data.get("cases") or []}
    missing = sorted(set(declared) - covered)
    if missing:
        raise ValueError(f"dataset capabilities without cases: {missing} — coverage must be total")
    return data


def compare_gate_runs(baseline: GateRun, candidate: GateRun) -> dict[str, Any]:
    """The repository-side comparison + policy interface. Policy: ANY objective failure
    on a CRITICAL capability in the candidate -> verdict='blocked_requires_approval'.
    Rubric deltas are reported as informational ONLY — subjective quality drift never
    produces a block by itself (and never a pass either). This function writes nothing
    and switches nothing: it returns evidence the authorized operational system acts on."""
    base_failed = baseline.failed_critical_capabilities()
    cand_failed = candidate.failed_critical_capabilities()
    regressed = sorted(cand_failed - base_failed)
    still_failing = sorted(cand_failed & base_failed)
    recovered = sorted(base_failed - cand_failed)

    rubric_info: list[dict[str, Any]] = []
    base_by_id = {r.case_id: r for r in baseline.case_results}
    for cand_case in candidate.case_results:
        base_case = base_by_id.get(cand_case.case_id)
        if not base_case:
            continue
        cand_scores = {s.name: s.value for s in cand_case.rubric if s.value is not None}
        base_scores = {s.name: s.value for s in base_case.rubric if s.value is not None}
        for name, value in cand_scores.items():
            if value is None:
                continue
            base_value = base_scores.get(name)
            if base_value != value:
                rubric_info.append(
                    {"case_id": cand_case.case_id, "dimension": name, "baseline": base_value, "candidate": value}
                )

    verdict = "blocked_requires_approval" if (regressed or still_failing) else "objective_gate_passed"
    return {
        "verdict": verdict,
        "critical_regressions": regressed,
        "critical_still_failing": still_failing,
        "critical_recovered": recovered,
        "rubric_deltas_informational": rubric_info,
        "baseline_run_id": str(baseline.run_id),
        "candidate_run_id": str(candidate.run_id),
        "policy": (
            "objective regression on critical capability — approval required; the gate "
            "does not switch models and does not deploy: the authorized operational "
            "system (Freebuff lane) owns enforcement"
        ),
    }


def record_run(db: Session, run: GateRun) -> uuid.UUID:
    """Persist a gate run + per-case results for cross-model comparison over time.
    Model-generated rubric values are stored in the SAME row as the objective outcomes
    but in a clearly separated column set — separation of deterministic failures from
    subjective measurements is preserved at the storage layer too."""
    from app.models.cognitive_gate import CognitiveGateCaseResult, CognitiveGateRun

    row = CognitiveGateRun(
        id=run.run_id, dataset_version=run.dataset_version, provider=run.provider,
        model=run.model, executed_at=run.executed_at, objective_passed=run.objective_passed,
    )
    db.add(row)
    db.flush()  # the run row must exist before the case rows reference it
    for case in run.case_results:
        db.add(
            CognitiveGateCaseResult(
                run_id=run.run_id, case_id=case.case_id, capability=case.capability,
                execution=case.execution, status=case.status,
                objective_json=[
                    {"assertion": a.assertion, "detail": a.detail, "passed": a.passed}
                    for a in case.objective
                ],
                rubric_json=[
                    {"name": s.name, "scale": s.scale, "value": s.value} for s in case.rubric
                ],
                notes=case.notes,
            )
        )
    db.commit()
    return run.run_id


def load_run(db: Session, run_id: uuid.UUID) -> GateRun:
    from app.models.cognitive_gate import CognitiveGateCaseResult, CognitiveGateRun

    row = db.query(CognitiveGateRun).filter(CognitiveGateRun.id == run_id).one()
    cases = db.query(CognitiveGateCaseResult).filter(CognitiveGateCaseResult.run_id == run_id).all()
    results = [
        CaseResult(
            case_id=c.case_id, capability=c.capability, execution=c.execution, status=c.status,
            objective=[ObjectiveAssertion(**o) for o in (c.objective_json or [])],
            rubric=[RubricScore(**r) for r in (c.rubric_json or [])], notes=c.notes or "",
        )
        for c in cases
    ]
    return GateRun(
        run_id=row.id, dataset_version=row.dataset_version, provider=row.provider,
        model=row.model, executed_at=row.executed_at, case_results=results,
    )
