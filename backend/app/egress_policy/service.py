"""Life Vault / External-AI Egress Control -- the default-deny policy gate every outbound
provider call must pass through before content leaves the process. See
docs/LIFE_VAULT_EGRESS_CONTROL.md for the full threat model and architecture map this
implements against.

DEFAULT EGRESS = DENY. MODEL_REQUESTED_CONTEXT != AUTHORIZED_EGRESS_CONTEXT. RETRIEVAL
AUTHORITY != EGRESS AUTHORITY. Retrieved/external content is DATA, never AUTHORITY -- nothing
this gate evaluates can expand what it is itself authorized to disclose. This is the same
family of invariant this codebase already established for execution authority
(PROPOSED_SCOPE != AUTHORIZED_SCOPE, migration 0057; MODEL REQUEST != AUTHORIZATION,
app.mainai_execution.recovery_takeover), extended here to information disclosure.

Wired first into the `provider_planning`/Safe Planner boundary only (the narrowest,
best-understood, already-partially-redacted call path -- see `safe_provider_prompt()`,
`app/safe_planner/service.py`). Deliberately NOT wired into `app/routers/chat.py` or the RAG
embedding call sites yet -- those are real, larger, separately-tracked gaps (V1/V2 in the
blocker map), out of scope for this foundation."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.development_operator import service as operator
from app.models.provider_disclosure import ProviderDisclosureEvent

# Content matching any of these markers is refused OUTRIGHT -- the whole call denied, never
# partial-redacted-and-sent, matching safe_provider_prompt()'s own fail-closed posture (refuse
# the field, never guess how much of it is safe to keep).
_NEVER_EGRESS_MARKERS = (
    "NEVER_EGRESS:",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
)


class EgressDeniedError(RuntimeError):
    """Raised when the egress gate denies a call outright -- severe content, or a malformed/
    incomplete request. The caller must treat this exactly like any other hard authority
    boundary: never retry with the same content, never silently continue without the provider
    call, never catch-and-ignore. A ProviderDisclosureEvent row recording the denial is
    already durably committed to the session (flushed, not yet committed by this function --
    see enforce_egress_policy()'s own docstring) before this is ever raised."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"egress denied: {reason}")


def _hash_payload(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _contains_never_egress_marker(payload: Any) -> bool:
    if isinstance(payload, str):
        return any(marker in payload for marker in _NEVER_EGRESS_MARKERS)
    if isinstance(payload, dict):
        return any(_contains_never_egress_marker(value) for value in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_contains_never_egress_marker(item) for item in payload)
    return False


def _record(
    db: Session,
    *,
    owner_id: uuid.UUID,
    provider: str,
    model: str,
    purpose: str,
    requested_by: str,
    task_id: uuid.UUID | None,
    goal_id: uuid.UUID | None,
    job_id: uuid.UUID | None,
    decision: str,
    reason: str,
    redaction_categories: list[str],
    attempted_hash: str,
    sent_hash: str | None,
) -> ProviderDisclosureEvent:
    event = ProviderDisclosureEvent(
        owner_id=owner_id,
        provider=provider,
        model=model,
        purpose=purpose,
        requested_by=requested_by,
        task_id=task_id,
        goal_id=goal_id,
        job_id=job_id,
        decision=decision,
        reason=reason,
        redaction_categories=redaction_categories,
        attempted_content_hash=attempted_hash,
        sent_content_hash=sent_hash,
    )
    db.add(event)
    db.flush()
    return event


def enforce_egress_policy(
    db: Session,
    *,
    owner_id: uuid.UUID,
    provider: str,
    model: str,
    purpose: str,
    requested_by: str,
    payload: Any,
    task_id: uuid.UUID | None = None,
    goal_id: uuid.UUID | None = None,
    job_id: uuid.UUID | None = None,
) -> Any:
    """THE enforcement point: every outbound provider call this gate covers must pass its
    content through this function before the provider is ever invoked. Returns the sanitized
    payload actually safe to send. Raises EgressDeniedError (never returns a value in that
    case) if the call is denied outright.

    Records exactly one ProviderDisclosureEvent for every call -- allowed or denied -- via
    `db.flush()` (not `db.commit()` -- the caller's own transaction boundary decides when
    that becomes durable, same convention as every other write function in this codebase)
    before returning or raising, so a denied call is just as durably auditable as an allowed
    one.

    STRUCTURALLY stateless / never memoized: every call re-evaluates the CURRENT payload from
    scratch. A retry, an idempotent replay, or a provider switch (A -> B) can never inherit an
    earlier call's decision -- there is no cache, no lookup-by-prior-hash, nothing here to
    bypass. This is deliberate, not an oversight: the adversarial test suite proves exactly
    this (retry/idempotency and provider-switch cannot skip re-evaluation)."""
    attempted_hash = _hash_payload(payload)

    if not provider or not model or not purpose or not requested_by:
        reason = "incomplete egress request: provider/model/purpose/requested_by are all required"
        _record(
            db,
            owner_id=owner_id,
            provider=provider or "unknown",
            model=model or "unknown",
            purpose=purpose or "unknown",
            requested_by=requested_by or "unknown",
            task_id=task_id,
            goal_id=goal_id,
            job_id=job_id,
            decision="denied",
            reason=reason,
            redaction_categories=[],
            attempted_hash=attempted_hash,
            sent_hash=None,
        )
        raise EgressDeniedError(reason)

    if _contains_never_egress_marker(payload):
        reason = "payload contains a NEVER_EGRESS-marked field or a private-key-shaped block -- refusing the whole call"
        _record(
            db,
            owner_id=owner_id,
            provider=provider,
            model=model,
            purpose=purpose,
            requested_by=requested_by,
            task_id=task_id,
            goal_id=goal_id,
            job_id=job_id,
            decision="denied",
            reason=reason,
            redaction_categories=["never_egress_marker"],
            attempted_hash=attempted_hash,
            sent_hash=None,
        )
        raise EgressDeniedError(reason)

    redacted = operator._redact_value(payload)
    redaction_categories = ["secret_pattern"] if redacted != payload else []
    sent_hash = _hash_payload(redacted)

    _record(
        db,
        owner_id=owner_id,
        provider=provider,
        model=model,
        purpose=purpose,
        requested_by=requested_by,
        task_id=task_id,
        goal_id=goal_id,
        job_id=job_id,
        decision="allowed",
        reason="passed egress policy" if not redaction_categories else "passed egress policy after secret-pattern redaction",
        redaction_categories=redaction_categories,
        attempted_hash=attempted_hash,
        sent_hash=sent_hash,
    )
    return redacted
