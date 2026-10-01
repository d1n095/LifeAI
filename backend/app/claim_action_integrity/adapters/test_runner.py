"""Structured test-run result ingestion. Log text is deliberately not accepted."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.claim_action_integrity.adapters.base import ingest_observation
from app.claim_action_integrity.adapters.github import _bindings
from app.claim_action_integrity.adapters.types import AdapterResult, EvidenceContext, FactMutability, ObservationStatus, ProviderObservation


@dataclass(frozen=True)
class TestRunResult:
    __test__ = False

    execution_id: str
    checkout_sha: str
    tree_hash: str | None
    command: tuple[str, ...]
    selection: tuple[str, ...]
    environment: dict[str, str]
    started_at: datetime
    completed_at: datetime | None
    passed: int
    failed: int
    skipped: int
    exit_code: int | None
    runner_result_id: str


class TestRunnerEvidenceAdapter:
    def ingest_result(self, db: Session, context: EvidenceContext, result: TestRunResult) -> AdapterResult:
        if result.execution_id != context.execution_id:
            return AdapterResult(ObservationStatus.invalid_evidence, reason="test_execution_id_mismatch")
        if result.completed_at is None or result.exit_code is None:
            return AdapterResult(ObservationStatus.unknown, reason="test_run_not_completed")
        if result.completed_at < result.started_at or min(result.passed, result.failed, result.skipped) < 0:
            return AdapterResult(ObservationStatus.invalid_evidence, reason="invalid_test_result_counts_or_time")
        status = "passed" if result.exit_code == 0 and result.failed == 0 else "failed"
        facts = {
            "status": status,
            "passed": result.passed,
            "failed": result.failed,
            "skipped": result.skipped,
            "exit_code": result.exit_code,
            "command": list(result.command),
            "test_selection": list(result.selection),
            "environment": result.environment,
            "started_at": result.started_at.isoformat(),
            "completed_at": result.completed_at.isoformat(),
            "execution_id": result.execution_id,
            "artifact_sha": result.checkout_sha,
        }
        bindings = _bindings(context, sha=result.checkout_sha, tree_hash=result.tree_hash or "")
        observation = ProviderObservation("test_runner", f"test-runner:{result.runner_result_id}", result.runner_result_id, FactMutability.immutable_fact, bindings, facts, result.completed_at)
        return ingest_observation(db, context, observation)
