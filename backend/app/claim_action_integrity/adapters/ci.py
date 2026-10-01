"""GitHub Actions/CI aggregate observation with exact run and head-SHA binding."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy.orm import Session

from app.claim_action_integrity.adapters.base import MalformedProviderResponse, ingest_observation, provider_failure
from app.claim_action_integrity.adapters.github import _bindings
from app.claim_action_integrity.adapters.types import EvidenceContext, FactMutability, ProviderObservation


class CIReader(Protocol):
    def get_workflow_run(self, owner: str, repo: str, run_id: int) -> tuple[dict[str, Any], str]: ...
    def get_workflow_jobs(self, owner: str, repo: str, run_id: int) -> tuple[list[dict[str, Any]], str]: ...


class GitHubActionsEvidenceAdapter:
    def __init__(self, reader: CIReader, *, mutable_max_age_seconds: int = 300):
        self.reader = reader
        self.mutable_max_age_seconds = mutable_max_age_seconds

    def observe_run(self, db: Session, context: EvidenceContext, *, owner: str, repo: str, run_id: int, expected_sha: str):
        try:
            run, run_response_id = self.reader.get_workflow_run(owner, repo, run_id)
            jobs, jobs_response_id = self.reader.get_workflow_jobs(owner, repo, run_id)
            if run.get("id") != run_id or run.get("head_sha") != expected_sha:
                raise MalformedProviderResponse("workflow run SHA or identity mismatch")
            if any(job.get("run_id", run_id) != run_id for job in jobs):
                raise MalformedProviderResponse("workflow job belongs to another run")
            status = run.get("status")
            conclusion = run.get("conclusion")
            all_jobs_complete = bool(jobs) and all(job.get("status") == "completed" for job in jobs)
            all_jobs_success = all_jobs_complete and all(job.get("conclusion") in {"success", "skipped", "neutral"} for job in jobs)
            required_ids = set(run.get("required_job_ids") or [])
            known_ids = {job.get("id") for job in jobs}
            required_gate_known = bool(required_ids) and required_ids.issubset(known_ids)
            passed = status == "completed" and conclusion == "success" and all_jobs_success
            if required_ids:
                passed = passed and required_gate_known and all(
                    job.get("conclusion") == "success" for job in jobs if job.get("id") in required_ids
                )
            failed_jobs = sum(job.get("conclusion") == "failure" for job in jobs)
            normalized_status = "passed" if passed else (
                "failed" if status == "completed" and (conclusion == "failure" or failed_jobs) else status
            )
            facts = {
                "status": normalized_status,
                "conclusion": conclusion,
                "passed": len(jobs) if passed else 0,
                "failed": failed_jobs,
                "skipped": sum(job.get("conclusion") == "skipped" for job in jobs),
                "command": f"github-actions workflow {run.get('workflow_id')}",
                "environment": {"provider": "github_actions"},
                "execution_id": context.execution_id,
                "artifact_sha": expected_sha,
                "workflow_id": run.get("workflow_id"),
                "run_id": run_id,
                "jobs": jobs,
                "aggregate_passed": passed,
                "required_gate_known": required_gate_known,
                "independent": True,
            }
            bindings = _bindings(context, repository=f"{owner}/{repo}", sha=expected_sha, workflow_run_id=str(run_id), workflow_id=str(run.get("workflow_id")))
            observation = ProviderObservation("ci", f"github-actions:{owner}/{repo}:runs/{run_id}", f"{run_response_id}:{jobs_response_id}", FactMutability.mutable_snapshot, bindings, facts, datetime.now(timezone.utc), self.mutable_max_age_seconds)
            return ingest_observation(db, context, observation)
        except Exception as exc:
            return provider_failure(exc)
