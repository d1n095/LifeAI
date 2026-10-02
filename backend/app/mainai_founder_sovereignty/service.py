"""Founder sovereignty + family delegation control plane.

The Founder talks to MainAI. Family members receive only scoped Life
capabilities through governed approvals. Conversation/policy state here does
not grant merge, deploy, Recall, or database-superuser authority.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy.orm import Session

from app.founder import FOUNDER_USER_ID
from app.mainai_founder_sovereignty.catalog import get_capability
from app.mainai_founder_sovereignty.kernel import assert_founder_identity, refuse_kernel_mutation
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
    }
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bind_founder_instance(db: Session, *, actor_id: UUID) -> FounderInstanceBinding:
    assert_founder_identity(actor_id)
    existing = db.query(FounderInstanceBinding).one_or_none()
    if existing is not None:
        if existing.founder_user_id != FOUNDER_USER_ID:
            raise SovereigntyError("rebind_forbidden", "Founder MainAI is already bound to the single Founder identity")
        return existing
    row = FounderInstanceBinding(
        owner_id=FOUNDER_USER_ID,
        founder_user_id=FOUNDER_USER_ID,
        tenant_kind=TenantKind.FOUNDER_MAINAI.value,
        bound_at=_now(),
    )
    db.add(row)
    db.flush()
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
    actor_id: UUID,
    actor_kind: ActorKind,
    policy_key: str,
    payload: dict,
    reason: str,
    policy_class: PolicyClass = PolicyClass.FOUNDER_POLICY,
    approval_receipt_id: UUID | None = None,
) -> FounderPolicyVersion:
    assert_founder_identity(actor_id)
    if actor_kind is not ActorKind.FOUNDER:
        raise SovereigntyError("not_founder", "only the authenticated Founder may apply policy")
    refuse_kernel_mutation(policy_class, policy_key)
    bind_founder_instance(db, actor_id=actor_id)
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


def inspect_active_policies(db: Session, *, actor_id: UUID) -> list[PolicyVersionView]:
    assert_founder_identity(actor_id)
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


def rollback_founder_policy(db: Session, *, actor_id: UUID, actor_kind: ActorKind, policy_key: str, reason: str) -> FounderPolicyVersion:
    assert_founder_identity(actor_id)
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
    restored = apply_founder_policy(
        db,
        actor_id=actor_id,
        actor_kind=ActorKind.FOUNDER,
        policy_key=policy_key,
        payload=prior.payload,
        reason=f"rollback to v{prior.version_number}: {reason}",
        policy_class=PolicyClass(prior.policy_class),
    )
    head.workflow_locked = False
    db.flush()
    return restored


def disable_founder_policy(db: Session, *, actor_id: UUID, policy_key: str, reason: str) -> FounderPolicyVersion:
    return apply_founder_policy(
        db,
        actor_id=actor_id,
        actor_kind=ActorKind.FOUNDER,
        policy_key=policy_key,
        payload={"enabled": False},
        reason=reason,
    )


def set_workflow_lock(db: Session, *, actor_id: UUID, policy_key: str, locked: bool) -> FounderPolicyHead:
    assert_founder_identity(actor_id)
    refuse_kernel_mutation(PolicyClass.FOUNDER_POLICY, policy_key)
    head = db.get(FounderPolicyHead, {"owner_id": FOUNDER_USER_ID, "policy_key": policy_key})
    if head is None:
        raise SovereigntyError("no_policy", "no policy head to lock or unlock")
    head.workflow_locked = locked
    head.updated_at = _now()
    db.flush()
    return head


def refuse_self_unlock(*, actor_kind: ActorKind) -> None:
    if actor_kind is not ActorKind.FOUNDER:
        raise SovereigntyError("self_unlock_forbidden", "MainAI must never self-unlock Founder policy")


def apply_from_proposal(db: Session, *, actor_id: UUID, proposal_id: UUID, reason: str) -> FounderPolicyVersion:
    assert_founder_identity(actor_id)
    proposal = db.get(FounderPolicyProposal, proposal_id)
    if proposal is None:
        raise SovereigntyError("no_proposal", "proposal not found")
    if PolicySource(proposal.source) is not PolicySource.FOUNDER:
        # Still only the Founder can apply. The source remains non-authoritative history.
        pass
    version = apply_founder_policy(
        db,
        actor_id=actor_id,
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


def add_family_member(
    db: Session,
    *,
    actor_id: UUID,
    principal_id: UUID,
    relationship: FamilyRelationship,
    display_name: str,
) -> FamilyMember:
    assert_founder_identity(actor_id)
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
    requested_duration: str = "once",
    consequences: str = "",
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
    request = FamilyApprovalRequest(
        owner_id=FOUNDER_USER_ID,
        principal_id=principal_id,
        capability_key=spec.key,
        resource=spec.resource,
        action=spec.action,
        scope="family",
        requested_duration=requested_duration,
        requested_data=requested_data or {},
        consequences=consequences,
        risk_tier=spec.risk_tier.value,
        status="pending",
        request_token_hash=_hash_token(raw_token),
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
        requested_duration=request.requested_duration,
        consequences=request.consequences,
        risk_tier=RiskTier(request.risk_tier),
    )


def decide_approval(
    db: Session,
    *,
    actor_id: UUID,
    request_id: UUID,
    mode: ApprovalMode,
    duration: timedelta | None = None,
    until: datetime | None = None,
    limits: dict | None = None,
    session_id: str | None = None,
) -> FamilyApprovalReceipt:
    assert_founder_identity(actor_id)
    request = db.get(FamilyApprovalRequest, request_id)
    if request is None or request.status != "pending":
        raise SovereigntyError("no_pending_request", "approval request is not pending")
    request.status = "decided"
    receipt = FamilyApprovalReceipt(
        owner_id=FOUNDER_USER_ID,
        request_id=request.id,
        decision=mode.value,
        receipt_token_hash=_hash_token(secrets.token_urlsafe(32)),
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
        expires = until
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
                limits=limits or {},
                issuer_id=FOUNDER_USER_ID,
                approval_receipt_id=receipt.id,
                session_id=session_id,
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
                issuer_id=FOUNDER_USER_ID,
                approval_receipt_id=receipt.id,
                blocked_future=True,
                created_at=_now(),
            )
        )
    db.flush()
    return receipt


def revoke_grant(db: Session, *, actor_id: UUID, grant_id: UUID) -> FamilyCapabilityGrant:
    assert_founder_identity(actor_id)
    grant = db.get(FamilyCapabilityGrant, grant_id)
    if grant is None:
        raise SovereigntyError("no_grant", "grant not found")
    grant.revoked_at = _now()
    db.flush()
    return grant


def authorize_capability(
    db: Session,
    *,
    principal_id: UUID,
    capability_key: str,
    consume: bool = False,
    now: datetime | None = None,
) -> AuthorizationResult:
    clock = now or _now()
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
    for grant in grants:
        if grant.revoked_at is not None:
            continue
        if grant.expires_at is not None and grant.expires_at <= clock:
            continue
        if grant.remaining_uses is not None and grant.remaining_uses <= 0:
            continue
        if consume and grant.remaining_uses is not None:
            grant.remaining_uses -= 1
            db.flush()
        return AuthorizationResult(AuthorizationVerdict.ALLOWED, "current grant matches exact capability", grant_id=grant.id)
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


def define_userai_boundary(db: Session, *, actor_id: UUID) -> UserAITenantBoundary:
    assert_founder_identity(actor_id)
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


def list_pending_approvals(db: Session, *, actor_id: UUID) -> list[FamilyApprovalRequest]:
    assert_founder_identity(actor_id)
    return (
        db.query(FamilyApprovalRequest)
        .filter_by(owner_id=FOUNDER_USER_ID, status="pending")
        .order_by(FamilyApprovalRequest.created_at.asc())
        .all()
    )
