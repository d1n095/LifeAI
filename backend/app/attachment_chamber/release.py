"""Attachment Chamber -- release policy (Milestone 6).

RELEASED FILE != TRUSTED EXECUTABLE: releasing an attachment from quarantine never itself
grants execution/network/Vault/provider-disclosure authority -- those are separate,
already-existing systems (app.guardian for authority decisions, app.egress_policy for
provider disclosure) this module references only conceptually, never imports.

Uses the SAME "no real policy supplied -> raise, never default-allow" seam already
established by app.operating_shell.risk.evaluate_action_authority()/PolicyNotWiredError --
not a new pattern.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from app.attachment_chamber.types import AttachmentChamberError, AttachmentIdentity


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReleaseLevel(str, Enum):
    PREVIEW = "PREVIEW"
    PARSE = "PARSE"
    LOCAL_USE = "LOCAL_USE"
    EXTERNAL_PROVIDER_DISCLOSURE = "EXTERNAL_PROVIDER_DISCLOSURE"


class ReleasePolicyNotWiredError(AttachmentChamberError):
    """ACTION REQUEST != AUTHORITY, applied to attachment release. Raised whenever no real
    AttachmentReleasePolicy is supplied -- NEVER a silent default-allow."""


class ReleaseDecisionValue(str):
    """String subclass, same shape as app.operating_shell.risk.PolicyDecision -- this seam
    intentionally makes no closed claim about what a real future policy might return."""


ALLOWED = ReleaseDecisionValue("allowed")
DENIED = ReleaseDecisionValue("denied")


class AttachmentReleasePolicy(Protocol):
    """The seam a future Guardian-backed (for LOCAL_USE authority) or egress_policy-backed
    (for EXTERNAL_PROVIDER_DISCLOSURE) integration implements. This module supplies NO real
    implementation."""

    def evaluate(self, identity: AttachmentIdentity, level: ReleaseLevel) -> ReleaseDecisionValue: ...


@dataclass(frozen=True)
class ReleaseDecision:
    attachment_id: uuid.UUID
    level: ReleaseLevel
    decision: ReleaseDecisionValue
    decided_at: datetime


def release_attachment(identity: AttachmentIdentity, *, level: ReleaseLevel, policy: AttachmentReleasePolicy | None) -> ReleaseDecision:
    """RELEASE FILE != TRUSTED EXECUTABLE. Raises ReleasePolicyNotWiredError if `policy` is
    None -- never a silent default-allow. A real policy (a future Guardian/egress_policy-
    backed one) must be supplied explicitly by the caller."""
    if policy is None:
        raise ReleasePolicyNotWiredError(
            f"no AttachmentReleasePolicy supplied for attachment {identity.attachment_id} at level {level.value} "
            "-- ACTION REQUEST != AUTHORITY, this seam refuses to default-allow"
        )
    decision = policy.evaluate(identity, level)
    return ReleaseDecision(attachment_id=identity.attachment_id, level=level, decision=decision, decided_at=_utcnow())
