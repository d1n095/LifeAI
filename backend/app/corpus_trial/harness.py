"""Runs `fixtures.CORPUS` through the REAL `app.founder_memory`/`app.diagnosis`/`app.
problem_learning` recording APIs (never a mock, never a direct DB write) and scores the
result against `scoring.py`'s structural invariants. See docs/LIFE_CORPUS_TRIAL_HARNESS.md
for the full rationale -- this is the minimal evaluation harness foundation for a later, real,
controlled mixed-corpus trial; it does NOT ingest the founder's actual corpus."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.corpus_trial.fixtures import CORPUS, CorpusItem
from app.corpus_trial.scoring import SCORERS, RecordSnapshot, score_epistemic_distinction
from app.diagnosis import (
    get_diagnosis,
    list_current_diagnoses,
    prove_diagnosis_cause,
    record_diagnosis,
    rule_out_diagnosis,
)
from app.founder_memory import (
    get_founder_memory,
    list_founder_memory,
    mark_founder_memory_disputed,
    record_founder_memory,
)
from app.intelligence_governance import record_evidence, record_execution
from app.models.problem_learning import LifeProblemDecision
from app.problem_learning.service import create_problem, record_decision

_TEXT_FIELD = {"founder_memory": "content", "diagnosis": "observation", "problem_learning": "decision"}


@dataclass
class TrialReport:
    dimension_violations: dict[str, list[str]]
    record_count: int

    @property
    def passed(self) -> bool:
        return all(not v for v in self.dimension_violations.values())

    @property
    def summary(self) -> dict[str, str]:
        return {dim: ("PASS" if not v else f"FAIL ({len(v)})") for dim, v in self.dimension_violations.items()}


def _real_evidence(db: Session, owner_id: uuid.UUID, task_id: uuid.UUID):
    execution = record_execution(db, owner_id=owner_id, task_id=task_id, idempotency_key=f"corpus-trial-exec-{uuid.uuid4()}", provider="internal")
    return record_evidence(
        db, owner_id=owner_id, execution_id=execution.id, evidence_kind="ci_log", review_kind="deterministic_tool",
        deterministic=True, payload={"conclusion": "external_503"}, source_type="ci_log", source_ref="corpus-trial-fixture",
        idempotency_key=f"corpus-trial-ev-{uuid.uuid4()}",
    )


def run_trial(db: Session, *, owner_id: uuid.UUID, evidence_task_id: uuid.UUID, corpus: list[CorpusItem] | None = None) -> TrialReport:
    """Replays `corpus` (default: the bundled bootstrap fixtures) against the real recording
    APIs for a single owner, then scores every dimension in `scoring.SCORERS` plus epistemic
    distinction. `evidence_task_id` must be an existing, owned `MainAITask.id` -- the harness
    creates its own real `intelligence_evidence` row against it to ground any `prove` actions,
    it never fabricates evidence."""

    items = CORPUS if corpus is None else corpus
    rows_by_key: dict[str, object] = {}
    supersedes_by_key: dict[str, str | None] = {}
    expect_contradicted: dict[str, bool] = {}
    systems_by_key: dict[str, str] = {}
    confidence_submitted_by_key: dict[str, float | None] = {}
    problem_id_by_key: dict[str, uuid.UUID] = {}
    evidence = None

    for item in items:
        systems_by_key[item.key] = item.system
        if item.action == "record":
            if item.system == "founder_memory":
                row = record_founder_memory(db, owner_id=owner_id, idempotency_key=f"corpus-trial:{item.key}", **item.kwargs)
            elif item.system == "diagnosis":
                row = record_diagnosis(db, owner_id=owner_id, idempotency_key=f"corpus-trial:{item.key}", **item.kwargs)
            else:
                decision_kwargs = {k: v for k, v in item.kwargs.items() if k not in ("problem_title", "problem_description")}
                problem = create_problem(
                    db, owner_id=owner_id, title=item.kwargs["problem_title"], description=item.kwargs["problem_description"],
                    idempotency_key=f"corpus-trial:{item.key}:problem",
                )
                problem_id_by_key[item.key] = problem.id
                row = record_decision(db, owner_id=owner_id, problem_id=problem.id, idempotency_key=f"corpus-trial:{item.key}", **decision_kwargs)
            rows_by_key[item.key] = row
            supersedes_by_key[item.key] = None
            confidence_submitted_by_key[item.key] = item.kwargs.get("confidence")
        elif item.action == "supersede":
            old = rows_by_key[item.supersedes_key]
            if item.system == "founder_memory":
                row = record_founder_memory(db, owner_id=owner_id, idempotency_key=f"corpus-trial:{item.key}", supersedes_note_id=old.id, **item.kwargs)
            elif item.system == "diagnosis":
                row = record_diagnosis(db, owner_id=owner_id, idempotency_key=f"corpus-trial:{item.key}", supersedes_diagnosis_id=old.id, **item.kwargs)
            else:
                problem_id_by_key[item.key] = problem_id_by_key[item.supersedes_key]
                row = record_decision(
                    db, owner_id=owner_id, problem_id=problem_id_by_key[item.key], idempotency_key=f"corpus-trial:{item.key}",
                    supersedes_decision_id=old.id, **item.kwargs,
                )
            rows_by_key[item.key] = row
            supersedes_by_key[item.key] = item.supersedes_key
            confidence_submitted_by_key[item.key] = item.kwargs.get("confidence")
        elif item.action == "dispute":
            row = mark_founder_memory_disputed(db, owner_id=owner_id, note_id=rows_by_key[item.key].id)
            rows_by_key[item.key] = row
        elif item.action == "rule_out":
            row = rule_out_diagnosis(db, owner_id=owner_id, diagnosis_id=rows_by_key[item.key].id)
            rows_by_key[item.key] = row
        elif item.action == "prove":
            if evidence is None:
                evidence = _real_evidence(db, owner_id, evidence_task_id)
            row = prove_diagnosis_cause(db, owner_id=owner_id, diagnosis_id=rows_by_key[item.key].id, evidence_id=evidence.id, confidence=0.9)
            rows_by_key[item.key] = row
            confidence_submitted_by_key[item.key] = 0.9
        else:  # pragma: no cover -- fixtures are static and validated by this exhaustive match
            raise ValueError(f"unknown corpus action: {item.action}")
        expect_contradicted[item.key] = expect_contradicted.get(item.key, False) or item.expect_contradicted

    current_founder_ids = {row.id for row in list_founder_memory(db, owner_id=owner_id, status="active")}
    current_diagnosis_ids = {row.id for row in list_current_diagnoses(db, owner_id=owner_id)}

    snapshots: list[RecordSnapshot] = []
    for item in items:
        if item.action not in ("record", "supersede"):
            continue  # only originating actions define a distinct record; mutations reuse the same key
        key = item.key
        system = systems_by_key[key]
        text_field = _TEXT_FIELD[system]
        submitted_text = item.kwargs.get(text_field, getattr(rows_by_key[key], text_field))
        has_confidence = True
        if system == "founder_memory":
            fresh = get_founder_memory(db, owner_id=owner_id, note_id=rows_by_key[key].id)
            is_current = fresh.id in current_founder_ids
            is_contradicted = fresh.status == "disputed"
        elif system == "diagnosis":
            fresh = get_diagnosis(db, owner_id=owner_id, diagnosis_id=rows_by_key[key].id)
            is_current = fresh.id in current_diagnosis_ids
            is_contradicted = fresh.epistemic_stage == "ruled_out"
        else:
            fresh = db.execute(select(LifeProblemDecision).where(
                LifeProblemDecision.id == rows_by_key[key].id, LifeProblemDecision.owner_id == owner_id)).scalar_one()
            is_current = fresh.status == "active"
            is_contradicted = False  # no in-place "mark contradicted without a replacement" path exists for this system
            has_confidence = False  # LifeProblemDecision has no confidence column

        old_key = supersedes_by_key.get(key)
        old_id = str(rows_by_key[old_key].id) if old_key else None
        read_back_text = getattr(fresh, text_field)

        snapshots.append(
            RecordSnapshot(
                system=system, record_id=str(fresh.id), text_submitted=submitted_text, text_read_back=read_back_text,
                authority_submitted=item.kwargs.get("authority", "unknown"), authority_read_back=fresh.authority,
                basis_submitted=item.kwargs.get("basis", "unknown"), basis_read_back=fresh.basis,
                confidence_submitted=confidence_submitted_by_key[key] if has_confidence else None,
                confidence_read_back=(float(fresh.confidence) if has_confidence and fresh.confidence is not None else None),
                supersedes_id=old_id, is_current=is_current, is_contradicted=is_contradicted,
                contradiction_text_read_back=read_back_text, contradiction_excludes_currency=(system != "diagnosis"),
                extra={"expected_contradicted": expect_contradicted.get(key, False)},
            )
        )

    dimension_violations = {dim: scorer(snapshots) for dim, scorer in SCORERS.items()}
    expected_authorities = {s.authority_submitted for s in snapshots}
    dimension_violations["epistemic_distinction"] = score_epistemic_distinction(snapshots, expected_authorities)

    return TrialReport(dimension_violations=dimension_violations, record_count=len(snapshots))
