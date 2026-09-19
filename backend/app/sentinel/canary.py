"""Sentinel -- canary / honeypot foundation (Stage V2-D1).

A canary should never be touched in normal operation; a CANARY_TOUCHED event is therefore
treated by app.sentinel.service as an unconditional, high-confidence security signal,
independent of the rule registry (see service.py's record_event()).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from app.sentinel.types import CanaryResource, SecurityEvent, SecurityEventType, SecuritySeverity, SecuritySource, SecuritySubject, SecurityConfidence

_KNOWN_CANARY_KINDS = frozenset({"fake_secret", "fake_credential_file", "fake_vault_object", "unused_document_path"})

# Reject anything shaped like a real provider credential (Stripe/OpenAI/AWS/GitHub/PEM key
# prefixes) so a canary fixture can never be mistaken for -- or accidentally double as -- a
# real-looking secret that would trip push-protection scanning, exactly the incident this
# session already hit once with an unrelated test fixture.
_REAL_LOOKING_SECRET_SHAPES = re.compile(
    r"(sk-|sk_live|sk_test|ghp_|gho_|AKIA|-----BEGIN)", re.IGNORECASE
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def register_canary(*, owner_id: uuid.UUID, kind: str, subject_ref: str) -> CanaryResource:
    if kind not in _KNOWN_CANARY_KINDS:
        raise ValueError(f"unknown canary kind {kind!r}, must be one of {sorted(_KNOWN_CANARY_KINDS)}")
    if _REAL_LOOKING_SECRET_SHAPES.search(subject_ref):
        raise ValueError(
            "canary subject_ref must not be shaped like a real provider credential -- use an "
            "explicit synthetic fixture identifier (e.g. 'canary_fixture_<random>')"
        )
    return CanaryResource(canary_id=uuid.uuid4(), owner_id=owner_id, kind=kind, subject_ref=subject_ref)


def build_canary_touch_event(canary: CanaryResource, *, device_id: str, adapter_name: str = "canary") -> SecurityEvent:
    """The only event type this module ever constructs. Severity/confidence are fixed at
    CRITICAL/HIGH -- a canary touch is definitionally suspicious, there is no "benign canary
    touch" case for this constructor to leave open."""
    return SecurityEvent(
        event_id=uuid.uuid4(),
        event_type=SecurityEventType.CANARY_TOUCHED,
        severity=SecuritySeverity.CRITICAL,
        confidence=SecurityConfidence.HIGH,
        subject=SecuritySubject(
            owner_id=canary.owner_id, device_id=device_id, subject_kind="canary", subject_ref=canary.subject_ref
        ),
        source=SecuritySource(adapter_name=adapter_name, adapter_version="0.1.0"),
        occurred_at=_utcnow(),
        correlation_id=uuid.uuid4(),
        parent_event_id=None,
        details={"canary_kind": canary.kind},
    )
