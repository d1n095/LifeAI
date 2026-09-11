"""Progressive Hydration / Fast Restore (MainAI V2, Stage V2-H4).

MainAI becomes usable once PRIORITY_0+1 components are restored, independent of whether
PRIORITY_2/3 have finished. VAULT is NEVER included in a tier-batch restore regardless of
what restore_priority a manifest claims for it -- unlocking Vault is always a separate,
explicit, owner-authorized action (see unlock_vault()). Restore is resumable/idempotent:
re-processing an already-completed component_id is always a no-op.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from app.sovereign_identity import decrypt_with_dek

from app.life_recovery.types import (
    CRITICAL_TIERS,
    ComponentType,
    HydrationError,
    HydrationProgress,
    LifeImageComponent,
    RestoreTier,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_hydration_progress(*, owner_id: uuid.UUID) -> HydrationProgress:
    return HydrationProgress(owner_id=owner_id)


def restore_component(
    progress: HydrationProgress,
    component: LifeImageComponent,
    *,
    dek: bytes,
) -> bytes | None:
    """Idempotent: if component_id is already in completed_component_ids, this is a no-op
    that returns None WITHOUT re-decrypting (the caller already has the plaintext from the
    first successful call, or does not need it again on a resume). Vault components are
    REJECTED here unconditionally -- see unlock_vault() for the only real path."""
    if component.component_type == ComponentType.VAULT:
        raise HydrationError(
            "restore_component() never restores VAULT components -- Vault must be restored via "
            "the separate, explicit unlock_vault() call (restoring MainAI essentials must never "
            "automatically unlock Vault)"
        )
    if component.component_id in progress.completed_component_ids:
        return None
    plaintext = decrypt_with_dek(component.envelope, dek=dek, expected_key_version=component.key_version)
    progress.completed_component_ids.add(component.component_id)
    progress.updated_at = _utcnow()
    return plaintext


def mark_tier_complete_if_ready(
    progress: HydrationProgress, *, tier: RestoreTier, components_in_tier: tuple[LifeImageComponent, ...]
) -> None:
    """A tier is complete only when every one of its (non-Vault) components' component_id is
    present in completed_component_ids -- never marked complete just because the caller says
    so."""
    restorable = [c for c in components_in_tier if c.component_type != ComponentType.VAULT]
    if restorable and all(c.component_id in progress.completed_component_ids for c in restorable):
        progress.completed_tiers.add(tier)
        progress.updated_at = _utcnow()


def restore_tier(
    progress: HydrationProgress,
    *,
    tier: RestoreTier,
    components_in_tier: tuple[LifeImageComponent, ...],
    dek_for_component: Callable[[LifeImageComponent], bytes],
) -> HydrationProgress:
    """Batch-restores every non-VAULT component in `components_in_tier`
    (VAULT_ACCESS != NORMAL_MEMORY_ACCESS: silently skipped here, never auto-unlocked --
    see restore_component()'s hard rejection, which this loop avoids triggering by
    filtering VAULT out up front rather than relying solely on the exception)."""
    for component in components_in_tier:
        if component.component_type == ComponentType.VAULT:
            continue
        if component.component_id in progress.completed_component_ids:
            continue
        dek = dek_for_component(component)
        restore_component(progress, component, dek=dek)
    mark_tier_complete_if_ready(progress, tier=tier, components_in_tier=components_in_tier)
    return progress


def unlock_vault(
    progress: HydrationProgress,
    vault_component: LifeImageComponent,
    *,
    dek: bytes,
    owner_authorized: bool,
) -> bytes:
    """The ONLY path that may decrypt a VAULT component. Requires explicit
    `owner_authorized=True` from the caller (a real integration would require an actual
    OWNER_ROOT/OWNER_CONFIRMED proof check here -- this foundation keeps the boolean seam
    explicit rather than silently defaulting to allowed)."""
    if vault_component.component_type != ComponentType.VAULT:
        raise HydrationError(f"unlock_vault() called with a non-VAULT component: {vault_component.component_type.value}")
    if not owner_authorized:
        raise HydrationError("unlock_vault() requires explicit owner authorization -- Vault is never unlocked implicitly")
    plaintext = decrypt_with_dek(vault_component.envelope, dek=dek, expected_key_version=vault_component.key_version)
    progress.completed_component_ids.add(vault_component.component_id)
    progress.updated_at = _utcnow()
    return plaintext


def get_continuity_summary(
    progress: HydrationProgress, components_by_tier: dict[RestoreTier, tuple[LifeImageComponent, ...]]
) -> tuple[ComponentType, ...]:
    """"What were we doing?" -- returns the ComponentTypes available from PRIORITY_0+1 once
    critical restore is complete. This foundation layer returns WHICH restored components a
    real MainAI answer-generation layer could draw on, not a fabricated answer itself --
    there is no real memory content at this abstraction level to summarize. Raises
    HydrationError if called before tier 0+1 completion; never silently falls back to
    tier 2/3 data."""
    if not progress.is_mainai_usable:
        raise HydrationError(
            "critical restore (PRIORITY_0 + PRIORITY_1) is not yet complete -- continuity summary is unavailable"
        )
    available: list[ComponentType] = []
    for tier in CRITICAL_TIERS:
        for component in components_by_tier.get(tier, ()):
            if component.component_id in progress.completed_component_ids:
                available.append(component.component_type)
    return tuple(available)


def to_snapshot(progress: HydrationProgress) -> dict:
    return {
        "owner_id": str(progress.owner_id),
        "completed_component_ids": sorted(str(cid) for cid in progress.completed_component_ids),
        "completed_tiers": sorted(t.value for t in progress.completed_tiers),
        "started_at": progress.started_at.isoformat(),
        "updated_at": progress.updated_at.isoformat(),
    }


def from_snapshot(snapshot: dict) -> HydrationProgress:
    return HydrationProgress(
        owner_id=uuid.UUID(snapshot["owner_id"]),
        completed_component_ids={uuid.UUID(cid) for cid in snapshot["completed_component_ids"]},
        completed_tiers={RestoreTier(t) for t in snapshot["completed_tiers"]},
        started_at=datetime.fromisoformat(snapshot["started_at"]),
        updated_at=datetime.fromisoformat(snapshot["updated_at"]),
    )
