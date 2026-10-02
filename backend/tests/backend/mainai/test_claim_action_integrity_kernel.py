"""Adversarial proofs for MODEL OUTPUT != FACT and INTENT != EXECUTION."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.claim_action_integrity import (
    ClaimActionIntegrityError,
    assess_claim,
    record_evidence,
    record_receipt,
    require_claimable,
)
from app.models.claim_action_integrity import ClaimActionEvidence
from app.models.user import User
from app.mainai_runtime_contract import sanitize_unverified_execution_claims


SHA = "1" * 40
OTHER_SHA = "2" * 40


def _owner(db):
    owner = User(email=f"claim-integrity-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(owner)
    db.flush()
    return owner


def _evidence(db, owner, *, action, source, payload, execution="exec-1", sha=SHA, observed_at=None, expires_at=None):
    return record_evidence(
        db,
        owner_id=owner.id,
        execution_id=execution,
        subject_key="release:alpha",
        action_key=action,
        source_type=source,
        source_ref=f"opaque:{uuid.uuid4()}",
        payload=payload,
        artifact_sha=sha,
        observed_at=observed_at,
        expires_at=expires_at,
        recorded_by="trusted-control-plane",
    )


def _assess(db, owner, action, state, evidence=None, *, execution="exec-1", sha=SHA):
    return assess_claim(
        db,
        owner_id=owner.id,
        execution_id=execution,
        subject_key="release:alpha",
        action_key=action,
        requested_state=state,
        artifact_sha=sha,
        evidence_id=evidence.id if evidence else None,
    )


def test_requested_cannot_be_upgraded_to_completed_without_evidence(superuser_db):
    owner = _owner(superuser_db)
    result = _assess(superuser_db, owner, "generic", "completed")
    assert result.claimable is False
    assert result.effective_state == "unknown"
    assert "state of release:alpha is unknown" in result.allowed_language


def test_unknown_remains_unknown_and_requested_requires_a_request_receipt(superuser_db):
    owner = _owner(superuser_db)
    unknown = _assess(superuser_db, owner, "generic", "unknown", sha=None)
    assert unknown.claimable is True
    assert unknown.effective_state == "unknown"

    unproved_request = _assess(superuser_db, owner, "generic", "requested", sha=None)
    assert unproved_request.claimable is False
    assert unproved_request.effective_state == "unknown"
    receipt = record_receipt(
        superuser_db, owner_id=owner.id, execution_id="exec-1", subject_key="release:alpha",
        action_key="generic", declared_state="requested", action_state="requested",
        declared_action={"request": "generic"}, permitted_action={}, executed_action={},
        observed_result={}, authority_snapshot={}, created_by="request-ledger",
    )
    assert receipt.effective_state == "requested"
    proved_request = _assess(superuser_db, owner, "generic", "requested", sha=None)
    assert proved_request.claimable is True
    assert proved_request.effective_state == "requested"


def test_failure_also_requires_bound_authoritative_evidence(superuser_db):
    owner = _owner(superuser_db)
    unproved = _assess(superuser_db, owner, "test_run", "failed")
    assert unproved.claimable is False
    assert unproved.effective_state == "unknown"

    evidence = _evidence(
        superuser_db,
        owner,
        action="test_run",
        source="test_runner",
        payload={
            "status": "failed",
            "passed": 2,
            "failed": 1,
            "execution_id": "exec-1",
            "artifact_sha": SHA,
            "command": "pytest",
            "environment": {"tz": "UTC"},
        },
    )
    proved = _assess(superuser_db, owner, "test_run", "failed", evidence)
    assert proved.claimable is True
    assert proved.effective_state == "failed"


def test_mainai_text_and_agent_self_report_can_never_prove_completion(superuser_db):
    owner = _owner(superuser_db)
    for source in ("mainai_generated_text", "agent_self_report"):
        evidence = _evidence(
            superuser_db, owner, action="agent_completion", source=source,
            payload={"status": "completed", "job_id": "job-1", "result_artifact_id": "artifact-1"},
        )
        result = _assess(superuser_db, owner, "agent_completion", "completed", evidence)
        assert result.claimable is False
        assert result.effective_state == "unknown"
        assert "self_report_or_non_authoritative_source" in result.reasons


def test_local_commit_is_not_remote_push_and_sha_must_match(superuser_db):
    owner = _owner(superuser_db)
    local = _evidence(
        superuser_db, owner, action="branch_push", source="filesystem",
        payload={"pushed": True, "branch": "x", "local_sha": SHA, "remote_sha": SHA},
    )
    assert _assess(superuser_db, owner, "branch_push", "externally_observed", local).effective_state == "unknown"

    mismatch = _evidence(
        superuser_db, owner, action="branch_push", source="github",
        payload={"pushed": True, "branch": "x", "local_sha": SHA, "remote_sha": OTHER_SHA},
    )
    result = _assess(superuser_db, owner, "branch_push", "externally_observed", mismatch)
    assert result.effective_state == "unknown"
    assert "local_remote_sha_mismatch" in result.reasons


def test_exact_github_remote_branch_sha_proves_push(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(
        superuser_db, owner, action="branch_push", source="github",
        payload={"pushed": True, "branch": "codex/x", "local_sha": SHA, "remote_sha": SHA},
    )
    result = _assess(superuser_db, owner, "branch_push", "externally_observed", evidence)
    assert result.claimable is True
    assert result.effective_state == "externally_observed"


def test_test_invoked_is_not_test_passed(superuser_db):
    owner = _owner(superuser_db)
    invoked = _evidence(
        superuser_db, owner, action="test_run", source="test_runner",
        payload={"status": "running", "execution_id": "exec-1", "artifact_sha": SHA, "command": "pytest", "environment": {}},
    )
    result = _assess(superuser_db, owner, "test_run", "completed", invoked)
    assert result.effective_state == "unknown"
    assert "passing_test_counts_required" in result.reasons


def test_passing_test_requires_counts_sha_execution_and_environment(superuser_db):
    owner = _owner(superuser_db)
    passed = _evidence(
        superuser_db, owner, action="test_run", source="test_runner",
        payload={"status": "passed", "passed": 42, "failed": 0, "skipped": 1, "execution_id": "exec-1", "artifact_sha": SHA, "command": "pytest -q", "environment": {"tz": "UTC"}},
    )
    result = _assess(superuser_db, owner, "test_run", "externally_observed", passed)
    assert result.claimable is True

    wrong_execution = _assess(superuser_db, owner, "test_run", "externally_observed", passed, execution="exec-2")
    assert wrong_execution.effective_state == "unknown"
    assert "execution_id_mismatch" in wrong_execution.reasons


def test_builder_pass_is_not_independent_certification(superuser_db):
    owner = _owner(superuser_db)
    self_review = _evidence(
        superuser_db, owner, action="certification", source="verification_registry",
        payload={"review_result": "PASS", "candidate_sha": SHA, "builder_actor_id": "agent:codex", "examiner_actor_id": "agent:codex"},
    )
    result = _assess(superuser_db, owner, "certification", "certified", self_review)
    assert result.effective_state == "unknown"
    assert "exact_sha_independent_examiner_required" in result.reasons


def test_independent_exact_sha_registry_pass_can_certify_but_grants_no_action(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(
        superuser_db, owner, action="certification", source="verification_registry",
        payload={"review_result": "PASS", "candidate_sha": SHA, "builder_actor_id": "agent:codex", "examiner_actor_id": "agent:claude"},
    )
    assert _assess(superuser_db, owner, "certification", "certified", evidence).claimable is True
    with pytest.raises(ClaimActionIntegrityError, match="permission"):
        record_receipt(
            superuser_db, owner_id=owner.id, execution_id="exec-1", subject_key="release:alpha",
            action_key="certification", declared_state="certified", action_state="completed",
            declared_action={"claim": "certified"}, permitted_action={"granted": False},
            executed_action={"next_action": "merge"}, observed_result={}, authority_snapshot={},
            created_by="codex", artifact_sha=SHA, evidence_id=evidence.id,
        )


def test_merge_does_not_prove_deployment(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(
        superuser_db, owner, action="merge", source="github",
        payload={"merged": True, "merge_sha": SHA},
    )
    result = _assess(superuser_db, owner, "merge", "deployed", evidence)
    assert result.effective_state == "merged"
    assert result.claimable is False


def test_deployment_does_not_prove_activation(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(
        superuser_db, owner, action="deployment", source="deployment_provider",
        payload={"status": "succeeded", "deployment_id": "dep-1", "artifact_sha": SHA},
    )
    result = _assess(superuser_db, owner, "deployment", "activated", evidence)
    assert result.effective_state == "deployed"
    assert result.claimable is False


def test_activation_requires_explicit_provider_or_external_observation(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(
        superuser_db, owner, action="activation", source="external_service",
        payload={"activated": True, "deployment_id": "dep-1", "artifact_sha": SHA},
    )
    assert _assess(superuser_db, owner, "activation", "activated", evidence).claimable is True


def test_agent_completion_requires_task_ledger_and_result_artifact(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(
        superuser_db, owner, action="agent_completion", source="task_execution_ledger",
        payload={"status": "completed", "job_id": "job-1", "result_artifact_id": "artifact-1"},
    )
    assert _assess(superuser_db, owner, "agent_completion", "externally_observed", evidence).claimable is True


def test_wrong_owner_and_wrong_sha_fail_closed(superuser_db):
    owner = _owner(superuser_db)
    other = _owner(superuser_db)
    evidence = _evidence(
        superuser_db, other, action="branch_push", source="github",
        payload={"pushed": True, "branch": "x", "local_sha": SHA, "remote_sha": SHA},
    )
    wrong_owner = _assess(superuser_db, owner, "branch_push", "externally_observed", evidence)
    assert "owner_mismatch" in wrong_owner.reasons
    wrong_sha = _assess(superuser_db, other, "branch_push", "externally_observed", evidence, sha=OTHER_SHA)
    assert "artifact_sha_mismatch" in wrong_sha.reasons


def test_stale_and_newer_conflicting_evidence_cannot_upgrade(superuser_db):
    owner = _owner(superuser_db)
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    stale = _evidence(
        superuser_db, owner, action="branch_push", source="github", observed_at=old,
        expires_at=old + timedelta(minutes=10),
        payload={"pushed": True, "branch": "x", "local_sha": SHA, "remote_sha": SHA},
    )
    stale_result = _assess(superuser_db, owner, "branch_push", "externally_observed", stale)
    assert stale_result.verification_state == "stale"

    fresh = _evidence(
        superuser_db, owner, action="test_run", source="test_runner", observed_at=old,
        payload={"status": "passed", "passed": 1, "failed": 0, "execution_id": "exec-1", "artifact_sha": SHA, "command": "pytest", "environment": {}},
    )
    _evidence(
        superuser_db, owner, action="test_run", source="test_runner",
        payload={"status": "failed", "passed": 0, "failed": 1, "execution_id": "exec-1", "artifact_sha": SHA, "command": "pytest", "environment": {}},
    )
    conflict = _assess(superuser_db, owner, "test_run", "externally_observed", fresh)
    assert conflict.verification_state == "conflicting"
    assert "superseded_by_newer_authoritative_evidence" in conflict.reasons


def test_mutable_snapshot_gets_governed_expiry_without_caller_help(superuser_db):
    owner = _owner(superuser_db)
    observed = datetime.now(timezone.utc) - timedelta(days=30)
    evidence = _evidence(
        superuser_db, owner, action="branch_push", source="github", observed_at=observed,
        payload={"pushed": True, "branch": "x", "local_sha": SHA, "remote_sha": SHA},
    )
    assert evidence.payload["fact_mutability"] == "mutable_snapshot"
    assert evidence.expires_at is not None
    result = _assess(superuser_db, owner, "branch_push", "externally_observed", evidence)
    assert result.claimable is False
    assert result.verification_state == "stale"


def test_older_failure_cannot_override_newer_pass_and_self_report_cannot_veto(superuser_db):
    owner = _owner(superuser_db)
    old = datetime.now(timezone.utc) - timedelta(minutes=2)
    failed = _evidence(
        superuser_db, owner, action="test_run", source="test_runner", observed_at=old,
        payload={"status": "failed", "passed": 0, "failed": 1, "execution_id": "exec-1", "artifact_sha": SHA, "command": "pytest", "environment": {}},
    )
    passed = _evidence(
        superuser_db, owner, action="test_run", source="test_runner",
        payload={"status": "passed", "passed": 9, "failed": 0, "execution_id": "exec-1", "artifact_sha": SHA, "command": "pytest", "environment": {}},
    )
    old_failure = _assess(superuser_db, owner, "test_run", "failed", failed)
    assert old_failure.claimable is False
    assert "superseded_by_newer_authoritative_evidence" in old_failure.reasons

    _evidence(
        superuser_db, owner, action="test_run", source="agent_self_report",
        payload={"status": "failed", "passed": 0, "failed": 1},
    )
    current_pass = _assess(superuser_db, owner, "test_run", "externally_observed", passed)
    assert current_pass.claimable is True


def test_certification_requires_caller_sha_and_canonical_independent_actor_ids(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(
        superuser_db, owner, action="certification", source="verification_registry",
        payload={"review_result": "PASS", "candidate_sha": SHA, "builder_actor_id": "actor:codex", "examiner_actor_id": " Actor:Codex "},
    )
    without_sha = assess_claim(
        superuser_db, owner_id=owner.id, execution_id="exec-1", subject_key="release:alpha",
        action_key="certification", requested_state="certified", evidence_id=evidence.id,
    )
    assert without_sha.claimable is False
    assert "caller_candidate_sha_required" in without_sha.reasons
    normalized_same_actor = _assess(superuser_db, owner, "certification", "certified", evidence)
    assert normalized_same_actor.claimable is False
    assert "exact_sha_independent_examiner_required" in normalized_same_actor.reasons


def test_empty_generic_database_payload_cannot_prove_completion(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(superuser_db, owner, action="generic", source="database", payload={}, sha=None)
    result = _assess(superuser_db, owner, "generic", "completed", evidence, sha=None)
    assert result.claimable is False
    assert result.effective_state == "unknown"
    assert "specific_database_state_proof_required" in result.reasons


def test_receipt_chain_rejects_old_predecessor_and_preserves_declared_vs_effective(superuser_db):
    owner = _owner(superuser_db)
    first = record_receipt(
        superuser_db, owner_id=owner.id, execution_id="exec-1", subject_key="release:alpha",
        action_key="branch_push", declared_state="completed", action_state="requested",
        declared_action={"text": "push branch"}, permitted_action={"granted": False},
        executed_action={}, observed_result={}, authority_snapshot={}, created_by="agent",
    )
    assert first.declared_state == "completed"
    assert first.effective_state == "unknown"
    second = record_receipt(
        superuser_db, owner_id=owner.id, execution_id="exec-1", subject_key="release:alpha",
        action_key="branch_push", declared_state="requested", action_state="requested",
        declared_action={}, permitted_action={}, executed_action={}, observed_result={},
        authority_snapshot={}, created_by="agent", predecessor_receipt_id=first.id,
    )
    with pytest.raises(ClaimActionIntegrityError, match="latest receipt"):
        record_receipt(
            superuser_db, owner_id=owner.id, execution_id="exec-1", subject_key="release:alpha",
            action_key="branch_push", declared_state="requested", action_state="requested",
            declared_action={}, permitted_action={}, executed_action={}, observed_result={},
            authority_snapshot={}, created_by="agent", predecessor_receipt_id=first.id,
        )
    assert second.predecessor_receipt_id == first.id


def test_database_rejects_self_report_marked_authoritative_and_mutation(superuser_db):
    owner = _owner(superuser_db)
    row = ClaimActionEvidence(
        owner_id=owner.id, execution_id="exec-x", subject_key="x", action_key="generic",
        source_type="mainai_generated_text", source_ref="self", artifact_sha=None,
        authoritative=True, payload={"success": True}, payload_digest="a" * 64,
        observed_at=datetime.now(timezone.utc), recorded_by="mainai",
    )
    superuser_db.add(row)
    with pytest.raises(Exception):
        superuser_db.flush()
    superuser_db.rollback()

    owner = _owner(superuser_db)
    evidence = _evidence(superuser_db, owner, action="generic", source="database", payload={"success": True})
    evidence.source_ref = "rewritten"
    with pytest.raises(Exception):
        superuser_db.flush()
    superuser_db.rollback()


def test_delete_guard_requires_real_active_erasure_operation_not_spoofed_gucs(superuser_db):
    owner = _owner(superuser_db)
    evidence = _evidence(
        superuser_db, owner, action="branch_push", source="github",
        payload={"pushed": True, "branch": "x", "local_sha": SHA, "remote_sha": SHA},
    )
    superuser_db.execute(text("SET LOCAL app.current_user_id = :owner"), {"owner": str(owner.id)})
    superuser_db.execute(text("SET LOCAL app.claim_action_erasure_in_progress = 'true'"))
    superuser_db.execute(
        text("SET LOCAL app.account_erasure_operation_id = :operation"),
        {"operation": str(uuid.uuid4())},
    )
    with pytest.raises(Exception):
        superuser_db.execute(
            text("DELETE FROM claim_action_evidence WHERE evidence_id=:evidence"),
            {"evidence": evidence.id},
        )
    superuser_db.rollback()


def test_guard_function_has_hardened_search_path(superuser_db):
    config = superuser_db.execute(
        text("SELECT proconfig FROM pg_proc WHERE proname='claim_action_integrity_guard'")
    ).scalar_one()
    assert "search_path=pg_catalog" in config


def test_caller_asserted_permission_without_grant_evidence_is_rejected(superuser_db):
    owner = _owner(superuser_db)
    with pytest.raises(ClaimActionIntegrityError, match="genuine grant evidence"):
        record_receipt(
            superuser_db, owner_id=owner.id, execution_id="exec-1", subject_key="release:alpha",
            action_key="merge", declared_state="requested", action_state="dispatched",
            declared_action={"action": "merge"},
            permitted_action={"granted": True, "authority_ref": "caller-says-so", "scopes": ["merge"]},
            executed_action={}, observed_result={}, authority_snapshot={}, created_by="mainai",
        )

def test_require_claimable_raises_instead_of_upgrading_language(superuser_db):
    owner = _owner(superuser_db)
    with pytest.raises(ClaimActionIntegrityError, match="cannot be upgraded"):
        require_claimable(
            superuser_db, owner_id=owner.id, execution_id="exec-1", subject_key="release:alpha",
            action_key="deployment", requested_state="deployed", artifact_sha=SHA,
        )


def test_runtime_role_has_read_only_no_fabrication_privilege():
    from app.db import migration_engine

    with migration_engine.connect() as connection:
        privileges = connection.execute(text("""
            SELECT privilege_type FROM information_schema.role_table_grants
            WHERE grantee='mainai_app' AND table_name IN ('claim_action_evidence','claim_action_receipts')
            ORDER BY privilege_type
        """)).scalars().all()
    assert privileges == ["SELECT", "SELECT"]


@pytest.mark.parametrize(
    "claim",
    [
        "I've pushed the branch.",
        "Tests are green.",
        "CI passed.",
        "All 2883 tests passed.",
        "It's merged into main.",
        "Deployed to production.",
        "Recall is live.",
        "Codex finished the task.",
        "Det är mergat.",
        "Recall är aktiverad.",
    ],
)
def test_natural_unsupported_external_claims_are_removed(claim):
    sanitized = sanitize_unverified_execution_claims(claim)
    assert sanitized != claim
    assert "binding evidence" in sanitized or "bindande evidens" in sanitized


@pytest.mark.parametrize(
    "honest",
    [
        "I have not verified that the branch was pushed.",
        "The branch was not pushed.",
        "Was the branch pushed?",
        "I cannot verify whether CI passed.",
        "Jag har inte verifierat att det är mergat.",
        "Är Recall aktiverad?",
    ],
)
def test_uncertainty_negation_and_questions_preserve_epistemic_meaning(honest):
    assert sanitize_unverified_execution_claims(honest) == honest


def test_negated_clause_cannot_mask_a_separate_unsupported_assertion():
    text = "I have not verified that the branch was pushed, but CI passed."
    sanitized = sanitize_unverified_execution_claims(text)
    assert "I have not verified that the branch was pushed" in sanitized
    assert "CI passed" not in sanitized
    assert "binding evidence" in sanitized


@pytest.mark.parametrize(
    "paraphrase",
    [
        "The branch is synced with origin.",
        "GitHub has the commit available.",
        "The test suite is clean.",
        "Pytest is passing.",
        "The pipeline is green.",
        "The workflow succeeded.",
        "The build was successful.",
        "The change landed on main.",
        "PR 42 is integrated.",
        "Production is live.",
        "The release is deployed.",
        "Recall is enabled.",
        "Recall is running.",
        "The agent is done.",
        "Claude completed the task.",
        "Grenen är synkad.",
        "Testsviten är godkända.",
        "CI är grön.",
        "Ändringen är mergad.",
        "Agenten är färdig.",
    ],
)
def test_compositional_boundary_catches_natural_paraphrases(paraphrase):
    assert sanitize_unverified_execution_claims(paraphrase) != paraphrase


@pytest.mark.parametrize(
    "claim",
    [
        "The branch is on GitHub.",
        "All tests passed.",
        "The deployment succeeded.",
        "The release has been certified.",
        "The pull request was merged.",
        "Recall has been activated.",
        "The agent finished.",
    ],
)
def test_plain_chat_cannot_emit_external_fact_without_receipt_context(claim):
    sanitized = sanitize_unverified_execution_claims(claim)
    assert claim not in sanitized
    assert "cannot present that external state as verified" in sanitized
