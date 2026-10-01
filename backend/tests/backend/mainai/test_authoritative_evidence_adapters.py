from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import httpx

from app.claim_action_integrity.adapters import (
    AdapterAuthenticationError,
    AdapterRateLimited,
    AdapterUnavailable,
    DatabaseEvidenceAdapter,
    EvidenceContext,
    FactMutability,
    FilesystemEvidenceAdapter,
    GitHubActionsEvidenceAdapter,
    GitHubAPIReader,
    GitHubEvidenceAdapter,
    ObservationStatus,
    ProviderObservation,
    TestRunnerEvidenceAdapter,
    TestRunResult,
    ingest_observation,
)
from app.claim_action_integrity.service import assess_claim, record_evidence
from app.models.claim_action_integrity import ClaimActionEvidence
from app.models.user import User


SHA = "1" * 40
OTHER_SHA = "2" * 40
TREE = "3" * 40


def _context(db, *, action="github_branch", execution="exec-1"):
    owner = User(email=f"adapter-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(owner)
    db.flush()
    return EvidenceContext(owner.id, execution, "release:alpha", action, "task-1")


class FakeGitHub:
    def __init__(self, responses=None, error=None):
        self.responses = responses or {}
        self.error = error

    def get(self, resource):
        if self.error:
            raise self.error
        return self.responses[resource], f"etag:{resource}"


class FakeCI:
    def __init__(self, run, jobs, error=None):
        self.run, self.jobs, self.error = run, jobs, error

    def get_workflow_run(self, owner, repo, run_id):
        if self.error:
            raise self.error
        return self.run, "run-etag"

    def get_workflow_jobs(self, owner, repo, run_id):
        return self.jobs, "jobs-etag"


def _branch_response(branch="feature", sha=SHA):
    return {"ref": f"refs/heads/{branch}", "object": {"sha": sha}}


def _assess(db, context, evidence_id, *, action=None, sha=SHA, bindings=None, now=None):
    return assess_claim(
        db,
        owner_id=context.owner_id,
        execution_id=context.execution_id,
        subject_key=context.subject_key,
        action_key=action or context.action_key,
        requested_state="externally_observed",
        artifact_sha=sha,
        evidence_id=evidence_id,
        required_bindings=bindings,
        now=now,
    )


def test_github_branch_binds_repo_branch_remote_and_local_sha(superuser_db):
    context = _context(superuser_db, action="branch_push")
    reader = FakeGitHub({"repos/acme/widget/git/ref/heads/feature": _branch_response()})
    result = GitHubEvidenceAdapter(reader).observe_branch(
        superuser_db, context, owner="acme", repo="widget", branch="feature", local_sha=SHA,
    )
    assert result.status is ObservationStatus.observed
    assessment = _assess(
        superuser_db, context, result.evidence_id,
        bindings={"repository": "acme/widget", "branch": "feature", "sha": SHA},
    )
    assert assessment.claimable is True


def test_wrong_repo_right_sha_is_rejected_by_kernel_binding(superuser_db):
    context = _context(superuser_db, action="branch_push")
    reader = FakeGitHub({"repos/evil/widget/git/ref/heads/feature": _branch_response()})
    result = GitHubEvidenceAdapter(reader).observe_branch(
        superuser_db, context, owner="evil", repo="widget", branch="feature", local_sha=SHA,
    )
    assessment = _assess(superuser_db, context, result.evidence_id, bindings={"repository": "acme/widget"})
    assert assessment.claimable is False
    assert "binding_mismatch:repository" in assessment.reasons


def test_adapter_evidence_cannot_upgrade_without_explicit_claim_bindings(superuser_db):
    context = _context(superuser_db, action="branch_push")
    reader = FakeGitHub({"repos/acme/widget/git/ref/heads/feature": _branch_response()})
    result = GitHubEvidenceAdapter(reader).observe_branch(
        superuser_db, context, owner="acme", repo="widget", branch="feature", local_sha=SHA,
    )
    assessment = _assess(superuser_db, context, result.evidence_id)
    assert assessment.claimable is False
    assert "claim_binding_requirements_missing" in assessment.reasons


def test_remote_branch_claim_cannot_be_satisfied_by_local_filesystem_evidence(superuser_db):
    context = _context(superuser_db, action="branch_push")
    evidence = record_evidence(
        superuser_db, owner_id=context.owner_id, execution_id=context.execution_id,
        subject_key=context.subject_key, action_key=context.action_key, source_type="filesystem",
        source_ref="git:local", payload={"pushed": True, "branch": "feature", "local_sha": SHA, "remote_sha": SHA},
        artifact_sha=SHA, recorded_by="agent",
    )
    assert _assess(superuser_db, context, evidence.id).claimable is False


def test_immutable_commit_tree_fact_has_no_expiry(superuser_db):
    context = _context(superuser_db, action="github_commit")
    resource = f"repos/acme/widget/commits/{SHA}"
    reader = FakeGitHub({resource: {"sha": SHA, "commit": {"tree": {"sha": TREE}}, "parents": [{"sha": OTHER_SHA}]}})
    result = GitHubEvidenceAdapter(reader).observe_commit(superuser_db, context, owner="acme", repo="widget", sha=SHA)
    evidence = superuser_db.get(ClaimActionEvidence, result.evidence_id)
    assert evidence.payload["fact_mutability"] == "immutable_fact"
    assert evidence.expires_at is None
    assert evidence.payload["tree_hash"] == TREE


def test_remote_branch_absence_is_a_fresh_snapshot_not_inferred_success(superuser_db):
    context = _context(superuser_db, action="github_branch")
    resource = "repos/acme/widget/git/ref/heads/missing"
    result = GitHubEvidenceAdapter(FakeGitHub({resource: {"exists": False}})).observe_branch(
        superuser_db, context, owner="acme", repo="widget", branch="missing",
    )
    evidence = superuser_db.get(ClaimActionEvidence, result.evidence_id)
    assert evidence.payload["remote_exists"] is False
    assert evidence.artifact_sha is None
    assert evidence.expires_at is not None


def test_commit_ancestry_is_bound_to_both_commits_and_repository(superuser_db):
    context = _context(superuser_db, action="github_commit")
    resource = f"repos/acme/widget/compare/{OTHER_SHA}...{SHA}"
    response = {
        "status": "ahead", "ahead_by": 2, "behind_by": 0,
        "base_commit": {"sha": OTHER_SHA}, "head_commit": {"sha": SHA},
        "merge_base_commit": {"sha": OTHER_SHA},
    }
    result = GitHubEvidenceAdapter(FakeGitHub({resource: response})).observe_commit_ancestry(
        superuser_db, context, owner="acme", repo="widget", base_sha=OTHER_SHA, head_sha=SHA,
    )
    assessment = _assess(
        superuser_db, context, result.evidence_id,
        bindings={"repository": "acme/widget", "base_sha": OTHER_SHA, "sha": SHA},
    )
    assert assessment.claimable is True


def test_stale_mutable_github_snapshot_is_not_current(superuser_db):
    context = _context(superuser_db, action="branch_push")
    result = GitHubEvidenceAdapter(
        FakeGitHub({"repos/acme/widget/git/ref/heads/feature": _branch_response()}),
        mutable_max_age_seconds=1,
    ).observe_branch(superuser_db, context, owner="acme", repo="widget", branch="feature", local_sha=SHA)
    evidence = superuser_db.get(ClaimActionEvidence, result.evidence_id)
    assessment = _assess(superuser_db, context, result.evidence_id, now=evidence.expires_at + timedelta(seconds=1))
    assert assessment.claimable is False
    assert assessment.verification_state == "stale"


def test_ci_sha_mismatch_rejected_and_creates_no_evidence(superuser_db):
    context = _context(superuser_db, action="test_run")
    adapter = GitHubActionsEvidenceAdapter(FakeCI(
        {"id": 7, "head_sha": OTHER_SHA, "workflow_id": 12, "status": "completed", "conclusion": "success"},
        [{"id": 1, "status": "completed", "conclusion": "success"}],
    ))
    result = adapter.observe_run(superuser_db, context, owner="acme", repo="widget", run_id=7, expected_sha=SHA)
    assert result.status is ObservationStatus.invalid_evidence
    assert superuser_db.query(ClaimActionEvidence).count() == 0


@pytest.mark.parametrize(
    ("run_status", "run_conclusion", "jobs"),
    [
        ("in_progress", None, [{"id": 1, "status": "in_progress", "conclusion": None}]),
        ("completed", "failure", [{"id": 1, "status": "completed", "conclusion": "failure"}]),
        ("completed", "success", [
            {"id": 1, "status": "completed", "conclusion": "success"},
            {"id": 2, "status": "completed", "conclusion": "failure"},
        ]),
    ],
)
def test_running_failed_or_one_passing_job_never_implies_aggregate_pass(superuser_db, run_status, run_conclusion, jobs):
    context = _context(superuser_db, action="test_run")
    run = {"id": 7, "head_sha": SHA, "workflow_id": 12, "status": run_status, "conclusion": run_conclusion}
    result = GitHubActionsEvidenceAdapter(FakeCI(run, jobs)).observe_run(
        superuser_db, context, owner="acme", repo="widget", run_id=7, expected_sha=SHA,
    )
    evidence = superuser_db.get(ClaimActionEvidence, result.evidence_id)
    assert evidence.payload["aggregate_passed"] is False
    assert _assess(superuser_db, context, result.evidence_id).claimable is False


def test_required_ci_gate_must_be_identifiable_and_all_success(superuser_db):
    context = _context(superuser_db, action="test_run")
    run = {"id": 7, "head_sha": SHA, "workflow_id": 12, "status": "completed", "conclusion": "success", "required_job_ids": [1, 2]}
    result = GitHubActionsEvidenceAdapter(FakeCI(run, [{"id": 1, "status": "completed", "conclusion": "success"}])).observe_run(
        superuser_db, context, owner="acme", repo="widget", run_id=7, expected_sha=SHA,
    )
    evidence = superuser_db.get(ClaimActionEvidence, result.evidence_id)
    assert evidence.payload["required_gate_known"] is False
    assert evidence.payload["aggregate_passed"] is False


def test_successful_ci_exact_binding_can_upgrade_test_claim(superuser_db):
    context = _context(superuser_db, action="test_run")
    run = {"id": 7, "head_sha": SHA, "workflow_id": 12, "status": "completed", "conclusion": "success", "required_job_ids": [1]}
    jobs = [{"id": 1, "run_id": 7, "status": "completed", "conclusion": "success"}]
    result = GitHubActionsEvidenceAdapter(FakeCI(run, jobs)).observe_run(
        superuser_db, context, owner="acme", repo="widget", run_id=7, expected_sha=SHA,
    )
    assessment = _assess(superuser_db, context, result.evidence_id, bindings={"repository": "acme/widget", "workflow_run_id": "7"})
    assert assessment.claimable is True
    assert assessment.verification_state == "independently_verified"


def test_test_started_is_unknown_and_wrong_execution_is_invalid(superuser_db):
    context = _context(superuser_db, action="test_run")
    start = datetime.now(timezone.utc)
    adapter = TestRunnerEvidenceAdapter()
    running = TestRunResult("exec-1", SHA, TREE, ("pytest",), ("tests",), {"TZ": "UTC"}, start, None, 0, 0, 0, None, "runner-1")
    assert adapter.ingest_result(superuser_db, context, running).status is ObservationStatus.unknown
    wrong = TestRunResult("other", SHA, TREE, ("pytest",), (), {}, start, start, 1, 0, 0, 0, "runner-2")
    assert adapter.ingest_result(superuser_db, context, wrong).status is ObservationStatus.invalid_evidence


def test_structured_test_result_binds_checkout_counts_environment_and_exit(superuser_db):
    context = _context(superuser_db, action="test_run")
    start = datetime.now(timezone.utc)
    run = TestRunResult("exec-1", SHA, TREE, ("pytest", "-q"), ("tests/unit",), {"TZ": "UTC"}, start, start + timedelta(seconds=2), 8, 0, 1, 0, "runner-3")
    result = TestRunnerEvidenceAdapter().ingest_result(superuser_db, context, run)
    assert _assess(superuser_db, context, result.evidence_id, bindings={"tree_hash": TREE}).claimable is True


def test_artifact_filename_cannot_replace_content_hash(superuser_db, tmp_path):
    context = _context(superuser_db, action="artifact_observation")
    artifact = tmp_path / "release.tar"
    artifact.write_bytes(b"actual")
    wrong_expected = hashlib.sha256(b"different").hexdigest()
    result = FilesystemEvidenceAdapter().observe_artifact(superuser_db, context, path=artifact, expected_sha256=wrong_expected)
    evidence = superuser_db.get(ClaimActionEvidence, result.evidence_id)
    assert evidence.payload["file_exists"] is True
    assert evidence.payload["valid_artifact"] is False
    assessment = assess_claim(
        superuser_db, owner_id=context.owner_id, execution_id=context.execution_id,
        subject_key=context.subject_key, action_key=context.action_key,
        requested_state="externally_observed", evidence_id=result.evidence_id,
    )
    assert assessment.claimable is False


def test_matching_artifact_hash_is_bound_to_content_size_and_path(superuser_db, tmp_path):
    context = _context(superuser_db, action="artifact_observation")
    artifact = tmp_path / "release.tar"
    artifact.write_bytes(b"actual")
    expected = hashlib.sha256(b"actual").hexdigest()
    result = FilesystemEvidenceAdapter().observe_artifact(superuser_db, context, path=artifact, expected_sha256=expected)
    assessment = assess_claim(
        superuser_db, owner_id=context.owner_id, execution_id=context.execution_id,
        subject_key=context.subject_key, action_key=context.action_key,
        requested_state="externally_observed", evidence_id=result.evidence_id,
        required_bindings={"artifact_sha256": expected},
    )
    assert assessment.claimable is True


def test_database_adapter_observes_head_without_mutation_surface(superuser_db):
    context = _context(superuser_db, action="database_observation")
    adapter = DatabaseEvidenceAdapter()
    assert not hasattr(adapter, "execute") and not hasattr(adapter, "update")
    result = adapter.observe_migration_head(superuser_db, context, expected_head="0089_claim_action_integrity")
    evidence = superuser_db.get(ClaimActionEvidence, result.evidence_id)
    assert evidence.payload["matches_expected"] is True


@pytest.mark.parametrize(
    ("error", "status", "retryable"),
    [
        (AdapterUnavailable("down"), ObservationStatus.unknown, True),
        (AdapterRateLimited("limit"), ObservationStatus.retryable, True),
        (AdapterAuthenticationError("auth"), ObservationStatus.unavailable, False),
    ],
)
def test_provider_failures_never_create_pass_evidence(superuser_db, error, status, retryable):
    context = _context(superuser_db, action="github_branch")
    result = GitHubEvidenceAdapter(FakeGitHub(error=error)).observe_branch(
        superuser_db, context, owner="acme", repo="widget", branch="feature",
    )
    assert result.status is status
    assert result.retryable is retryable
    assert result.evidence_id is None
    assert superuser_db.query(ClaimActionEvidence).count() == 0


def test_agent_text_cannot_fabricate_adapter_contract(superuser_db):
    context = _context(superuser_db, action="github_branch")
    evidence = record_evidence(
        superuser_db, owner_id=context.owner_id, execution_id=context.execution_id,
        subject_key=context.subject_key, action_key=context.action_key,
        source_type="agent_self_report", source_ref="agent:text",
        payload={"adapter_contract": "claim-action-evidence/v1", "bindings": {"repository": "acme/widget", "sha": SHA}},
        artifact_sha=SHA, recorded_by="agent",
    )
    assessment = _assess(superuser_db, context, evidence.id, action="github_branch")
    assert assessment.claimable is False
    assert "self_report_or_non_authoritative_source" in assessment.reasons


def test_ingestion_rejects_wrong_common_execution_binding(superuser_db):
    context = _context(superuser_db, action="github_branch")
    observation = ProviderObservation(
        "github", "github:acme/widget", "etag:1", FactMutability.mutable_snapshot,
        {"owner_id": str(context.owner_id), "execution_id": "wrong", "subject": context.subject_key, "task_id": "task-1"},
        {"remote_exists": True}, datetime.now(timezone.utc), 60,
    )
    result = ingest_observation(superuser_db, context, observation)
    assert result.status is ObservationStatus.invalid_evidence
    assert superuser_db.query(ClaimActionEvidence).count() == 0


def test_concrete_github_reader_is_get_only_and_captures_provider_identity():
    def handler(request: httpx.Request):
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={"full_name": "acme/widget", "default_branch": "main"},
            headers={"x-github-request-id": "REQ-1", "etag": '"abc"'},
        )

    client = httpx.Client(base_url="https://api.github.test", transport=httpx.MockTransport(handler))
    reader = GitHubAPIReader("test-token", client=client)
    payload, response_id = reader.get("repos/acme/widget")
    assert payload["default_branch"] == "main"
    assert response_id == 'github-request:REQ-1:etag:"abc"'
    assert not hasattr(reader, "post") and not hasattr(reader, "patch")
    reader.close()


def test_concrete_github_reader_maps_rate_limit_fail_closed():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            403, json={"message": "rate limit"},
            headers={"x-ratelimit-remaining": "0", "x-github-request-id": "REQ-2"},
        )
    )
    reader = GitHubAPIReader("test-token", client=httpx.Client(base_url="https://api.github.test", transport=transport))
    with pytest.raises(AdapterRateLimited):
        reader.get("repos/acme/widget")
    reader.close()
