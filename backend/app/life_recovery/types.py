"""Sovereign Recovery + Encrypted Life Image + Progressive Hydration -- core types (MainAI
V2, Stages V2-H1, V2-H2, V2-H4).

Standalone, isolated, NOT imported by any production runtime path. Does NOT import
app.guardian, app.privacy_boundary, or app.sentinel (same independence discipline those
three packages established). UNLIKE that trio, this package DOES intentionally import real
types/functions from app.sovereign_identity (Part 1 of this same V2-G/H stage) -- recovery
is inherently key-hierarchy-and-identity-dependent, so this is a real, necessary dependency,
not a design smell. See docs/mainai_v2/MAINAI_V2_SOVEREIGN_RECOVERY.md for the design.

CORE GOAL (same as app.sovereign_identity): the user's MainAI must belong cryptographically
to the user. LifeAI infrastructure may transport/store encrypted state, but must not possess
a universal master key or ordinary ability to decrypt user state.

Invariants held throughout this package:
  RECOVERY != BACKDOOR              -- every recovery path terminates in a real
                                        app.sovereign_identity proof check or an explicit,
                                        non-revoked threshold-share reconstruction; nothing
                                        here accepts a claimed identity ("I'm LifeAI support")
                                        as sufficient.
  BACKUP ACCESS != BACKUP DECRYPTION -- BackupRecord/BackupMode never carry key material;
                                        see life_image.py's module docstring.
  BACKUP EXISTS != BACKUP RESTORES   -- RestoreDrillState only reaches RESTORE_TESTED after
                                        real verification checks pass, never merely because
                                        upload/creation succeeded (see life_image.py).
  RECOVERY STARTED != RECOVERY COMPLETE -- RecoveryState's transition table (see
                                        recovery_state.py) makes REQUESTED -> COMPLETE
                                        structurally unreachable in one hop.
  CLOUD_FIRST != SERVER_CAN_DECRYPT  -- BackupMode is pure storage/sync metadata; see
                                        test_backup_mode_has_zero_effect_on_crypto_path.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from app.sovereign_identity import EncryptionEnvelope, KeyPurpose, WrappedKey


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --- V2-H1: Sovereign Recovery state machine. -----------------------------------------------


class RecoveryState(str, Enum):
    """RECOVERY STARTED != RECOVERY COMPLETE. See recovery_state.py's
    _VALID_RECOVERY_TRANSITIONS -- REQUESTED cannot reach COMPLETE except by passing through
    every intermediate state in order."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    READY = "READY"
    RECOVERY_REQUESTED = "RECOVERY_REQUESTED"
    IDENTITY_VERIFICATION = "IDENTITY_VERIFICATION"
    CAPSULE_AVAILABLE = "CAPSULE_AVAILABLE"
    KEY_UNLOCKED = "KEY_UNLOCKED"
    CRITICAL_RESTORE = "CRITICAL_RESTORE"
    BACKGROUND_RESTORE = "BACKGROUND_RESTORE"
    RECOVERY_COMPLETE = "RECOVERY_COMPLETE"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    REVOKED = "REVOKED"


class RecoveryStateError(ValueError):
    """Raised on an invalid recovery state transition, a revoked recovery method being
    used, or a capsule/manifest that fails integrity verification."""


@dataclass(frozen=True)
class RecoveryReceipt:
    """Hash-chained, append-only -- same discipline as app.guardian.ContainmentReceipt and
    app.sentinel.EventReceipt. See recovery_state.py for the chain implementation."""

    receipt_id: uuid.UUID
    from_state: RecoveryState
    to_state: RecoveryState
    reason: str
    prev_hash: str
    this_hash: str = ""
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class RecoveryMethodEnrollment:
    """Wraps an app.sovereign_identity.RecoveryIdentity with the threshold/revocation
    bookkeeping V2-H1 needs. `threshold`/`required_shares` model 2-of-N recovery WITHOUT
    making it mandatory -- a single strong method has threshold=1, required_shares=1."""

    recovery_identity_id: uuid.UUID
    kind_label: str  # mirrors app.sovereign_identity.RecoveryMethodKind.value, kept as str to avoid a hard enum re-import loop
    threshold: int
    required_shares: int
    enrolled_at: datetime = field(default_factory=_utcnow)
    revoked: bool = False
    revoked_reason: str | None = None

    def __post_init__(self) -> None:
        if self.threshold < 1 or self.required_shares < 1:
            raise ValueError("threshold and required_shares must each be >= 1")
        if self.threshold > self.required_shares:
            raise ValueError("threshold cannot exceed required_shares")


# --- Recovery Capsule. ----------------------------------------------------------------------


@dataclass(frozen=True)
class RecoveryCapsule:
    """Small encrypted bootstrap object -- fleshes out
    app.sovereign_identity.types.RecoveryEnvelope's placeholder shape (composition, not
    modification: this type WRAPS a RecoveryEnvelope rather than editing that package's own
    file). Must NEVER contain plaintext user memory or usable root key material -- every
    sensitive field here is itself wrapped/encrypted using app.sovereign_identity.keys'
    real AES-256-GCM primitives; `encrypted_owner_identity` is an EncryptionEnvelope
    (ciphertext + nonce, never plaintext), `wrapped_key_references` are WrappedKey objects
    (ciphertext, require a KEK to unwrap -- see capsule.py's
    test_capsule_alone_cannot_decrypt_life_image)."""

    capsule_id: uuid.UUID
    owner_id: uuid.UUID
    recovery_envelope: WrappedKey  # the KEK, wrapped a second time by recovery material (see capsule.py)
    encrypted_owner_identity: EncryptionEnvelope
    wrapped_key_references: tuple[WrappedKey, ...]
    trusted_device_ids: tuple[str, ...]
    critical_config_references: tuple[str, ...]
    life_image_manifest_pointer: str
    policy_versions: dict[str, int]
    recovery_version: int
    integrity_hash: str
    integrity_signature: bytes
    created_at: datetime = field(default_factory=_utcnow)


# --- Reset levels. --------------------------------------------------------------------------


class ResetLevel(str, Enum):
    RESET_LIFEAI = "RESET_LIFEAI"
    SECURE_RESET = "SECURE_RESET"
    FULL_DEVICE_RESET = "FULL_DEVICE_RESET"  # interface/stub only -- platform-specific, not implemented


class ResetError(ValueError):
    """Raised when a reset is attempted without sufficient proof/preauthorization, or when
    FULL_DEVICE_RESET is attempted (stub -- always raises, never silently no-ops as if it
    worked)."""


@dataclass(frozen=True)
class EmergencyResetPreauthorization:
    """A structurally-bounded standing permission for SECURE_RESET, mirroring
    app.sentinel.types.PreauthorizedDefense's non-wildcard-bounding discipline
    (DEFENSIVE AUTONOMY != GENERAL AUTONOMY, applied here to reset authority). Must itself
    have been created by an OWNER_ROOT-proof caller (see reset.py's
    require_bounded_reset_preauthorization()) -- never a general "reset anything anytime"
    grant."""

    preauth_id: uuid.UUID
    owner_id: uuid.UUID
    scope_hint: str  # concrete, non-wildcard -- e.g. a specific device_id or component set label
    max_uses: int
    valid_until: datetime
    used_count: int = 0
    revoked: bool = False
    created_at: datetime = field(default_factory=_utcnow)


# --- V2-H2: Encrypted Life Image. -------------------------------------------------------------


class ComponentType(str, Enum):
    MAINAI_MEMORY = "MAINAI_MEMORY"
    INTENT_OBJECTS = "INTENT_OBJECTS"
    WORKSPACE_MEMORY = "WORKSPACE_MEMORY"
    USER_SETTINGS = "USER_SETTINGS"
    LOCAL_AGENTS = "LOCAL_AGENTS"
    AGENT_COMPETENCE_STATE = "AGENT_COMPETENCE_STATE"
    KNOWLEDGE_PACK_REFERENCES = "KNOWLEDGE_PACK_REFERENCES"
    DOCUMENTS = "DOCUMENTS"
    VAULT = "VAULT"
    SECURITY_POLICY = "SECURITY_POLICY"
    GUARDIAN_POLICY_REFERENCES = "GUARDIAN_POLICY_REFERENCES"
    DEVICE_PREFERENCES = "DEVICE_PREFERENCES"
    LOCAL_MODEL_CONFIG = "LOCAL_MODEL_CONFIG"
    APP_CONFIGURATION = "APP_CONFIGURATION"
    ACTIVE_PROJECTS = "ACTIVE_PROJECTS"
    RECOVERY_METADATA = "RECOVERY_METADATA"


class ComponentCriticality(str, Enum):
    CRITICAL = "CRITICAL"
    IMPORTANT = "IMPORTANT"
    OPTIONAL = "OPTIONAL"


class RestoreTier(str, Enum):
    """V2-H4 restore priorities. PRIORITY_0/1 = "critical restore"; PRIORITY_2/3 =
    "background restore". See hydration.py."""

    PRIORITY_0 = "PRIORITY_0"
    PRIORITY_1 = "PRIORITY_1"
    PRIORITY_2 = "PRIORITY_2"
    PRIORITY_3 = "PRIORITY_3"


_TIER_ORDER: dict[RestoreTier, int] = {
    RestoreTier.PRIORITY_0: 0,
    RestoreTier.PRIORITY_1: 1,
    RestoreTier.PRIORITY_2: 2,
    RestoreTier.PRIORITY_3: 3,
}

CRITICAL_TIERS = frozenset({RestoreTier.PRIORITY_0, RestoreTier.PRIORITY_1})

# VAULT ACCESS != NORMAL MEMORY ACCESS, applied to key purpose binding at the component
# level -- every component type maps to a KeyPurpose; VAULT is the only one bound to
# KeyPurpose.VAULT_KEY, everything else to MEMORY_KEY or DOCUMENT_KEY. This is what makes
# "restoring memory never unlocks Vault" a structural (AAD-bound) property, not just a
# convention -- see hydration.py's restore_tier(), which additionally never includes VAULT
# in any tier-batch restore regardless of what restore_priority a manifest claims for it.
COMPONENT_KEY_PURPOSE: dict[ComponentType, KeyPurpose] = {
    ComponentType.VAULT: KeyPurpose.VAULT_KEY,
    ComponentType.DOCUMENTS: KeyPurpose.DOCUMENT_KEY,
    ComponentType.MAINAI_MEMORY: KeyPurpose.MEMORY_KEY,
    ComponentType.INTENT_OBJECTS: KeyPurpose.MEMORY_KEY,
    ComponentType.WORKSPACE_MEMORY: KeyPurpose.MEMORY_KEY,
    ComponentType.USER_SETTINGS: KeyPurpose.MEMORY_KEY,
    ComponentType.LOCAL_AGENTS: KeyPurpose.MEMORY_KEY,
    ComponentType.AGENT_COMPETENCE_STATE: KeyPurpose.MEMORY_KEY,
    ComponentType.KNOWLEDGE_PACK_REFERENCES: KeyPurpose.MEMORY_KEY,
    ComponentType.SECURITY_POLICY: KeyPurpose.MEMORY_KEY,
    ComponentType.GUARDIAN_POLICY_REFERENCES: KeyPurpose.MEMORY_KEY,
    ComponentType.DEVICE_PREFERENCES: KeyPurpose.MEMORY_KEY,
    ComponentType.LOCAL_MODEL_CONFIG: KeyPurpose.MEMORY_KEY,
    ComponentType.APP_CONFIGURATION: KeyPurpose.MEMORY_KEY,
    ComponentType.ACTIVE_PROJECTS: KeyPurpose.MEMORY_KEY,
    ComponentType.RECOVERY_METADATA: KeyPurpose.MEMORY_KEY,
}


@dataclass(frozen=True)
class LifeImageComponent:
    component_id: uuid.UUID
    component_type: ComponentType
    schema_version: int
    content_version: int
    key_purpose: KeyPurpose
    key_version: int
    envelope: EncryptionEnvelope
    content_hash: str  # sha256 hex over envelope.ciphertext
    size_bytes: int
    criticality: ComponentCriticality
    restore_priority: RestoreTier
    dependencies: tuple[ComponentType, ...] = ()
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True)
class LifeImageManifest:
    """Encrypted-adjacent, signed, tamper-evident. Components are embedded directly (a real
    system would separate manifest metadata from a ciphertext blob store, but this
    foundation keeps them together for a clean, testable round-trip -- see
    docs/mainai_v2/MAINAI_V2_IMPLEMENTATION_PLAN.md for that noted simplification).
    `manifest_hash`/`manifest_signature` cover every field below via life_image.py's
    _manifest_signable_payload() -- flipping any field (via dataclasses.replace) makes
    verify_manifest_integrity() fail, see test_tampered_manifest_rejected."""

    image_id: uuid.UUID
    version: int
    created_at: datetime
    device_origin: str
    components: tuple[LifeImageComponent, ...]
    policy_versions: dict[str, int]
    min_compatible_lifeai_version: str
    manifest_hash: str
    manifest_signature: bytes


# --- Backup modes + authenticity. ------------------------------------------------------------


class BackupMode(str, Enum):
    """Storage/synchronization description ONLY. CLOUD_FIRST != SERVER_CAN_DECRYPT -- see
    test_backup_mode_has_zero_effect_on_crypto_path: this enum has no code path anywhere in
    this package that reads it before/during/after any encrypt/decrypt call."""

    LOCAL_ONLY = "LOCAL_ONLY"
    LOCAL_BACKUP = "LOCAL_BACKUP"
    PRIVATE_NAS = "PRIVATE_NAS"
    ENCRYPTED_CLOUD_BACKUP = "ENCRYPTED_CLOUD_BACKUP"
    HYBRID = "HYBRID"
    CLOUD_FIRST = "CLOUD_FIRST"
    MULTI_DEVICE = "MULTI_DEVICE"


class RestoreDrillState(str, Enum):
    """BACKUP EXISTS != BACKUP RESTORES. UNTESTED is the only state reachable merely by
    creating/uploading a backup -- RESTORE_TESTED requires life_image.run_backup_verification()
    to have actually run and passed. See life_image.py."""

    UNTESTED = "UNTESTED"
    PARTIALLY_VERIFIED = "PARTIALLY_VERIFIED"
    RESTORE_TESTED = "RESTORE_TESTED"
    STALE = "STALE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class BackupRecord:
    """Pure storage/sync metadata -- deliberately holds no key material and no ciphertext of
    its own (the manifest/components already carry the real ciphertext)."""

    backup_id: uuid.UUID
    owner_id: uuid.UUID
    mode: BackupMode
    manifest_image_id: uuid.UUID
    manifest_version: int
    restore_drill_state: RestoreDrillState = RestoreDrillState.UNTESTED
    created_at: datetime = field(default_factory=_utcnow)
    last_verified_at: datetime | None = None


@dataclass(frozen=True)
class BackupVerificationResult:
    manifest_valid: bool
    components_available_ok: bool
    hashes_match: bool
    key_references_resolvable: bool
    dependency_graph_valid: bool
    software_compatible: bool
    failure_reasons: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return (
            self.manifest_valid
            and self.components_available_ok
            and self.hashes_match
            and self.key_references_resolvable
            and self.dependency_graph_valid
            and self.software_compatible
        )


# --- V2-H4: Progressive hydration. ------------------------------------------------------------


class HydrationError(ValueError):
    """Raised when continuity/context is requested before critical (tier 0+1) restore has
    completed."""


@dataclass
class HydrationProgress:
    """Mutable; owned/mutated only via hydration.py's functions. `completed_component_ids`
    is what makes resume-after-interruption idempotent -- re-processing an already-completed
    component_id is always a no-op, never redone or double-applied."""

    owner_id: uuid.UUID
    completed_component_ids: set[uuid.UUID] = field(default_factory=set)
    completed_tiers: set[RestoreTier] = field(default_factory=set)
    started_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def is_mainai_usable(self) -> bool:
        return CRITICAL_TIERS.issubset(self.completed_tiers)
