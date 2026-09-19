"""Reset levels (MainAI V2, Stage V2-H1).

RESET_LIFEAI is a pure state-transition/intent record (no real destructive I/O in this
foundation stage). SECURE_RESET is real CRYPTO_ERASE: it deletes DEK/KEK bytes from a
LocalKeyStore, the only place this package's own decrypt helpers look them up -- after a
successful SECURE_RESET, a subsequent decrypt attempt genuinely raises KeyMaterialError
because the key is gone, not because of an internal flag. FULL_DEVICE_RESET is an
interface/stub only -- calling it always raises, it never silently pretends to succeed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.sovereign_identity import (
    InsufficientProofLevel,
    KeyMaterialError,
    KeyPurpose,
    RootSensitiveAction,
    SessionIdentity,
    require_proof_level,
)

from app.life_recovery.types import EmergencyResetPreauthorization, ResetError, ResetLevel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_WILDCARD_SCOPE_HINTS = frozenset({"*", "all", "any", "everything"})


@dataclass
class LocalKeyStore:
    """The ONLY place this package's own encrypt/decrypt helpers pull DEK/KEK bytes from.
    SECURE_RESET clears this dict in place -- a caller who separately kept a raw copy of a
    key outside this store is not this module's problem to solve (real hardware-backed key
    storage would not expose raw bytes to a caller at all; this in-memory stand-in is the
    foundation-stage equivalent, see app.sovereign_identity.hardware for the real seam)."""

    _material: dict[tuple[str, str], bytes] = field(default_factory=dict)

    def put(self, *, purpose: KeyPurpose, owner_id: uuid.UUID, key_bytes: bytes) -> None:
        self._material[(purpose.value, str(owner_id))] = key_bytes

    def get(self, *, purpose: KeyPurpose, owner_id: uuid.UUID) -> bytes:
        try:
            return self._material[(purpose.value, str(owner_id))]
        except KeyError as exc:
            raise KeyMaterialError(
                f"no key material available for purpose={purpose.value} owner={owner_id} "
                "-- either never provisioned, or destroyed by a prior SECURE_RESET"
            ) from exc

    def clear(self) -> None:
        self._material.clear()


def require_bounded_reset_preauthorization(preauth: EmergencyResetPreauthorization) -> None:
    """DEFENSIVE AUTONOMY != GENERAL AUTONOMY, applied to reset authority: scope_hint must
    not be a wildcard, max_uses/valid_until must be concrete and bounded. Mirrors
    app.sentinel.defensive_action.require_bounded_preauthorization()'s discipline."""
    if preauth.scope_hint.strip().lower() in _WILDCARD_SCOPE_HINTS:
        raise ResetError("EmergencyResetPreauthorization.scope_hint must not be a wildcard -- scope must be concrete")
    if preauth.max_uses < 1:
        raise ResetError("EmergencyResetPreauthorization.max_uses must be >= 1 -- no unbounded grant is allowed")
    if preauth.revoked:
        raise ResetError(f"EmergencyResetPreauthorization {preauth.preauth_id} has been revoked")
    if preauth.used_count >= preauth.max_uses:
        raise ResetError(f"EmergencyResetPreauthorization {preauth.preauth_id} has exhausted its max_uses")
    if _utcnow() > preauth.valid_until:
        raise ResetError(f"EmergencyResetPreauthorization {preauth.preauth_id} has expired")


def perform_secure_reset(
    key_store: LocalKeyStore,
    *,
    identity: SessionIdentity | None = None,
    preauth: EmergencyResetPreauthorization | None = None,
) -> EmergencyResetPreauthorization | None:
    """CRYPTO_ERASE: clears every key in `key_store`. Requires EITHER an OWNER_ROOT-proof
    `identity` (checked via the real require_proof_level(), never a local weaker copy) OR a
    valid, non-expired, non-revoked, not-exhausted `preauth`. Raises InsufficientProofLevel /
    ResetError (never silently no-ops) if neither is satisfied. Returns the preauth with its
    used_count incremented (a NEW object -- EmergencyResetPreauthorization is frozen) if a
    preauth path was used, else None."""
    if identity is not None:
        try:
            require_proof_level(identity, RootSensitiveAction.SECURE_RESET)
            key_store.clear()
            return None
        except InsufficientProofLevel:
            if preauth is None:
                raise
            # Fall through to the preauthorization path below.
    if preauth is None:
        raise InsufficientProofLevel(
            "SECURE_RESET requires either an OWNER_ROOT-proof SessionIdentity or a valid EmergencyResetPreauthorization"
        )
    require_bounded_reset_preauthorization(preauth)
    key_store.clear()
    from dataclasses import replace

    return replace(preauth, used_count=preauth.used_count + 1)


def perform_reset_lifeai() -> ResetLevel:
    """Pure intent record -- no real destructive I/O in this foundation stage (see module
    docstring). Local profile/session/cache removal is a future integration's job; this
    function only proves the RESET_LIFEAI level exists and is distinct from SECURE_RESET."""
    return ResetLevel.RESET_LIFEAI


def perform_full_device_reset() -> None:
    """Interface/stub only -- platform-specific future work. Always raises; never silently
    pretends a real full-device reset happened."""
    raise ResetError("FULL_DEVICE_RESET is not implemented -- platform-specific future work, interface stub only")
