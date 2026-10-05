"""Founder sovereignty + family delegation vocabulary.

FAMILY MEMBER != FOUNDER. ADMIN != FOUNDER. SUPPORT != FOUNDER.
DELEGATE != FOUNDER. AI != FOUNDER.

AI MAY PROPOSE POLICY CHANGE != AI MAY APPLY POLICY CHANGE.
ONE_TIME_APPROVAL != PERMANENT_PERMISSION.
CAPABILITY != FOUNDER_AUTHORITY.
This package never grants merge, deploy, Recall, or database-superuser authority.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID


class ActorKind(str, enum.Enum):
    FOUNDER = "founder"
    FAMILY_MEMBER = "family_member"
    ADMIN = "admin"
    SUPPORT = "support"
    DELEGATE = "delegate"
    AI = "ai"
    REMOTE_AGENT = "remote_agent"


class PolicyClass(str, enum.Enum):
    KERNEL_SECURITY_INVARIANT = "kernel_security_invariant"
    FOUNDER_POLICY = "founder_policy"
    RUNTIME_PREFERENCE = "runtime_preference"


class PolicySource(str, enum.Enum):
    FOUNDER = "founder"
    AI_PROPOSAL = "ai_proposal"
    REMOTE_AGENT = "remote_agent"
    BUG_REPORT = "bug_report"
    PROMPT_INJECTION = "prompt_injection"
    MODEL_OUTPUT = "model_output"


class RiskTier(str, enum.Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    FOUNDER_ONLY = "founder_only"


class ApprovalMode(str, enum.Enum):
    ALLOW_ONCE = "allow_once"
    ALLOW_FOR_DURATION = "allow_for_duration"
    ALLOW_UNTIL_DATE = "allow_until_date"
    ALWAYS_ALLOW = "always_allow"
    DENY = "deny"
    DENY_AND_BLOCK = "deny_and_block"
    REVIEW_EXACT_ACTION = "review_exact_action"


class FamilyRelationship(str, enum.Enum):
    PARTNER = "partner"
    CHILD = "child"
    OTHER = "other"


class TenantKind(str, enum.Enum):
    FOUNDER_MAINAI = "founder_mainai"
    USERAI_PERSONAL = "userai_personal"


class AuthorizationVerdict(str, enum.Enum):
    ALLOWED = "allowed"
    DENIED_NOT_FOUNDER = "denied_not_founder"
    DENIED_WRONG_PRINCIPAL = "denied_wrong_principal"
    DENIED_NO_GRANT = "denied_no_grant"
    DENIED_REVOKED = "denied_revoked"
    DENIED_EXPIRED = "denied_expired"
    DENIED_EXHAUSTED = "denied_exhausted"
    DENIED_BLOCKED = "denied_blocked"
    DENIED_FOUNDER_ONLY = "denied_founder_only"
    DENIED_IMPLIED_CAPABILITY = "denied_implied_capability"
    DENIED_OLD_RECEIPT = "denied_old_receipt"
    DENIED_COPIED_TOKEN = "denied_copied_token"
    DENIED_KERNEL = "denied_kernel"
    DENIED_SELF_UNLOCK = "denied_self_unlock"
    DENIED_NON_POLICY_SOURCE = "denied_non_policy_source"
    DENIED_RESOURCE = "denied_resource"
    DENIED_SCOPE = "denied_scope"
    DENIED_LIMITS = "denied_limits"
    DENIED_DATA = "denied_data"
    DENIED_STEP_UP = "denied_step_up"


class StepUpPurpose(str, enum.Enum):
    POLICY_ROLLBACK = "policy_rollback"
    WORKFLOW_UNLOCK = "workflow_unlock"
    PERMANENT_HIGH_RISK_DELEGATION = "permanent_high_risk_delegation"
    FOUNDER_ONLY_CAPABILITY_CHANGE = "founder_only_capability_change"


class SovereigntyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class CapabilitySpec:
    key: str
    resource: str
    action: str
    risk_tier: RiskTier
    description: str = ""


@dataclass(frozen=True)
class ApprovalContext:
    who_principal_id: UUID
    who_label: str
    capability_key: str
    resource: str
    action: str
    scope: str
    requested_data: dict
    requested_limits: dict
    requested_duration: str
    consequences: str
    risk_tier: RiskTier
    snapshot_hash: str
    session_id: str | None = None
    device_id: str | None = None


@dataclass(frozen=True)
class AuthorizationResult:
    verdict: AuthorizationVerdict
    reason: str
    grant_id: UUID | None = None
    receipt_id: UUID | None = None

    @property
    def allowed(self) -> bool:
        return self.verdict is AuthorizationVerdict.ALLOWED


@dataclass(frozen=True)
class PolicyVersionView:
    version_number: int
    policy_class: PolicyClass
    policy_key: str
    payload: dict
    reason: str
    created_at: datetime
    previous_version_number: int | None = None


@dataclass
class PolicyProposal:
    policy_key: str
    payload: dict
    source: PolicySource
    notes: tuple[str, ...] = field(default_factory=tuple)
