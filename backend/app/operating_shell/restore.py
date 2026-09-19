"""Workspace restore planner (MainAI V2, Stage V2-I7).

Does NOT claim RESTORABLE if any referenced resource is actually missing. Vault-linked
resources are NEVER included in an automatic restore action -- restoring must not
automatically reopen/unlock a Vault-linked surface.
"""

from __future__ import annotations

import uuid

from app.operating_shell.types import (
    ResourceAvailability,
    RestoreResourceStatus,
    RestoreResult,
    WorkspaceRestorePlan,
)


def plan_workspace_restore(
    resources: tuple[RestoreResourceStatus, ...],
) -> WorkspaceRestorePlan:
    """Classifies the overall restore result from real per-resource availability -- never
    optimistic. Vault-linked resources are excluded from automatic restore regardless of
    their own availability status (locked, not silently reopened)."""
    if not resources:
        return WorkspaceRestorePlan(result=RestoreResult.BLOCKED, resources=(), vault_locked_refs=(), notes="no resources to restore")

    vault_locked = tuple(r.target_ref for r in resources if r.vault_linked)
    non_vault = [r for r in resources if not r.vault_linked]

    missing = [r for r in non_vault if r.availability == ResourceAvailability.MISSING]
    stale = [r for r in non_vault if r.availability == ResourceAvailability.STALE]
    available = [r for r in non_vault if r.availability == ResourceAvailability.AVAILABLE]

    if not non_vault:
        # Every referenced resource is Vault-linked -- nothing can be auto-restored.
        result = RestoreResult.BLOCKED
        notes = "all referenced resources are Vault-linked; restore cannot proceed automatically"
    elif missing and not available and not stale:
        result = RestoreResult.BLOCKED
        notes = f"{len(missing)} resource(s) missing, nothing else available"
    elif stale and not missing and not available:
        result = RestoreResult.STALE
        notes = f"{len(stale)} resource(s) are stale"
    elif missing or stale:
        result = RestoreResult.PARTIALLY_RESTORABLE
        notes = f"{len(missing)} missing, {len(stale)} stale, {len(available)} available"
    else:
        result = RestoreResult.RESTORABLE
        notes = "all non-Vault resources available"

    return WorkspaceRestorePlan(result=result, resources=resources, vault_locked_refs=vault_locked, notes=notes)


def resource_status(
    *, target_ref: uuid.UUID, kind: str, availability: ResourceAvailability, vault_linked: bool = False, note: str = ""
) -> RestoreResourceStatus:
    return RestoreResourceStatus(target_ref=target_ref, kind=kind, availability=availability, vault_linked=vault_linked, note=note)
