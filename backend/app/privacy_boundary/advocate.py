"""MainAI V2 -- Privacy Boundary Engine: Local Privacy Advocate.

SERVER REQUEST != DISCLOSURE AUTHORITY. A central/external caller can only ask for an
AGGREGATE CATEGORY of already-egressed signal data -- never a specific record by id, owner,
conversation, or file. This is enforced two ways (defense in depth):

1. STRUCTURAL: CategoryAggregateRequest's fields cannot express "give me record X" at all --
   there is no id/owner_id/content field on the type, only domain/skill/purpose/time-window
   category selectors. A caller cannot construct a request for a specific record even if they
   wanted to; the type doesn't have a slot for it.
2. RUNTIME: even if a caller tries to smuggle a raw identifier through one of the category
   fields (e.g. domain="conversation:<uuid>"), evaluate_advocate_request() re-validates every
   field against the same closed vocabularies the pipeline itself uses and rejects anything
   that doesn't match a known category shape.

A "malicious server request" that tries to set its own permissive local-privacy-mode flag is
rejected by construction: CategoryAggregateRequest has no field that could set or override
the local TelemetryMode at all -- the caller supplies a request, evaluate_advocate_request()
supplies (from the LOCAL policy, never from the request) the mode that actually governs it.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.privacy_boundary.sanitize import sanitize_text
from app.privacy_boundary.types import OutboundPurpose, TelemetryMode


class DisclosureRequestRejected(RuntimeError):
    pass


@dataclass(frozen=True)
class CategoryAggregateRequest:
    """What a central/external caller is ALLOWED to ask for. No id/owner/content field
    exists on this type -- see module docstring point 1."""

    domain: str
    skill: str | None
    purpose: OutboundPurpose
    # NOTE: deliberately no `local_privacy_mode_override` field or anything resembling one --
    # this type structurally cannot express "use this mode instead of the local one."


@dataclass(frozen=True)
class AggregateResult:
    domain: str
    skill: str | None
    sample_count: int
    summary: dict


def evaluate_advocate_request(
    request: CategoryAggregateRequest,
    *,
    local_mode: TelemetryMode,
    allowed_purposes_for_mode: frozenset[OutboundPurpose],
) -> None:
    """Raises DisclosureRequestRejected if the request cannot be honored. `local_mode` and
    `allowed_purposes_for_mode` are ALWAYS supplied by the local caller from the device's own
    current policy state -- never read from `request` itself, so nothing on the request object
    can widen what's allowed."""
    if request.purpose not in allowed_purposes_for_mode:
        raise DisclosureRequestRejected(
            f"purpose {request.purpose.value} is not permitted at local telemetry mode {local_mode.value}"
        )
    if local_mode == TelemetryMode.OFF:
        raise DisclosureRequestRejected("local telemetry mode is OFF -- no aggregate requests are honored")
    # Domain/skill are free strings on the request only because a real deployment would
    # validate them against a known domain taxonomy; for this foundation, presence of a
    # non-empty category string is the only shape check available without that taxonomy.
    if not request.domain:
        raise DisclosureRequestRejected("domain category is required")
    # Runtime re-validation (defense in depth alongside the structural check in point 1):
    # a caller smuggling a raw identifier through the domain/skill category fields (e.g.
    # domain="conversation:<uuid>" or an embedded file path) is caught here even though the
    # type itself would have accepted the string.
    for label, value in (("domain", request.domain), ("skill", request.skill)):
        if value is None:
            continue
        _, categories = sanitize_text(value)
        if categories:
            raise DisclosureRequestRejected(f"{label} category contains a raw identifier-shaped value ({categories[0]}), not a category name")
