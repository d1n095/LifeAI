"""Translates `app.agent_coordination.adapter_config.adapter_availability()` -- the EXISTING,
already-reviewed five-way supported/executable-found/credentials/enabled/dispatch-authorized
distinction (PR #87) -- into a `capability_records` observation. A pure translation layer:
this module reimplements none of `adapter_config`'s own logic and adds no second source of
truth for real-adapter availability.

Hard rule, mirroring `adapter_config`'s own doctrine exactly: an executable being found on
PATH and the founder having enabled it is still NOT verification. This function can therefore
only ever produce `configured_disabled`, `configured_unavailable`, or `unknown` -- never
`verified_available`. A caller who has observed a real successful dispatch must record that
fact itself, separately, via `record_capability_observation(..., status="verified_available",
success=True, verification_evidence_id=...)`."""

from uuid import UUID

from sqlalchemy.orm import Session

from app.agent_coordination.adapter_config import adapter_availability
from app.capability_reality.service import record_capability_observation
from app.models.capability_reality import CapabilityRecord

AGENT_DISPATCH_DOMAIN = "agent_dispatch"


def sync_agent_adapter_capability(db: Session, *, owner_id: UUID, provider_key: str) -> CapabilityRecord:
    """Reads the REAL, current local-machine availability for `provider_key` (e.g.
    `"claude-code"`, `"cursor-agent"`, `"codex"`) and records a capability observation for
    `capability_key=f"agent_dispatch.{provider_key}"`. Never sets `verified_available` --
    see module docstring."""

    availability = adapter_availability(provider_key)
    if not availability.supported:
        status = "unknown"
    elif not availability.enabled:
        status = "configured_disabled"
    else:
        # enabled=True here, regardless of executable_found -- both "enabled but the
        # executable is missing" and "enabled and the executable is genuinely present" are
        # equally NOT verification; only a real recorded successful dispatch is.
        status = "configured_unavailable"

    return record_capability_observation(
        db, owner_id=owner_id, capability_key=f"agent_dispatch.{provider_key}", domain=AGENT_DISPATCH_DOMAIN,
        status=status, status_reason=availability.reason, authority="deterministic_source",
        dependencies=[f"executable:{provider_key}"],
        provenance={"observed_via": "app.agent_coordination.adapter_config.adapter_availability", "provider_key": provider_key},
    )
