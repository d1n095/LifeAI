"""Sentinel -- defensive action requests (Stage V2-D4).

Sentinel may REQUEST a defensive action; it must never execute one. There is no
execute()/apply() function anywhere in this module or this package -- the only thing built
here is a DefensiveActionRequest (pure data). A future caller is responsible for turning
that into a Guardian ContainmentRequest and evaluating it through Guardian's own policy;
that composition deliberately does not live in this package (see types.py module
docstring and test_sentinel_foundation.py's structural test).
"""

from __future__ import annotations

import uuid

from app.sentinel.types import (
    CONFIDENCE_ORDER,
    SEVERITY_ORDER,
    DefensiveAction,
    DefensiveActionRequest,
    PreauthorizedDefense,
    SecurityIncident,
)

_REQUIRED_PREAUTH_FIELDS = (
    "allowed_action",
    "scope_hint",
    "min_severity",
    "min_confidence",
    "time_to_damage_class",
    "owner_response_timeout_seconds",
    "max_containment_duration_seconds",
    "rollback_conditions",
)


def require_bounded_preauthorization(preauth: PreauthorizedDefense) -> None:
    """DEFENSIVE AUTONOMY != GENERAL AUTONOMY: every field must be a concrete, non-wildcard
    value. `scope_hint` may not be a bare "*"/"all"/"any" wildcard, and both duration fields
    must be positive and finite -- there is no way to construct an unbounded standing grant
    through this function."""
    for f in _REQUIRED_PREAUTH_FIELDS:
        if getattr(preauth, f) is None:
            raise ValueError(f"PreauthorizedDefense.{f} must not be None -- no unbounded grant is allowed")
    if preauth.scope_hint.strip().lower() in {"*", "all", "any", "everything"}:
        raise ValueError("PreauthorizedDefense.scope_hint must not be a wildcard -- scope must be concrete")
    if preauth.owner_response_timeout_seconds <= 0:
        raise ValueError("owner_response_timeout_seconds must be a positive, bounded duration")
    if preauth.max_containment_duration_seconds <= 0:
        raise ValueError("max_containment_duration_seconds must be a positive, bounded duration")
    if not preauth.rollback_conditions.strip():
        raise ValueError("rollback_conditions must be explicit -- an unbounded grant with no release path is not allowed")


def preauthorization_covers(preauth: PreauthorizedDefense, incident: SecurityIncident, *, action: DefensiveAction) -> bool:
    """Whether a standing preauthorization is strong/specific enough to cover this incident.
    Every threshold must be met -- action identity, and severity/confidence at or above the
    preauthorization's own minimums."""
    if preauth.allowed_action != action:
        return False
    if SEVERITY_ORDER[incident.severity] < SEVERITY_ORDER[preauth.min_severity]:
        return False
    if CONFIDENCE_ORDER[incident.confidence] < CONFIDENCE_ORDER[preauth.min_confidence]:
        return False
    return True


def build_defensive_action_request(
    incident: SecurityIncident,
    *,
    action: DefensiveAction,
    scope_hint: str,
    reason: str,
    preauthorized_by: PreauthorizedDefense | None = None,
) -> DefensiveActionRequest:
    """Construct a DefensiveActionRequest for a given incident. If `preauthorized_by` is
    given, it must actually cover this incident+action (checked here, not assumed) --
    attaching a preauthorization that doesn't cover the request would be a silent authority
    mismatch bug of exactly the "evidence exists != evidence supports claim" shape found
    repeatedly elsewhere in this codebase."""
    if preauthorized_by is not None:
        require_bounded_preauthorization(preauthorized_by)
        if not preauthorization_covers(preauthorized_by, incident, action=action):
            raise ValueError(
                "preauthorized_by does not cover this incident/action -- its thresholds are not met, "
                "so this request must be built without it (falls back to owner-response-required path)"
            )
    return DefensiveActionRequest(
        request_id=uuid.uuid4(),
        action=action,
        scope_hint=scope_hint,
        threat_class=incident.threat_class,
        severity=incident.severity,
        confidence=incident.confidence,
        incident_id=incident.incident_id,
        owner_id=incident.owner_id,
        reason=reason,
        preauthorized_by=preauthorized_by,
    )
