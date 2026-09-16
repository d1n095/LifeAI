"""ExternalProviderLease + provider usage/failover (Milestone 3).

PROVIDER FAILURE != AUTHORITY WIDENING: select_failover_provider() never returns a
candidate with broader capabilities than the original lease requested -- a failover
candidate happening to have more capabilities is still only ever granted a lease scoped to
what the original task needed.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.dev_director.types import (
    ExternalProviderLease,
    NoAvailableProvider,
    ProviderCapabilityProfile,
    ProviderUsageState,
)

_EXPECTED_LEASE_FIELDS = frozenset(
    {
        "lease_id", "provider_identity", "task_ref", "workspace_ref", "branch", "allowed_tools",
        "allowed_files", "expires_at", "budget_usd", "network_scope", "disclosure_scope", "authority_scope",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_external_provider_lease(
    *, provider_identity: str, task_ref: uuid.UUID | None, workspace_ref: str, branch: str,
    allowed_tools: tuple[str, ...], allowed_files: tuple[str, ...], ttl_seconds: int,
    budget_usd: float | None = None, network_scope: tuple[str, ...] = (), disclosure_scope: str = "none",
    authority_scope: tuple[str, ...] = (),
) -> ExternalProviderLease:
    from datetime import timedelta

    return ExternalProviderLease(
        lease_id=uuid.uuid4(), provider_identity=provider_identity, task_ref=task_ref,
        workspace_ref=workspace_ref, branch=branch, allowed_tools=allowed_tools, allowed_files=allowed_files,
        expires_at=_utcnow() + timedelta(seconds=ttl_seconds), budget_usd=budget_usd,
        network_scope=network_scope, disclosure_scope=disclosure_scope, authority_scope=authority_scope,
    )


def select_failover_provider(
    candidates: tuple[ProviderCapabilityProfile, ...], *, excluding: tuple[str, ...] = (),
    required_capabilities: tuple[str, ...] = (), examiner_role: bool = False,
) -> ProviderCapabilityProfile | NoAvailableProvider:
    eligible = [
        c for c in candidates
        if c.provider_identity not in excluding
        and c.usage_state == ProviderUsageState.AVAILABLE
        and set(required_capabilities).issubset(set(c.capabilities))
        and (not examiner_role or c.is_examiner_eligible)
    ]
    if not eligible:
        return NoAvailableProvider(reason="no candidate is AVAILABLE, excluded-clear, and capability-matching")
    eligible.sort(key=lambda c: (c.recent_failure_rate, c.provider_identity))
    return eligible[0]


def lease_scoped_to_original_request(
    selected: ProviderCapabilityProfile, *, original_allowed_tools: tuple[str, ...], original_allowed_files: tuple[str, ...], **lease_kwargs,
) -> ExternalProviderLease:
    """PROVIDER FAILURE != AUTHORITY WIDENING, enforced structurally: builds a lease for
    `selected` using ONLY the ORIGINAL request's allowed_tools/allowed_files, regardless of
    how much broader `selected.capabilities` might be. A failover candidate with more
    capabilities than the original builder never gets a wider lease as a side effect."""
    return new_external_provider_lease(
        provider_identity=selected.provider_identity, allowed_tools=original_allowed_tools,
        allowed_files=original_allowed_files, **lease_kwargs,
    )
