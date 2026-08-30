"""Task-scoped authority envelope helpers (T3). Authority is minimal, explicit, revocable, time-bounded."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.models.workforce import WorkforceAssignment


class AuthorityEnvelopeError(Exception):
    pass


@dataclass(frozen=True)
class TaskScopedAuthority:
    allowed_read_paths: tuple[str, ...] = ()
    allowed_write_paths: tuple[str, ...] = ()
    allowed_tool_classes: tuple[str, ...] = ()
    allowed_network_destinations: tuple[str, ...] = ()
    allowed_project_ids: tuple[str, ...] = ()
    spend_ceiling_usd: float | None = None
    allow_execution_effects: bool = False
    expires_at: datetime | None = None

    def as_dict(self) -> dict:
        return {
            "allowed_read_paths": list(self.allowed_read_paths),
            "allowed_write_paths": list(self.allowed_write_paths),
            "allowed_tool_classes": list(self.allowed_tool_classes),
            "allowed_network_destinations": list(self.allowed_network_destinations),
            "allowed_project_ids": list(self.allowed_project_ids),
            "spend_ceiling_usd": self.spend_ceiling_usd,
            "allow_execution_effects": self.allow_execution_effects,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class AuthorityCheckResult:
    live: bool
    reasons: list[str] = field(default_factory=list)


def assignment_authority_is_live(assignment: WorkforceAssignment, *, now: datetime | None = None) -> AuthorityCheckResult:
    """Live check — expired/revoked assignments grant no authority."""
    now = now or datetime.utcnow()
    reasons: list[str] = []
    if assignment.revoked_at is not None:
        reasons.append("revoked")
    if assignment.expires_at is not None and assignment.expires_at <= now:
        reasons.append("expired")
    if assignment.status in ("revoked", "expired", "cancelled", "superseded"):
        reasons.append(f"status={assignment.status}")
    return AuthorityCheckResult(live=not reasons, reasons=reasons)


def require_live_assignment_authority(assignment: WorkforceAssignment, *, now: datetime | None = None) -> None:
    check = assignment_authority_is_live(assignment, now=now)
    if not check.live:
        raise AuthorityEnvelopeError(f"assignment {assignment.id} authority not live: {', '.join(check.reasons)}")


def revoke_assignment_authority(
    assignment: WorkforceAssignment,
    *,
    reason: str,
    now: datetime | None = None,
) -> WorkforceAssignment:
    now = now or datetime.utcnow()
    assignment.revoked_at = now
    assignment.revocation_reason = reason
    assignment.status = "revoked"
    assignment.updated_at = now
    return assignment


def tool_class_allowed(assignment: WorkforceAssignment, tool_class: str) -> bool:
    allowed = list(assignment.allowed_tool_classes or [])
    if not allowed:
        return False
    return tool_class in allowed


def path_allowed(assignment: WorkforceAssignment, path: str, *, write: bool) -> bool:
    prefixes = list(assignment.allowed_write_paths if write else assignment.allowed_read_paths) or []
    if not prefixes:
        return False
    normalized = path.lstrip("./")
    for prefix in prefixes:
        p = str(prefix).rstrip("*")
        if normalized == p.rstrip("/") or normalized.startswith(p.rstrip("/") + "/") or (
            str(prefix).endswith("**") and normalized.startswith(p.rstrip("*").rstrip("/"))
        ):
            return True
        if normalized.startswith(str(prefix)):
            return True
    return False
