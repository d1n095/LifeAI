"""Sovereign Identity + Key Hierarchy + Device Trust -- core types (MainAI V2, Stages
V2-G1, V2-G2, V2-H3).

Standalone, isolated, NOT imported by any production runtime path, and does NOT import
app.guardian, app.privacy_boundary, or app.sentinel (same independence discipline those
three packages already established -- composition happens only in a future cross-layer
test). See docs/mainai_v2/MAINAI_V2_SOVEREIGN_IDENTITY.md for the design this implements.

CORE GOAL: the user's MainAI must belong cryptographically to the user. LifeAI
infrastructure may transport/store encrypted state, but must not possess a universal
master key or ordinary ability to decrypt user state.

Invariants held throughout this package (not just documented -- see service.py/keys.py for
where each becomes a real, tested check):
  OWNER != SESSION                      -- a SessionIdentity's proof_level is evidence-derived,
                                            never self-claimed.
  LOGIN SUCCESS != ROOT AUTHORITY       -- ordinary session auth caps out at
                                            TRUSTED_DEVICE, never OWNER_ROOT.
  SERVER ACCESS != DECRYPTION AUTHORITY -- KeyEnvelope/WrappedKey never carry plaintext key
                                            material in any serialized/logged form.
  DEVICE WAS TRUSTED != DEVICE IS TRUSTED -- see device_trust.py: revocation is durable and
                                            keys off device_id, not off enrollment-record
                                            freshness.

NOTE on naming vs. app.guardian: Guardian already has its own 3-state `DeviceTrustState`
(TRUSTED/UNKNOWN/REVOKED) for its own fail-closed bounded-action logic. This package's
device trust model is deliberately richer (5 states, PENDING/TRUSTED/RESTRICTED/SUSPECTED/
REVOKED) and is named `DeviceTrustLevel` specifically to avoid the two being confused in
later cross-layer test code, even though they live in different modules and would not
technically collide.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- Proof levels: ordered, closed, deliberately small. ---------------------------------


class ProofLevel(str, Enum):
    """OWNER != SESSION, LOGIN SUCCESS != ROOT AUTHORITY: this ordering is the whole point
    of V2-G1. Only a real, verified root-authority proof (see root_authority.py) can ever
    produce OWNER_ROOT -- ordinary session auth caps out at TRUSTED_DEVICE."""

    ANONYMOUS = "ANONYMOUS"
    AUTHENTICATED_SESSION = "AUTHENTICATED_SESSION"
    TRUSTED_DEVICE = "TRUSTED_DEVICE"
    OWNER_CONFIRMED = "OWNER_CONFIRMED"
    OWNER_ROOT = "OWNER_ROOT"


PROOF_LEVEL_ORDER: dict[ProofLevel, int] = {
    ProofLevel.ANONYMOUS: 0,
    ProofLevel.AUTHENTICATED_SESSION: 1,
    ProofLevel.TRUSTED_DEVICE: 2,
    ProofLevel.OWNER_CONFIRMED: 3,
    ProofLevel.OWNER_ROOT: 4,
}


class RootSensitiveAction(str, Enum):
    """Closed vocabulary. Every one of these requires OWNER_ROOT-level proof, never less --
    see service.py's require_proof_level(). An action NOT in this enum is out of scope for
    this gate entirely (this package makes no claim about ordinary, non-root-sensitive
    actions)."""

    CHANGE_ROOT_SECURITY_POLICY = "CHANGE_ROOT_SECURITY_POLICY"
    AUTHORIZE_RECOVERY_METHOD = "AUTHORIZE_RECOVERY_METHOD"
    ENROLL_TRUSTED_DEVICE = "ENROLL_TRUSTED_DEVICE"
    REVOKE_TRUSTED_DEVICE = "REVOKE_TRUSTED_DEVICE"
    EXPORT_ROOT_KEY_MATERIAL = "EXPORT_ROOT_KEY_MATERIAL"
    SECURE_RESET = "SECURE_RESET"
    REMOTE_DESTRUCTIVE_OPERATION = "REMOTE_DESTRUCTIVE_OPERATION"
    CHANGE_GUARDIAN_POLICY = "CHANGE_GUARDIAN_POLICY"


class InsufficientProofLevel(PermissionError):
    """Raised by require_proof_level() -- never silently downgraded to a boolean."""


# --- Identity model. ----------------------------------------------------------------------


@dataclass(frozen=True)
class OwnerIdentity:
    """The abstract "this UUID is a real owner" fact -- carries no proof level itself.
    Proof level is a property of a specific SessionIdentity/assertion, never of the owner
    record, since the same owner can simultaneously hold sessions at different levels on
    different devices."""

    owner_id: uuid.UUID
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class OwnerRootCapability:
    """The owner's registered root key -- the ONE thing capable of producing an OWNER_ROOT
    SessionIdentity. `public_key` is the raw Ed25519 public key bytes (safe to hold
    server-side; it is the private half that never leaves the owner, see
    root_authority.py). Never holds private key material -- there is no field here that
    could."""

    owner_id: uuid.UUID
    public_key: bytes
    enrolled_at: datetime = field(default_factory=_utcnow)
    key_version: int = 1


@dataclass(frozen=True)
class SessionIdentity:
    """A concrete, evaluated identity assertion. `proof_level` is set exactly once, by
    root_authority.evaluate_identity_assertion() -- never self-claimed by a caller
    constructing this directly (nothing stops Python from letting a caller construct one
    with OWNER_ROOT baked in, since this is a plain dataclass and not e.g. Guardian's
    closed-constructor-path pattern; the REAL enforcement is that nothing in this package's
    service layer ever *trusts* a caller-supplied SessionIdentity's proof_level without
    having produced it itself via evaluate_identity_assertion() -- see
    require_proof_level()'s docstring for why that is the actual guarantee that matters)."""

    session_id: uuid.UUID
    owner_id: uuid.UUID
    proof_level: ProofLevel
    device_id: str | None
    established_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class DeviceIdentity:
    """A device's own cryptographic identity -- who this device claims to be. Distinct from
    DeviceRecord (device_trust.py), which tracks this device's current TRUST STATE. A
    device can have a stable DeviceIdentity across its whole lifetime while its
    DeviceRecord.trust_state changes many times (PENDING -> TRUSTED -> REVOKED)."""

    device_id: str
    owner_id: uuid.UUID
    public_key_fingerprint: str
    generated_at: datetime = field(default_factory=_utcnow)


class RecoveryMethodKind(str, Enum):
    PASSKEY = "PASSKEY"
    HARDWARE_KEY = "HARDWARE_KEY"
    OFFLINE_RECOVERY_CODE = "OFFLINE_RECOVERY_CODE"
    TRUSTED_DEVICE_SHARE = "TRUSTED_DEVICE_SHARE"


@dataclass(frozen=True)
class RecoveryIdentity:
    """An owner-enrolled recovery method. This package only models that the method exists
    and its capability status (see hardware.py) -- H1 (a separate, later fork) builds the
    actual Recovery Capsule / recovery state machine that consumes this."""

    recovery_identity_id: uuid.UUID
    owner_id: uuid.UUID
    kind: RecoveryMethodKind
    enrolled_at: datetime = field(default_factory=_utcnow)
    label: str = ""


@dataclass(frozen=True)
class AgentIdentity:
    """An internal agent/worker's identity -- structurally never root-capable. Nothing in
    this package has a code path that could construct a SessionIdentity at OWNER_ROOT (or
    even OWNER_CONFIRMED) for an AgentIdentity; agents authenticate as themselves, never as
    the owner (MODEL OUTPUT != AUTHORITY, the same invariant app.guardian's docstring
    states, applied to identity instead of decisions)."""

    agent_id: str
    owner_id: uuid.UUID
    scope_hint: str


@dataclass(frozen=True)
class ProviderIdentity:
    """An external AI provider's identity. A ProviderIdentity is never an owner and never
    carries a ProofLevel at all -- there is no field for one, and no function in this
    package accepts a ProviderIdentity anywhere a ProofLevel-bearing identity is expected."""

    provider_name: str


# --- Hardware-backed identity capability status. -------------------------------------------


class HardwareCapabilityStatus(str, Enum):
    """Default/fresh status is always UNAVAILABLE (see hardware.py) -- this package has no
    real hardware integration yet, so nothing may default to CONFIGURED or VERIFIED without
    genuine evidence."""

    UNAVAILABLE = "UNAVAILABLE"
    SUPPORTED_NOT_CONFIGURED = "SUPPORTED_NOT_CONFIGURED"
    CONFIGURED = "CONFIGURED"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


# --- Key hierarchy. -------------------------------------------------------------------------


class KeyPurpose(str, Enum):
    """VAULT ACCESS != NORMAL MEMORY ACCESS: MEMORY_KEY and VAULT_KEY are structurally
    separate purposes. Purpose is bound into AES-GCM's associated data at encrypt time (see
    keys.py) -- a key generated for one purpose cannot successfully decrypt ciphertext
    bound to a different purpose, even given the literal correct key bytes, because GCM's
    AAD authentication fails."""

    MEMORY_KEY = "MEMORY_KEY"
    VAULT_KEY = "VAULT_KEY"
    DOCUMENT_KEY = "DOCUMENT_KEY"
    BACKUP_MANIFEST_KEY = "BACKUP_MANIFEST_KEY"
    DEVICE_SYNC_KEY = "DEVICE_SYNC_KEY"
    RECOVERY_CAPSULE_KEY = "RECOVERY_CAPSULE_KEY"


@dataclass(frozen=True)
class KeyVersion:
    purpose: KeyPurpose
    owner_id: uuid.UUID
    version: int
    created_at: datetime = field(default_factory=_utcnow)
    rotated_from_version: int | None = None


@dataclass(frozen=True)
class WrappedKey:
    """A DEK, wrapped (encrypted) by a KEK. Never holds plaintext key material -- `wrapped`
    is ciphertext. `__repr__`/`__str__` are dataclass-default here, but that default only
    ever prints `wrapped`/`nonce` as bytes (already ciphertext, never plaintext), so there
    is nothing sensitive for a default repr to leak -- see
    test_no_plaintext_key_material_in_repr_or_snapshot for the proof."""

    purpose: KeyPurpose
    owner_id: uuid.UUID
    key_version: int
    wrapped: bytes
    nonce: bytes
    algorithm: str = "AES-256-GCM"
    wrapped_by_kek_version: int = 1


@dataclass(frozen=True)
class EncryptionEnvelope:
    """The result of encrypting real data under a DEK. `ciphertext` includes AES-GCM's auth
    tag (via AESGCM.encrypt's return value) -- tamper detection is structural, not a
    separate manual check. Never holds the DEK itself, only which purpose/version produced
    it (a reference, not the key)."""

    purpose: KeyPurpose
    owner_id: uuid.UUID
    key_version: int
    ciphertext: bytes
    nonce: bytes
    algorithm: str = "AES-256-GCM"


@dataclass(frozen=True)
class KeyEnvelope:
    """Bundles a WrappedKey with the metadata a future caller needs to know WHICH key to
    unwrap it with -- purpose + version + algorithm, all explicit, never implied."""

    wrapped_key: WrappedKey
    purpose: KeyPurpose
    key_version: int
    algorithm: str


@dataclass(frozen=True)
class RecoveryEnvelope:
    """Minimal placeholder shape for V2-H1 (a separate, later fork) to flesh out into the
    real Recovery Capsule. Deliberately small here: just enough field shape that H1 has a
    clean seam (a KEK wrapped a second time, by owner-chosen recovery material) without
    this fork inventing H1's own state machine."""

    owner_id: uuid.UUID
    wrapped_kek: bytes
    nonce: bytes
    recovery_method_kind: RecoveryMethodKind
    algorithm: str = "AES-256-GCM"
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class DeviceKeyGrant:
    """A specific device's wrapped copy of a specific DEK -- what device_trust.py's
    revocation flow stops issuing new instances of, and what a revoked device must never
    receive again."""

    device_id: str
    owner_id: uuid.UUID
    purpose: KeyPurpose
    key_version: int
    wrapped_key: WrappedKey
    granted_at: datetime = field(default_factory=_utcnow)


class KeyMaterialError(ValueError):
    """Raised on decrypt/unwrap failure -- tampered ciphertext, wrong key, wrong purpose,
    or wrong version. Never silently returns wrong plaintext."""


# --- Device trust (V2-H3). -----------------------------------------------------------------


class DeviceTrustLevel(str, Enum):
    """DEVICE WAS TRUSTED != DEVICE IS TRUSTED. See naming note in module docstring re:
    app.guardian's separate 3-state DeviceTrustState."""

    PENDING = "PENDING"
    TRUSTED = "TRUSTED"
    RESTRICTED = "RESTRICTED"
    SUSPECTED = "SUSPECTED"
    REVOKED = "REVOKED"


@dataclass
class DeviceRecord:
    """Mutable; owned/mutated only via device_trust.py's functions (mirrors
    GuardianState/SentinelState's "no public mutation outside the service module"
    discipline). `generation` increments on every enrollment -- see
    device_trust.py's reject_old_enrollment_replay() for why this, not enrollment-record
    freshness, is what revocation actually keys off of."""

    device_id: str
    owner_id: uuid.UUID
    public_identity: str
    generation: int
    enrolled_at: datetime
    last_verified: datetime
    trust_state: DeviceTrustLevel
    key_grants: tuple[DeviceKeyGrant, ...]
    sync_scope: tuple[str, ...]
    revoked_at: datetime | None = None
    reason: str | None = None
    attestation_status: HardwareCapabilityStatus = HardwareCapabilityStatus.UNAVAILABLE
    capability_leases: tuple[str, ...] = ()


class DeviceTrustError(ValueError):
    """Raised when an operation would violate DEVICE WAS TRUSTED != DEVICE IS TRUSTED --
    e.g. granting a key to a non-TRUSTED device, or an old-enrollment replay."""
