"""Fail-closed authorization contracts for the future Personal Recall service boundary."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.revoked_access_token import RevokedAccessToken
from app.models.user import User
from app.personal_recall.types import PersonalKnowledgeItem, SourceType


class RecallAuthorizationError(PermissionError):
    pass


class DisclosureLevel(str, Enum):
    NONE = "none"
    METADATA = "metadata"
    SNIPPET = "snippet"


class DisclosureCheckResult(str, Enum):
    VALID = "VALID"
    STALE = "STALE"
    REVOKED = "REVOKED"
    DELETED = "DELETED"
    SUPERSEDED = "SUPERSEDED"
    OWNER_MISMATCH = "OWNER_MISMATCH"
    CONTENT_MISMATCH = "CONTENT_MISMATCH"


@dataclass(frozen=True)
class DisclosureEvidence:
    source_id: str
    owner_id: str
    canonical_generation: int | None
    retrieved_generation: int | None
    content_identity: str | None
    lifecycle_state: str
    checked_at: datetime
    result: DisclosureCheckResult


@dataclass(frozen=True)
class IdentifierScope:
    """An explicit scope. Empty and not unrestricted means no authority."""

    identifiers: frozenset[str] = field(default_factory=frozenset)
    unrestricted: bool = False
    allow_unscoped: bool = False

    def allows(self, value: str | None) -> bool:
        if value is None:
            return self.allow_unscoped
        return self.unrestricted or value in self.identifiers


@dataclass(frozen=True, repr=False)
class RecallAuthorizationContext:
    authorization_id: str
    authorization_version: str
    owner_id: str
    session_user_id: str
    session_jti: str
    session_issued_at: datetime
    expires_at: datetime
    allowed_source_types: frozenset[SourceType]
    project_scope: IdentifierScope
    conversation_scope: IdentifierScope
    allow_current: bool
    allow_historical: bool
    disclosure_level: DisclosureLevel
    allow_locator_open: bool


@dataclass(frozen=True)
class AuthorizationReceipt:
    authorization_id: str
    authorization_version: str
    owner_id: str
    session_jti: str
    checked_at: datetime
    disclosure_level: DisclosureLevel
    result_item_ids: tuple[str, ...]
    disclosure_evidence: tuple[DisclosureEvidence, ...] = ()


class RecallAuthorityResolver(Protocol):
    def resolve(self, presented: RecallAuthorizationContext, *, now: datetime) -> RecallAuthorizationContext | None: ...


class SQLAlchemyRecallAuthorityResolver:
    """Revalidates canonical account/JTI state; never treats the presented grant as truth."""

    def __init__(self, db: Session, *, authenticated_user_id: uuid.UUID | str, grant_loader):
        self.db = db
        self.authenticated_user_id = uuid.UUID(str(authenticated_user_id))
        self.grant_loader = grant_loader

    def resolve(self, presented: RecallAuthorizationContext, *, now: datetime) -> RecallAuthorizationContext | None:
        if presented.owner_id != presented.session_user_id:
            return None
        try:
            owner = uuid.UUID(presented.owner_id)
        except ValueError:
            return None
        if owner != self.authenticated_user_id:
            return None
        user = self.db.execute(select(User).where(User.id == owner, User.is_active.is_(True)).execution_options(populate_existing=True)).scalar_one_or_none()
        if user is None:
            return None
        if self.db.execute(select(RevokedAccessToken.jti).where(RevokedAccessToken.jti == presented.session_jti)).scalar_one_or_none():
            return None
        issued = _aware(presented.session_issued_at)
        valid_after = _aware(user.sessions_valid_after)
        if issued <= valid_after or _aware(presented.expires_at) <= _aware(now):
            return None
        current = self.grant_loader(presented)
        if current is None or current.owner_id != str(user.id) or current.session_user_id != str(user.id):
            return None
        return current


def require_fresh_authority(
    presented: RecallAuthorizationContext,
    *,
    resolver: RecallAuthorityResolver,
    now: datetime | None = None,
) -> RecallAuthorizationContext:
    checked_at = _aware(now or datetime.now(timezone.utc))
    current = resolver.resolve(presented, now=checked_at)
    if current is None:
        raise RecallAuthorizationError("recall authority is missing or revoked")
    if current.authorization_id != presented.authorization_id or current.session_jti != presented.session_jti:
        raise RecallAuthorizationError("authorization identity changed")
    if current.owner_id != current.session_user_id or current.owner_id != presented.owner_id:
        raise RecallAuthorizationError("session identity does not own this recall authority")
    if _aware(current.expires_at) <= checked_at:
        raise RecallAuthorizationError("recall authority expired")
    return current


def require_item_authority(context: RecallAuthorizationContext, item: PersonalKnowledgeItem) -> None:
    if item.owner_id != context.owner_id:
        raise RecallAuthorizationError("foreign-owner recall item")
    if item.source_type not in context.allowed_source_types:
        raise RecallAuthorizationError("source class is outside recall authority")
    if not context.project_scope.allows(item.project_id):
        raise RecallAuthorizationError("project is outside recall authority")
    if item.source_type in (SourceType.CONVERSATION, SourceType.PREVIOUS_ANSWER) and not context.conversation_scope.allows(item.conversation_id):
        raise RecallAuthorizationError("conversation is outside recall authority")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
