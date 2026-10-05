"""Founder sovereignty + family delegation control plane.

The Founder talks to MainAI. Family members receive only scoped Life
capabilities through governed approvals. Conversation/policy state here does
not grant merge, deploy, Recall, or database-superuser authority.

Authority derives from the authenticated session GUC and governed Founder
binding. Public FOUNDER_USER_ID is a sentinel, not a credential.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.founder import FOUNDER_USER_ID
from app.mainai_founder_sovereignty.catalog import get_capability
from app.mainai_founder_sovereignty.identity import (
    canonical_approval_snapshot_hash,
    require_authenticated_founder,
    session_user_id,
)
from app.mainai_founder_sovereignty.kernel import refuse_kernel_mutation
from app.mainai_founder_sovereignty.types import (
    ActorKind,
    ApprovalContext,
    ApprovalMode,
    AuthorizationResult,
    AuthorizationVerdict,
    FamilyRelationship,
    PolicyClass,
    PolicySource,
    PolicyVersionView,
    RiskTier,
    SovereigntyError,
    StepUpPurpose,
    TenantKind,
)
from app.models.founder_sovereignty import (
    FamilyApprovalReceipt,
    FamilyApprovalRequest,
    FamilyCapabilityGrant,
    FamilyMember,
    FounderInstanceBinding,
    FounderPolicyHead,
    FounderPolicyProposal,
    FounderPolicyVersion,
    FounderStepUpReceipt,
    UserAITenantBoundary,
)

_PROTECTED_FOUNDER_SURFACES = frozenset(
    {
        "founder_private_memory",
        "founder_private_conversation",
        "founder_project_context",
        "founder_secrets",
        "founder_credentials",
        "founder_policies",
        "founder_agent_control",
        "founder_tenant",
        "founder_authority",
        "founder_policy_root",
    }
)

_MAX_UNTIL = timedelta(days=366)
_STEP_UP_TTL = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bind_founder_instance(db: Session, *, actor_id: UUID | None = None) -> FounderInstanceBinding:
    uid = require_authenticated_founder(db, claimed_actor_id=actor_id)
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext('founder_instance_bind'))"))
    existing = db.query(FounderInstanceBinding).one_or_none()
    if existing is not None:
        if existing.founder_user_id != FOUNDER_USER_ID or existing.owner_id != FOUNDER_USER_ID:
            raise SovereigntyError("rebind_forbidden", "Founder MainAI is already bound to the single Founder identity")
        return existing
    row = FounderInstanceBinding(
        owner_id=FOUNDER_USER_ID,
        founder_user_id=FOUNDER_USER_ID,
        tenant_kind=TenantKind.FOUNDER_MAINAI.value,
        bound_at=_now(),
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError as exc:
        existing = db.query(FounderInstanceBinding).one_or_none()
        if existing is None:
            raise SovereigntyError("bind_failed", "Founder binding race did not yield a legitimate row") from exc
        return existing
    return row


def propose_policy(
    db: Session,
    *,
    policy_key: str,
    payload: dict,
    source: PolicySource,
    notes: str = "",
) -> FounderPolicyProposal:
    if source is PolicySource.FOUNDER:
        raise SovereigntyError("use_apply", "founder applies policy; this path is for non-authoritative proposals")
    row = FounderPolicyProposal(
        owner_id=FOUNDER_USER_ID,
        policy_key=policy_key,
        payload=payload,
        source=source.value,
        applied=False,
        notes=notes,
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def apply_founder_policy(
    db: Session,
    *,
    actor_id: UUID | None = None,
    actor_kind: ActorKind,
    policy_key: str,
    payload: dict,
    reason: str,
    policy_class: PolicyClass = PolicyClass.FOUNDER_POLICY,
    approval_receipt_id: UUID | None = None,
) -> FounderPolicyVersion:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    if actor_kind is not ActorKind.FOUNDER:
        raise SovereigntyError("not_founder", "only the authenticated Founder may apply policy")
    refuse_kernel_mutation(policy_class, policy_key)
    if policy_class is PolicyClass.KERNEL_SECURITY_INVARIANT:
        raise SovereigntyError("kernel_immutable", "runtime must not insert kernel-invariant state")
    bind_founder_instance(db, actor_id=None)
    latest = (
        db.query(FounderPolicyVersion)
        .filter_by(owner_id=FOUNDER_USER_ID, policy_key=policy_key)
        .order_by(FounderPolicyVersion.version_number.desc())
        .first()
    )
    version = FounderPolicyVersion(
        owner_id=FOUNDER_USER_ID,
        policy_key=policy_key,
        policy_class=policy_class.value,
        version_number=(latest.version_number + 1) if latest else 1,
        payload=payload,
        reason=reason,
        actor_kind=ActorKind.FOUNDER.value,
        previous_version_id=latest.id if latest else None,
        approval_receipt_id=approval_receipt_id,
        created_at=_now(),
    )
    db.add(version)
    db.flush()
    head = db.get(FounderPolicyHead, {"owner_id": FOUNDER_USER_ID, "policy_key": policy_key})
    if head is None:
        db.add(
            FounderPolicyHead(
                owner_id=FOUNDER_USER_ID,
                policy_key=policy_key,
                current_version_id=version.id,
                workflow_locked=False,
                updated_at=_now(),
            )
        )
    else:
        head.current_version_id = version.id
        head.updated_at = _now()
    db.flush()
    return version


def inspect_active_policies(db: Session, *, actor_id: UUID | None = None) -> list[PolicyVersionView]:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    heads = db.query(FounderPolicyHead).filter_by(owner_id=FOUNDER_USER_ID).all()
    views: list[PolicyVersionView] = []
    for head in heads:
        version = db.get(FounderPolicyVersion, head.current_version_id)
        if version is None:
            continue
        previous = db.get(FounderPolicyVersion, version.previous_version_id) if version.previous_version_id else None
        views.append(
            PolicyVersionView(
                version_number=version.version_number,
                policy_class=PolicyClass(version.policy_class),
                policy_key=version.policy_key,
                payload=version.payload,
                reason=version.reason,
                created_at=version.created_at,
                previous_version_number=previous.version_number if previous else None,
            )
        )
    return views


def restore_founder_policy_to_version(
    db: Session,
    *,
    actor_id: UUID | None = None,
    actor_kind: ActorKind,
    policy_key: str,
    target_version: int,
    reason: str,
) -> FounderPolicyVersion:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    if actor_kind is not ActorKind.FOUNDER:
        raise SovereigntyError("not_founder", "Non-Founder cannot restore Founder policy")
    refuse_kernel_mutation(PolicyClass.FOUNDER_POLICY, policy_key)
    require_recent_step_up(db, StepUpPurpose.POLICY_ROLLBACK)
    target = (
        db.query(FounderPolicyVersion)
        .filter_by(owner_id=FOUNDER_USER_ID, policy_key=policy_key, version_number=target_version)
        .one_or_none()
    )
    if target is None:
        raise SovereigntyError("no_prior_version", "target policy version does not exist")
    if PolicyClass(target.policy_class) is PolicyClass.KERNEL_SECURITY_INVARIANT:
        raise SovereigntyError("kernel_immutable", "Founder-policy recovery must not mutate kernel security invariants")
    return apply_founder_policy(
        db,
        actor_id=None,
        actor_kind=ActorKind.FOUNDER,
        policy_key=policy_key,
        payload=target.payload,
        reason=f"restore_to_version v{target_version}: {reason}",
        policy_class=PolicyClass(target.policy_class),
    )


def rollback_founder_policy(db: Session, *, actor_id: UUID | None = None, actor_kind: ActorKind, policy_key: str, reason: str) -> FounderPolicyVersion:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    if actor_kind is not ActorKind.FOUNDER:
        raise SovereigntyError("not_founder", "Non-Founder cannot rollback or unlock policy")
    refuse_kernel_mutation(PolicyClass.FOUNDER_POLICY, policy_key)
    head = db.get(FounderPolicyHead, {"owner_id": FOUNDER_USER_ID, "policy_key": policy_key})
    if head is None:
        raise SovereigntyError("no_policy", "no active policy to roll back")
    current = db.get(FounderPolicyVersion, head.current_version_id)
    if current is None or current.previous_version_id is None:
        raise SovereigntyError("no_prior_version", "no prior policy version exists")
    prior = db.get(FounderPolicyVersion, current.previous_version_id)
    if prior is None:
        raise SovereigntyError("no_prior_version", "prior policy version missing")
    return restore_founder_policy_to_version(
        db,
        actor_id=None,
        actor_kind=ActorKind.FOUNDER,
        policy_key=policy_key,
        target_version=prior.version_number,
        reason=reason,
    )


def disable_founder_policy(db: Session, *, actor_id: UUID | None = None, policy_key: str, reason: str) -> FounderPolicyVersion:
    return apply_founder_policy(
        db,
        actor_id=actor_id,
        actor_kind=ActorKind.FOUNDER,
        policy_key=policy_key,
        payload={"enabled": False},
        reason=reason,
    )


def set_workflow_lock(
    db: Session,
    *,
    actor_id: UUID | None = None,
    actor_kind: ActorKind = ActorKind.FOUNDER,
    policy_key: str,
    locked: bool,
) -> FounderPolicyHead:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    if actor_kind is not ActorKind.FOUNDER:
        raise SovereigntyError("self_unlock_forbidden", "MainAI must never self-unlock Founder policy")
    refuse_self_unlock(actor_kind=actor_kind)
    refuse_kernel_mutation(PolicyClass.FOUNDER_POLICY, policy_key)
    head = db.get(FounderPolicyHead, {"owner_id": FOUNDER_USER_ID, "policy_key": policy_key})
    if head is None:
        raise SovereigntyError("no_policy", "no policy head to lock or unlock")
    if head.workflow_locked and not locked:
        require_recent_step_up(db, StepUpPurpose.WORKFLOW_UNLOCK)
    head.workflow_locked = locked
    head.updated_at = _now()
    db.flush()
    return head


def refuse_self_unlock(*, actor_kind: ActorKind) -> None:
    if actor_kind is not ActorKind.FOUNDER:
        raise SovereigntyError("self_unlock_forbidden", "MainAI must never self-unlock Founder policy")


def apply_from_proposal(db: Session, *, actor_id: UUID | None = None, proposal_id: UUID, reason: str) -> FounderPolicyVersion:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    proposal = db.get(FounderPolicyProposal, proposal_id)
    if proposal is None:
        raise SovereigntyError("no_proposal", "proposal not found")
    version = apply_founder_policy(
        db,
        actor_id=None,
        actor_kind=ActorKind.FOUNDER,
        policy_key=proposal.policy_key,
        payload=proposal.payload,
        reason=reason,
    )
    proposal.applied = True
    db.flush()
    return version


def refuse_non_founder_policy_source(source: PolicySource) -> None:
    if source is not PolicySource.FOUNDER:
        raise SovereigntyError(
            "source_is_not_policy",
            "REMOTE AGENT INPUT / BUG REPORT / PROMPT INJECTION / MODEL OUTPUT != POLICY",
        )


def issue_founder_step_up(
    db: Session,
    *,
    purpose: StepUpPurpose,
    ttl: timedelta | None = None,
) -> FounderStepUpReceipt:
    require_authenticated_founder(db)
    jti = db.execute(text("SELECT COALESCE(NULLIF(current_setting('app.current_access_jti', true), ''), 'session')")).scalar()
    row = FounderStepUpReceipt(
        owner_id=FOUNDER_USER_ID,
        purpose=purpose.value,
        session_jti=str(jti or "session"),
        verified_at=_now(),
        expires_at=_now() + (ttl or _STEP_UP_TTL),
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def require_recent_step_up(db: Session, purpose: StepUpPurpose) -> FounderStepUpReceipt:
    require_authenticated_founder(db)
    row = (
        db.query(FounderStepUpReceipt)
        .filter(
            FounderStepUpReceipt.owner_id == FOUNDER_USER_ID,
            FounderStepUpReceipt.purpose == purpose.value,
            FounderStepUpReceipt.consumed_at.is_(None),
            FounderStepUpReceipt.expires_at > _now(),
        )
        .order_by(FounderStepUpReceipt.verified_at.desc())
        .first()
    )
    if row is None:
        raise SovereigntyError("step_up_required", f"high-risk Founder action {purpose.value} requires a recent verified step-up receipt")
    return row


def add_family_member(
    db: Session,
    *,
    actor_id: UUID | None = None,
    principal_id: UUID,
    relationship: FamilyRelationship,
    display_name: str,
) -> FamilyMember:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    if principal_id == FOUNDER_USER_ID:
        raise SovereigntyError("family_is_not_founder", "FAMILY MEMBER != FOUNDER")
    row = FamilyMember(
        owner_id=FOUNDER_USER_ID,
        principal_id=principal_id,
        relationship=relationship.value,
        display_name=display_name,
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def _member(db: Session, principal_id: UUID) -> FamilyMember | None:
    return db.query(FamilyMember).filter_by(owner_id=FOUNDER_USER_ID, principal_id=principal_id).one_or_none()


def request_family_capability(
    db: Session,
    *,
    principal_id: UUID,
    capability_key: str,
    requested_data: dict | None = None,
    requested_limits: dict | None = None,
    requested_duration: str = "once",
    consequences: str = "",
    session_id: str | None = None,
    device_id: str | None = None,
) -> FamilyApprovalRequest:
    member = _member(db, principal_id)
    if member is None:
        raise SovereigntyError("unknown_family_member", "principal is not a governed family member")
    spec = get_capability(capability_key)
    if spec.risk_tier is RiskTier.FOUNDER_ONLY:
        raise SovereigntyError("founder_only", "FOUNDER_ONLY capabilities cannot be requested by family members")
    blocked = (
        db.query(FamilyCapabilityGrant)
        .filter_by(owner_id=FOUNDER_USER_ID, principal_id=principal_id, capability_key=capability_key, blocked_future=True)
        .first()
    )
    if blocked is not None:
        raise SovereigntyError("scope_blocked", "Founder blocked future requests for this scope")
    raw_token = secrets.token_urlsafe(32)
    data = requested_data or {}
    limits = requested_limits or {}
    snapshot = canonical_approval_snapshot_hash(
        principal_id=principal_id,
        resource=spec.resource,
        action=spec.action,
        scope="family",
        requested_data=data,
        requested_limits=limits,
        requested_duration=requested_duration,
        risk_tier=spec.risk_tier.value,
        consequences=consequences,
        session_id=session_id,
        device_id=device_id,
    )
    request = FamilyApprovalRequest(
        owner_id=FOUNDER_USER_ID,
        principal_id=principal_id,
        capability_key=spec.key,
        resource=spec.resource,
        action=spec.action,
        scope="family",
        requested_duration=requested_duration,
        requested_data=data,
        requested_limits=limits,
        consequences=consequences,
        risk_tier=spec.risk_tier.value,
        status="pending",
        request_token_hash=_hash_token(raw_token),
        snapshot_hash=snapshot,
        session_id=session_id,
        device_id=device_id,
        created_at=_now(),
    )
    db.add(request)
    db.flush()
    request._plaintext_token = raw_token  # test/control-plane handoff only; never a grant
    return request


def approval_context(request: FamilyApprovalRequest, *, who_label: str) -> ApprovalContext:
    return ApprovalContext(
        who_principal_id=request.principal_id,
        who_label=who_label,
        capability_key=request.capability_key,
        resource=request.resource,
        action=request.action,
        scope=request.scope,
        requested_data=request.requested_data,
        requested_limits=getattr(request, "requested_limits", {}) or {},
        requested_duration=request.requested_duration,
        consequences=request.consequences,
        risk_tier=RiskTier(request.risk_tier),
        snapshot_hash=request.snapshot_hash,
        session_id=request.session_id,
        device_id=request.device_id,
    )


def _recompute_request_hash(request: FamilyApprovalRequest) -> str:
    return canonical_approval_snapshot_hash(
        principal_id=request.principal_id,
        resource=request.resource,
        action=request.action,
        scope=request.scope,
        requested_data=request.requested_data,
        requested_limits=getattr(request, "requested_limits", {}) or {},
        requested_duration=request.requested_duration,
        risk_tier=request.risk_tier,
        consequences=request.consequences,
        session_id=request.session_id,
        device_id=request.device_id,
    )


def decide_approval(
    db: Session,
    *,
    actor_id: UUID | None = None,
    request_id: UUID,
    mode: ApprovalMode,
    duration: timedelta | None = None,
    until: datetime | None = None,
    limits: dict | None = None,
    session_id: str | None = None,
    expected_snapshot_hash: str | None = None,
) -> FamilyApprovalReceipt:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    request = db.get(FamilyApprovalRequest, request_id)
    if request is None or request.status != "pending":
        raise SovereigntyError("no_pending_request", "approval request is not pending")
    live_hash = _recompute_request_hash(request)
    if live_hash != request.snapshot_hash:
        raise SovereigntyError("snapshot_mismatch", "approval request was rewritten after presentation; create a NEW request")
    if expected_snapshot_hash is not None and expected_snapshot_hash != request.snapshot_hash:
        raise SovereigntyError("snapshot_mismatch", "decision receipt must bind to the exact snapshot hash the Founder saw")
    if mode in {ApprovalMode.ALWAYS_ALLOW, ApprovalMode.ALLOW_UNTIL_DATE} and request.risk_tier in {RiskTier.HIGH.value, RiskTier.FOUNDER_ONLY.value}:
        require_recent_step_up(db, StepUpPurpose.PERMANENT_HIGH_RISK_DELEGATION)
    request.status = "decided"
    receipt = FamilyApprovalReceipt(
        owner_id=FOUNDER_USER_ID,
        request_id=request.id,
        decision=mode.value,
        receipt_token_hash=_hash_token(secrets.token_urlsafe(32)),
        snapshot_hash=request.snapshot_hash,
        created_at=_now(),
    )
    db.add(receipt)
    db.flush()
    if mode in {ApprovalMode.DENY, ApprovalMode.REVIEW_EXACT_ACTION}:
        db.flush()
        return receipt
    remaining = 1 if mode is ApprovalMode.ALLOW_ONCE else None
    expires = None
    if mode is ApprovalMode.ALLOW_FOR_DURATION:
        if duration is None:
            raise SovereigntyError("duration_required", "ALLOW_FOR_DURATION requires a duration")
        expires = _now() + duration
    if mode is ApprovalMode.ALLOW_UNTIL_DATE:
        if until is None:
            raise SovereigntyError("until_required", "ALLOW_UNTIL_DATE requires an until timestamp")
        if until > _now() + _MAX_UNTIL:
            raise SovereigntyError("until_exceeds_maximum", "ALLOW_UNTIL_DATE exceeds Founder maximum duration")
        expires = until
    grant_limits = dict(request.requested_limits or {})
    if limits:
        grant_limits.update(limits)
    if mode is not ApprovalMode.DENY_AND_BLOCK:
        db.add(
            FamilyCapabilityGrant(
                owner_id=FOUNDER_USER_ID,
                principal_id=request.principal_id,
                capability_key=request.capability_key,
                resource=request.resource,
                action=request.action,
                scope=request.scope,
                duration_mode=mode.value,
                expires_at=expires,
                remaining_uses=remaining,
                limits=grant_limits,
                requested_data=request.requested_data or {},
                issuer_id=FOUNDER_USER_ID,
                approval_receipt_id=receipt.id,
                session_id=session_id or request.session_id,
                blocked_future=False,
                created_at=_now(),
            )
        )
    else:
        db.add(
            FamilyCapabilityGrant(
                owner_id=FOUNDER_USER_ID,
                principal_id=request.principal_id,
                capability_key=request.capability_key,
                resource=request.resource,
                action=request.action,
                scope=request.scope,
                duration_mode=mode.value,
                limits={},
                requested_data=request.requested_data or {},
                issuer_id=FOUNDER_USER_ID,
                approval_receipt_id=receipt.id,
                blocked_future=True,
                created_at=_now(),
            )
        )
    db.flush()
    return receipt


def revoke_grant(db: Session, *, actor_id: UUID | None = None, grant_id: UUID) -> FamilyCapabilityGrant:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    grant = db.get(FamilyCapabilityGrant, grant_id)
    if grant is None:
        raise SovereigntyError("no_grant", "grant not found")
    if grant.revoked_at is not None:
        return grant
    grant.revoked_at = _now()
    db.flush()
    return grant


def _limits_allow(granted: dict, requested: dict | None, data: dict | None) -> bool:
    granted = granted or {}
    requested = requested or {}
    data = data or {}
    if granted.get("no_external_invites") and (
        data.get("external_invites")
        or data.get("invite_external")
        or data.get("external_invitation")
        or requested.get("external_invites")
        or data.get("invitees")
    ):
        return False
    if "max_amount" in granted:
        amount = data.get("amount", requested.get("amount"))
        if amount is not None and float(amount) > float(granted["max_amount"]):
            return False
    for key, value in requested.items():
        if key in granted and granted[key] != value and key not in {"amount"}:
            if granted.get(key) is True and not value:
                continue
            if key == "no_external_invites" and value is False:
                return False
    return True


def _data_matches_grant(grant: FamilyCapabilityGrant, data: dict | None) -> bool:
    bound = grant.requested_data or {}
    if not bound or data is None:
        return True
    for key, value in bound.items():
        if key in data and data[key] != value:
            return False
    return True


def authorize_capability(
    db: Session,
    *,
    principal_id: UUID | None = None,
    capability_key: str,
    resource: str | None = None,
    action: str | None = None,
    scope: str | None = None,
    data: dict | None = None,
    limits: dict | None = None,
    now: datetime | None = None,
) -> AuthorizationResult:
    clock = now or _now()
    session_id = session_user_id(db)
    if principal_id is not None and principal_id != session_id:
        return AuthorizationResult(AuthorizationVerdict.DENIED_WRONG_PRINCIPAL, "CALLER-SUPPLIED actor_id != AUTHENTICATION")
    principal_id = session_id
    spec = get_capability(capability_key)
    if principal_id == FOUNDER_USER_ID:
        return AuthorizationResult(AuthorizationVerdict.ALLOWED, "Founder identity holds Founder-only authority")
    if spec.risk_tier is RiskTier.FOUNDER_ONLY:
        return AuthorizationResult(AuthorizationVerdict.DENIED_FOUNDER_ONLY, "CAPABILITY != FOUNDER_AUTHORITY")
    if _member(db, principal_id) is None:
        return AuthorizationResult(AuthorizationVerdict.DENIED_WRONG_PRINCIPAL, "Wrong person: not a family principal")
    blocked = (
        db.query(FamilyCapabilityGrant)
        .filter_by(owner_id=FOUNDER_USER_ID, principal_id=principal_id, capability_key=capability_key, blocked_future=True)
        .first()
    )
    if blocked is not None:
        return AuthorizationResult(AuthorizationVerdict.DENIED_BLOCKED, "Founder blocked this scope")
    grants = (
        db.query(FamilyCapabilityGrant)
        .filter_by(owner_id=FOUNDER_USER_ID, principal_id=principal_id, capability_key=capability_key, blocked_future=False)
        .order_by(FamilyCapabilityGrant.created_at.desc())
        .all()
    )
    if not grants:
        return AuthorizationResult(AuthorizationVerdict.DENIED_NO_GRANT, "FAMILY_MEMBERSHIP != CAPABILITY")
    mismatch: AuthorizationVerdict | None = None
    for grant in grants:
        if grant.revoked_at is not None:
            continue
        if grant.expires_at is not None and grant.expires_at <= clock:
            continue
        if grant.remaining_uses is not None and grant.remaining_uses <= 0:
            continue
        if resource is not None and grant.resource != resource:
            mismatch = AuthorizationVerdict.DENIED_RESOURCE
            continue
        if action is not None and grant.action != action:
            mismatch = AuthorizationVerdict.DENIED_SCOPE
            continue
        if scope is not None and grant.scope != scope:
            mismatch = AuthorizationVerdict.DENIED_SCOPE
            continue
        if not _data_matches_grant(grant, data):
            mismatch = AuthorizationVerdict.DENIED_DATA
            continue
        if not _limits_allow(grant.limits or {}, limits, data):
            mismatch = AuthorizationVerdict.DENIED_LIMITS
            continue
        if grant.remaining_uses is not None:
            try:
                db.execute(text("SELECT consume_family_capability_grant_once(:gid)"), {"gid": grant.id})
                db.flush()
                db.expire(grant)
            except Exception:
                db.expire(grant)
                continue
        return AuthorizationResult(AuthorizationVerdict.ALLOWED, "current grant matches exact capability", grant_id=grant.id)
    if mismatch is not None:
        return AuthorizationResult(mismatch, "capability grant does not cover requested resource, data, or limits")
    if any(grant.revoked_at is not None for grant in grants) and all(
        grant.revoked_at is not None or (grant.remaining_uses is not None and grant.remaining_uses <= 0) or (grant.expires_at is not None and grant.expires_at <= clock)
        for grant in grants
    ):
        if all(grant.revoked_at is not None for grant in grants):
            return AuthorizationResult(AuthorizationVerdict.DENIED_REVOKED, "REVOKED != ACTIVE")
    if any(grant.expires_at is not None and grant.expires_at <= clock and grant.revoked_at is None for grant in grants):
        return AuthorizationResult(AuthorizationVerdict.DENIED_EXPIRED, "expired capability must fail")
    if any(grant.remaining_uses is not None and grant.remaining_uses <= 0 for grant in grants):
        return AuthorizationResult(AuthorizationVerdict.DENIED_EXHAUSTED, "ONE_TIME_APPROVAL != PERMANENT_PERMISSION")
    return AuthorizationResult(AuthorizationVerdict.DENIED_NO_GRANT, "no current grant")


def authorize_with_receipt(db: Session, *, receipt_id: UUID, principal_id: UUID, capability_key: str) -> AuthorizationResult:
    receipt = db.get(FamilyApprovalReceipt, receipt_id)
    if receipt is None:
        return AuthorizationResult(AuthorizationVerdict.DENIED_OLD_RECEIPT, "unknown receipt is not current authorization")
    return AuthorizationResult(AuthorizationVerdict.DENIED_OLD_RECEIPT, "PAST_APPROVAL != CURRENT_APPROVAL; old receipt must fail")


def authorize_with_copied_token(*, token: str) -> AuthorizationResult:
    return AuthorizationResult(AuthorizationVerdict.DENIED_COPIED_TOKEN, "copied approval token is not a capability grant")


def assert_userai_boundary(db: Session, *, tenant_kind: TenantKind, surface: str) -> None:
    if tenant_kind is TenantKind.USERAI_PERSONAL and surface in _PROTECTED_FOUNDER_SURFACES:
        raise SovereigntyError(
            "userai_founder_leak",
            "UserAI has separate tenant/memory/authority/policy root and no Founder MainAI access",
        )


def define_userai_boundary(db: Session, *, actor_id: UUID | None = None) -> UserAITenantBoundary:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    row = UserAITenantBoundary(
        owner_id=FOUNDER_USER_ID,
        tenant_kind=TenantKind.USERAI_PERSONAL.value,
        memory_root="userai_personal_memory",
        policy_root="userai_personal_policy",
        notes="interface only — UserAI product is not built in this lane",
        created_at=_now(),
    )
    db.add(row)
    db.flush()
    return row


def list_pending_approvals(db: Session, *, actor_id: UUID | None = None) -> list[FamilyApprovalRequest]:
    require_authenticated_founder(db, claimed_actor_id=actor_id)
    return (
        db.query(FamilyApprovalRequest)
        .filter_by(owner_id=FOUNDER_USER_ID, status="pending")
        .order_by(FamilyApprovalRequest.created_at.asc())
        .all()
    )
