"""Fail-closed authority primitives for the first supervised activation cycle.

This module performs no provider call and no remote mutation.  Its rows are authority
inputs for a later transport layer; they are not evidence that a push occurred.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.execution_envelope import ExecutionAuthorizationEnvelope
from app.models.mainai_activation import (
    ActivationPreflightVerdict,
    MainAIActivationPreflightReceipt,
    MainAIPushGrant,
    PushGrantStatus,
)
from app.models.mainai_execution import MainAIGoal, MainAITask, MainAITaskEvent, MainAITaskEventType
from app.models.mainai_job import MainAIJob
from app.models.mainai_recovery import MainAITaskWorktree
from app.models.provider_spend import ProviderSpendAuthorization, ProviderSpendAuthorizationStatus

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
FORBIDDEN_ACTIVATION_CAPABILITIES = frozenset({"merge", "deploy", "deployment", "credentials", "credential_grant", "recall", "recall_activation"})
REQUIRED_ACTIVATION_CAPABILITIES = frozenset({"path_a", "repo_edit", "push"})


class ActivationAuthorityError(RuntimeError):
    code = "activation_authority_denied"


class FounderAuthorityRequired(ActivationAuthorityError):
    code = "founder_authority_required"


class ActivationBaseSHAMoved(ActivationAuthorityError):
    code = "activation_base_sha_moved"


class ActivationReceiptStale(ActivationAuthorityError):
    code = "activation_receipt_stale"


class ActivationEnvelopeSuperseded(ActivationAuthorityError):
    code = "activation_envelope_superseded"


class PushGrantExpired(ActivationAuthorityError):
    code = "push_grant_expired"


class PushGrantConsumed(ActivationAuthorityError):
    code = "push_grant_consumed"


class AuthorityVersionMismatch(ActivationAuthorityError):
    code = "authority_version_mismatch"


@dataclass(frozen=True)
class AuthoritativePreflightFacts:
    activation_sha: str | None
    repository_identity: str | None
    integration_branch: str | None
    observed_base_sha: str | None
    runtime_config: dict[str, Any]
    runtime_token_identity: str | None
    runtime_token_attestation_digest: str | None
    ruleset_attestation_digest: str | None
    founder_attestation_digest: str | None
    negative_authority_confirmed: bool


def _authoritative_now(db: Session, override: datetime | None) -> datetime:
    """Use database time for authority decisions; tests may inject a fixed clock."""
    if override is not None:
        return override
    # PostgreSQL ``now()`` is the transaction-start timestamp. Authority checks may run in
    # a long transaction, so use the wall clock there; retain a portable fallback for unit DBs.
    clock = func.clock_timestamp() if db.get_bind().dialect.name == "postgresql" else func.current_timestamp()
    value = db.execute(select(clock)).scalar_one()
    if not isinstance(value, datetime):
        raise ActivationAuthorityError("database did not return an authoritative timestamp")
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo is not None else value


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def canonical_required_checks(checks: list[dict[str, str]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for check in checks:
        if set(check) != {"name", "app_slug"}:
            raise ActivationAuthorityError("required checks must contain only name and app_slug")
        pair = (check["name"].strip(), check["app_slug"].strip())
        if not all(pair) or pair in seen:
            raise ActivationAuthorityError("required checks must be non-empty and unique")
        seen.add(pair)
        normalized.append({"name": pair[0], "app_slug": pair[1]})
    return sorted(normalized, key=lambda item: (item["name"], item["app_slug"]))


def _live_receipt(receipt: MainAIActivationPreflightReceipt, *, now: datetime) -> None:
    if receipt.verdict != ActivationPreflightVerdict.go.value or receipt.unknown_required_facts:
        raise ActivationReceiptStale("preflight receipt is not GO")
    if receipt.invalidated_at is not None or receipt.expires_at <= now:
        raise ActivationReceiptStale("preflight receipt is stale")


def create_preflight_receipt(
    db: Session,
    *,
    owner_id: uuid.UUID,
    goal_id: uuid.UUID,
    execution_envelope_id: uuid.UUID,
    provider_spend_authorization_id: uuid.UUID,
    expected_repository_identity: str,
    expected_integration_branch: str,
    expected_base_branch: str,
    expected_base_sha: str,
    expected_activation_sha: str,
    expected_authority_version: int,
    facts: AuthoritativePreflightFacts,
    expires_at: datetime,
    founder_authorized: bool,
    now: datetime | None = None,
) -> MainAIActivationPreflightReceipt:
    if not founder_authorized:
        raise FounderAuthorityRequired("only the Founder control plane may persist preflight authority")
    now = _authoritative_now(db, now)
    if not SHA_RE.fullmatch(expected_base_sha) or not SHA_RE.fullmatch(expected_activation_sha):
        raise ActivationAuthorityError("activation and base SHAs must be exact lowercase 40-character SHAs")
    if not REPOSITORY_RE.fullmatch(expected_repository_identity) or not expected_base_branch.strip() or not expected_integration_branch.strip():
        raise ActivationAuthorityError("repository and branch bindings must be canonical and non-empty")
    if expires_at <= now or expires_at > now + timedelta(minutes=15):
        raise ActivationAuthorityError("preflight expiry must be within 15 minutes")
    goal = db.execute(select(MainAIGoal).where(MainAIGoal.id == goal_id, MainAIGoal.owner_id == owner_id)).scalar_one_or_none()
    envelope = db.execute(select(ExecutionAuthorizationEnvelope).where(ExecutionAuthorizationEnvelope.id == execution_envelope_id, ExecutionAuthorizationEnvelope.owner_id == owner_id)).scalar_one_or_none()
    spend = db.execute(select(ProviderSpendAuthorization).where(ProviderSpendAuthorization.id == provider_spend_authorization_id, ProviderSpendAuthorization.owner_id == owner_id)).scalar_one_or_none()
    unknown: list[str] = []
    if goal is None:
        unknown.append("goal")
    if envelope is None or envelope.goal_id != goal_id or envelope.status != "active":
        unknown.append("active_execution_envelope")
    if spend is None or spend.goal_id != goal_id or spend.execution_envelope_id != execution_envelope_id or spend.status != ProviderSpendAuthorizationStatus.active.value or (spend.expires_at and spend.expires_at <= now):
        unknown.append("active_spend_authorization")

    expected = {
        "repository_identity": expected_repository_identity,
        "integration_branch": expected_integration_branch,
        "base_branch": expected_base_branch,
        "base_sha": expected_base_sha,
        "activation_sha": expected_activation_sha,
        "authority_version": expected_authority_version,
    }
    authoritative = {
        "repository_identity": facts.repository_identity,
        "integration_branch": facts.integration_branch,
        "base_sha": facts.observed_base_sha,
        "activation_sha": facts.activation_sha,
    }
    for key, value in authoritative.items():
        if not value:
            unknown.append(key)
        elif value != expected[key]:
            unknown.append(f"{key}_mismatch")
    if envelope is not None:
        if envelope.repository_identity != expected_repository_identity:
            unknown.append("envelope_repository_mismatch")
        if envelope.base_branch != expected_base_branch:
            unknown.append("envelope_base_branch_mismatch")
        if envelope.approved_base_sha != expected_base_sha:
            unknown.append("envelope_base_sha_mismatch")
        if envelope.authority_version != expected_authority_version:
            unknown.append("authority_version_mismatch")
        caps = set(envelope.authorized_capabilities or [])
        if not REQUIRED_ACTIVATION_CAPABILITIES.issubset(caps) or caps & FORBIDDEN_ACTIVATION_CAPABILITIES:
            unknown.append("activation_capability_boundary")
    checks = canonical_required_checks(envelope.required_check_contexts if envelope is not None else [])
    if not checks:
        unknown.append("required_check_contexts")
    for name, value in (
        ("runtime_token_identity", facts.runtime_token_identity),
        ("runtime_token_attestation", facts.runtime_token_attestation_digest),
        ("ruleset_attestation", facts.ruleset_attestation_digest),
        ("founder_attestation", facts.founder_attestation_digest),
    ):
        if not value:
            unknown.append(name)
        elif name != "runtime_token_identity" and not DIGEST_RE.fullmatch(value):
            unknown.append(f"{name}_invalid")
    if not facts.negative_authority_confirmed:
        unknown.append("negative_authority")
    unknown = sorted(set(unknown))
    config_digest = _digest(facts.runtime_config)
    payload = {
        **expected,
        "owner_id": str(owner_id), "goal_id": str(goal_id),
        "execution_envelope_id": str(execution_envelope_id),
        "provider_spend_authorization_id": str(provider_spend_authorization_id),
        "required_check_contexts": checks, "runtime_config_digest": config_digest,
        "runtime_token_identity": facts.runtime_token_identity or "",
        "runtime_token_attestation_digest": facts.runtime_token_attestation_digest or "",
        "ruleset_attestation_digest": facts.ruleset_attestation_digest or "",
        "founder_attestation_digest": facts.founder_attestation_digest or "",
        "unknown_required_facts": unknown,
        "verdict": "no_go" if unknown else "go",
        "expires_at": expires_at.isoformat(),
    }
    row = MainAIActivationPreflightReceipt(
        owner_id=owner_id, goal_id=goal_id, execution_envelope_id=execution_envelope_id,
        provider_spend_authorization_id=provider_spend_authorization_id,
        repository_identity=expected_repository_identity, integration_branch=expected_integration_branch,
        activation_sha=expected_activation_sha, base_branch=expected_base_branch, base_sha=expected_base_sha,
        authority_version=expected_authority_version, required_check_contexts=checks,
        runtime_config_digest=config_digest, runtime_token_identity=facts.runtime_token_identity or "unknown",
        runtime_token_attestation_digest=facts.runtime_token_attestation_digest or "0" * 64,
        ruleset_attestation_digest=facts.ruleset_attestation_digest or "0" * 64,
        founder_attestation_digest=facts.founder_attestation_digest or "0" * 64,
        facts={"authoritative": authoritative, "runtime": facts.runtime_config},
        unknown_required_facts=unknown, verdict=payload["verdict"], observed_at=now,
        expires_at=expires_at, receipt_digest=_digest(payload), provenance={"source": "founder_activation_preflight_v1"},
    )
    db.add(row)
    db.flush()
    return row


def create_push_grant(
    db: Session, *, owner_id: uuid.UUID, goal_id: uuid.UUID, task_id: uuid.UUID,
    job_id: uuid.UUID, worktree_id: uuid.UUID, execution_envelope_id: uuid.UUID,
    preflight_receipt_id: uuid.UUID, repository_identity: str, branch: str,
    base_branch: str, base_sha: str, local_commit_sha: str, authority_version: int,
    expires_at: datetime, idempotency_key: str, authorized_by: str,
    founder_authorized: bool, now: datetime | None = None,
    local_head_resolver: Callable[[MainAITaskWorktree], str | None] | None = None,
) -> MainAIPushGrant:
    if not founder_authorized:
        raise FounderAuthorityRequired("MainAI cannot mint a Founder push grant")
    now = _authoritative_now(db, now)
    if expires_at <= now or not SHA_RE.fullmatch(local_commit_sha) or not SHA_RE.fullmatch(base_sha):
        raise ActivationAuthorityError("invalid expiry or commit SHA")
    existing = db.execute(select(MainAIPushGrant).where(MainAIPushGrant.owner_id == owner_id, MainAIPushGrant.idempotency_key == idempotency_key)).scalar_one_or_none()
    if existing is not None:
        exact = (
            existing.goal_id,
            existing.task_id,
            existing.job_id,
            existing.worktree_id,
            existing.execution_envelope_id,
            existing.preflight_receipt_id,
            existing.repository_identity,
            existing.branch,
            existing.base_branch,
            existing.base_sha,
            existing.local_commit_sha,
            existing.authority_version,
            existing.expires_at,
        ) == (
            goal_id,
            task_id,
            job_id,
            worktree_id,
            execution_envelope_id,
            preflight_receipt_id,
            repository_identity,
            branch,
            base_branch,
            base_sha,
            local_commit_sha,
            authority_version,
            expires_at,
        )
        if not exact:
            raise ActivationAuthorityError("idempotency key reused with different authority bindings")
        return existing
    task = db.execute(select(MainAITask).where(MainAITask.id == task_id, MainAITask.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    job = db.execute(select(MainAIJob).where(MainAIJob.id == job_id, MainAIJob.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    worktree = db.execute(select(MainAITaskWorktree).where(MainAITaskWorktree.id == worktree_id, MainAITaskWorktree.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    envelope = db.execute(select(ExecutionAuthorizationEnvelope).where(ExecutionAuthorizationEnvelope.id == execution_envelope_id, ExecutionAuthorizationEnvelope.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    receipt = db.execute(select(MainAIActivationPreflightReceipt).where(MainAIActivationPreflightReceipt.id == preflight_receipt_id, MainAIActivationPreflightReceipt.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if task is None or task.goal_id != goal_id or task.mainai_job_id != job_id or job is None:
        raise ActivationAuthorityError("task/job/owner binding mismatch")
    if worktree is None or worktree.task_id != task_id or worktree.job_id != job_id or worktree.repo != repository_identity or worktree.branch != branch or worktree.base_sha != base_sha:
        raise ActivationAuthorityError("worktree binding mismatch")
    observed_head = local_head_resolver(worktree) if local_head_resolver else worktree.current_commit
    if observed_head != local_commit_sha:
        raise ActivationAuthorityError("local commit SHA mismatch")
    if envelope is None or envelope.status != "active":
        raise ActivationEnvelopeSuperseded("execution envelope is not active")
    if envelope.goal_id != goal_id or envelope.repository_identity != repository_identity or envelope.base_branch != base_branch or envelope.approved_base_sha != base_sha:
        raise ActivationAuthorityError("envelope binding mismatch")
    if envelope.authority_version != authority_version:
        raise AuthorityVersionMismatch("authority version mismatch")
    if receipt is None:
        raise ActivationReceiptStale("preflight receipt missing")
    _live_receipt(receipt, now=now)
    receipt_tuple = (receipt.goal_id, receipt.execution_envelope_id, receipt.repository_identity, receipt.integration_branch, receipt.base_branch, receipt.base_sha, receipt.authority_version)
    if receipt_tuple != (goal_id, execution_envelope_id, repository_identity, branch, base_branch, base_sha, authority_version):
        raise ActivationReceiptStale("preflight receipt bindings differ")
    if canonical_required_checks(envelope.required_check_contexts or []) != canonical_required_checks(
        receipt.required_check_contexts or []
    ):
        raise ActivationReceiptStale("required check set changed after preflight")
    if expires_at > receipt.expires_at:
        raise ActivationAuthorityError("push grant cannot outlive its preflight receipt")
    passed = db.execute(select(MainAITaskEvent.id).where(MainAITaskEvent.task_id == task_id, MainAITaskEvent.owner_id == owner_id, MainAITaskEvent.event_type == MainAITaskEventType.verification_passed)).first()
    if passed is None:
        raise ActivationAuthorityError("verification PASS is not durably recorded")
    row = MainAIPushGrant(
        owner_id=owner_id, goal_id=goal_id, task_id=task_id, job_id=job_id, worktree_id=worktree_id,
        execution_envelope_id=execution_envelope_id, preflight_receipt_id=preflight_receipt_id,
        repository_identity=repository_identity, branch=branch, base_branch=base_branch, base_sha=base_sha,
        local_commit_sha=local_commit_sha, authority_version=authority_version, authorized_by=authorized_by,
        authorized_at=now, expires_at=expires_at, status=PushGrantStatus.active.value,
        idempotency_key=idempotency_key, provenance={"source": "founder_push_grant_v1"},
    )
    db.add(row)
    db.flush()
    return row


def cancel_push_grant(db: Session, *, owner_id: uuid.UUID, grant_id: uuid.UUID, founder_authorized: bool) -> MainAIPushGrant:
    if not founder_authorized:
        raise FounderAuthorityRequired("only the Founder may cancel push authority")
    row = db.execute(select(MainAIPushGrant).where(MainAIPushGrant.id == grant_id, MainAIPushGrant.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ActivationAuthorityError("push grant not found")
    if row.status == PushGrantStatus.cancelled.value:
        return row
    if row.status != PushGrantStatus.active.value:
        raise ActivationAuthorityError(f"cannot cancel {row.status} grant")
    row.status = PushGrantStatus.cancelled.value
    db.flush()
    return row


def consume_push_grant(
    db: Session, *, owner_id: uuid.UUID, grant_id: uuid.UUID, job_id: uuid.UUID,
    local_commit_sha: str, observed_base_sha: str, authority_version: int,
    now: datetime | None = None,
) -> MainAIPushGrant:
    """Atomically consumes authority only; it deliberately performs no git operation."""
    now = _authoritative_now(db, now)
    row = db.execute(select(MainAIPushGrant).where(MainAIPushGrant.id == grant_id, MainAIPushGrant.owner_id == owner_id).with_for_update()).scalar_one_or_none()
    if row is None:
        raise ActivationAuthorityError("push grant not found")
    if row.status == PushGrantStatus.consumed.value:
        raise PushGrantConsumed("push grant was already consumed")
    if row.status != PushGrantStatus.active.value:
        raise ActivationAuthorityError(f"push grant is {row.status}")
    if row.expires_at <= now:
        row.status = PushGrantStatus.expired.value
        raise PushGrantExpired("push grant expired")
    if row.job_id != job_id or row.local_commit_sha != local_commit_sha:
        raise ActivationAuthorityError("push grant execution binding mismatch")
    if row.base_sha != observed_base_sha:
        raise ActivationBaseSHAMoved("base branch moved after approval")
    if row.authority_version != authority_version:
        raise AuthorityVersionMismatch("authority version mismatch")
    envelope = db.execute(
        select(ExecutionAuthorizationEnvelope)
        .where(ExecutionAuthorizationEnvelope.id == row.execution_envelope_id, ExecutionAuthorizationEnvelope.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if envelope is None or envelope.status != "active":
        raise ActivationEnvelopeSuperseded("execution envelope was superseded after grant creation")
    if envelope.authority_version != authority_version:
        raise AuthorityVersionMismatch("active envelope authority version mismatch")
    receipt = db.execute(
        select(MainAIActivationPreflightReceipt)
        .where(MainAIActivationPreflightReceipt.id == row.preflight_receipt_id, MainAIActivationPreflightReceipt.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if receipt is None:
        raise ActivationReceiptStale("bound preflight receipt no longer exists")
    _live_receipt(receipt, now=now)
    worktree = db.execute(
        select(MainAITaskWorktree)
        .where(MainAITaskWorktree.id == row.worktree_id, MainAITaskWorktree.owner_id == owner_id)
        .with_for_update()
    ).scalar_one_or_none()
    if worktree is None or worktree.job_id != job_id or worktree.current_commit != local_commit_sha:
        raise ActivationAuthorityError("authoritative worktree HEAD no longer matches the grant")
    result = db.execute(update(MainAIPushGrant).where(MainAIPushGrant.id == row.id, MainAIPushGrant.status == PushGrantStatus.active.value).values(status=PushGrantStatus.consumed.value, consumed_at=now, consumed_by_job_id=job_id))
    if result.rowcount != 1:
        raise PushGrantConsumed("push grant lost atomic consumption race")
    db.flush()
    return db.get(MainAIPushGrant, row.id, populate_existing=True)
