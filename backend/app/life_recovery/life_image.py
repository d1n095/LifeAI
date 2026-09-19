"""Encrypted Life Image: components, manifest, backup modes, backup authenticity (MainAI V2,
Stage V2-H2).

BACKUP ACCESS != BACKUP DECRYPTION: BackupRecord (see types.py) never carries key material
or ciphertext of its own -- only a pointer to a manifest, which itself only ever carries
already-encrypted LifeImageComponent envelopes. BACKUP EXISTS != BACKUP RESTORES: reaching
RestoreDrillState.RESTORE_TESTED requires run_backup_verification() to actually execute and
pass every check below, never merely because a backup was created/uploaded.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from app.sovereign_identity import KeyPurpose, decrypt_with_dek, encrypt_with_dek

from app.life_recovery.types import (
    COMPONENT_KEY_PURPOSE,
    BackupVerificationResult,
    ComponentCriticality,
    ComponentType,
    LifeImageComponent,
    LifeImageManifest,
    RestoreTier,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def build_life_image_component(
    plaintext: bytes,
    *,
    component_type: ComponentType,
    dek: bytes,
    owner_id: uuid.UUID,
    key_version: int,
    schema_version: int,
    content_version: int,
    criticality: ComponentCriticality,
    restore_priority: RestoreTier,
    dependencies: tuple[ComponentType, ...] = (),
) -> LifeImageComponent:
    """VAULT ACCESS != NORMAL MEMORY ACCESS: `purpose` is derived from `component_type` via
    the closed COMPONENT_KEY_PURPOSE mapping, never accepted as a free caller-supplied
    argument -- a caller cannot mislabel a VAULT component as MEMORY_KEY-bound (or vice
    versa) through this function."""
    purpose = COMPONENT_KEY_PURPOSE[component_type]
    envelope = encrypt_with_dek(plaintext, dek=dek, purpose=purpose, owner_id=owner_id, key_version=key_version)
    content_hash = hashlib.sha256(envelope.ciphertext).hexdigest()
    return LifeImageComponent(
        component_id=uuid.uuid4(),
        component_type=component_type,
        schema_version=schema_version,
        content_version=content_version,
        key_purpose=purpose,
        key_version=key_version,
        envelope=envelope,
        content_hash=content_hash,
        size_bytes=len(envelope.ciphertext),
        criticality=criticality,
        restore_priority=restore_priority,
        dependencies=dependencies,
    )


def decrypt_life_image_component(component: LifeImageComponent, *, dek: bytes) -> bytes:
    """Thin wrapper over app.sovereign_identity.decrypt_with_dek -- raises the real
    KeyMaterialError on tamper/wrong-key/wrong-purpose, never reimplements that check."""
    return decrypt_with_dek(component.envelope, dek=dek, expected_key_version=component.key_version)


def _manifest_signable_payload(
    *,
    image_id: uuid.UUID,
    version: int,
    created_at: datetime,
    device_origin: str,
    components: tuple[LifeImageComponent, ...],
    policy_versions: dict[str, int],
    min_compatible_lifeai_version: str,
) -> bytes:
    """Covers every manifest field a tamperer might want to alter -- component list
    (including each component's own content_hash), policy versions, min-compatible-version
    -- not just the identifiers. Component order is NOT sorted (order is itself meaningful
    metadata a tamperer could otherwise silently reorder without detection)."""
    component_parts = "|".join(
        f"{c.component_id}:{c.component_type.value}:{c.content_hash}:{c.content_version}:{c.restore_priority.value}"
        for c in components
    )
    parts = [
        str(image_id),
        str(version),
        created_at.isoformat(),
        device_origin,
        component_parts,
        ",".join(f"{k}={v}" for k, v in sorted(policy_versions.items())),
        min_compatible_lifeai_version,
    ]
    return "\x1f".join(parts).encode("utf-8")


def build_life_image_manifest(
    components: tuple[LifeImageComponent, ...],
    *,
    device_origin: str,
    policy_versions: dict[str, int],
    min_compatible_lifeai_version: str,
    owner_root_private_key: bytes,
    version: int = 1,
) -> LifeImageManifest:
    """`owner_root_private_key` is used only for this one `.sign()` call, never stored --
    same discipline as capsule.build_recovery_capsule()."""
    image_id = uuid.uuid4()
    created_at = _utcnow()
    payload = _manifest_signable_payload(
        image_id=image_id,
        version=version,
        created_at=created_at,
        device_origin=device_origin,
        components=components,
        policy_versions=policy_versions,
        min_compatible_lifeai_version=min_compatible_lifeai_version,
    )
    manifest_hash = hashlib.sha256(payload).hexdigest()
    private_key = Ed25519PrivateKey.from_private_bytes(owner_root_private_key)
    signature = private_key.sign(manifest_hash.encode("utf-8"))
    return LifeImageManifest(
        image_id=image_id,
        version=version,
        created_at=created_at,
        device_origin=device_origin,
        components=components,
        policy_versions=policy_versions,
        min_compatible_lifeai_version=min_compatible_lifeai_version,
        manifest_hash=manifest_hash,
        manifest_signature=signature,
    )


def verify_manifest_integrity(manifest: LifeImageManifest, *, owner_root_public_key: bytes) -> bool:
    """Real tamper detection: recompute the hash from the manifest's OWN current fields and
    verify the signature over the recomputed hash. Never trusts the stored manifest_hash
    blindly. Returns False (never raises) on mismatch or bad signature."""
    payload = _manifest_signable_payload(
        image_id=manifest.image_id,
        version=manifest.version,
        created_at=manifest.created_at,
        device_origin=manifest.device_origin,
        components=manifest.components,
        policy_versions=manifest.policy_versions,
        min_compatible_lifeai_version=manifest.min_compatible_lifeai_version,
    )
    recomputed_hash = hashlib.sha256(payload).hexdigest()
    if recomputed_hash != manifest.manifest_hash:
        return False
    try:
        public_key = Ed25519PublicKey.from_public_bytes(owner_root_public_key)
        public_key.verify(manifest.manifest_signature, manifest.manifest_hash.encode("utf-8"))
        return True
    except InvalidSignature:
        return False


def _dependency_graph_valid(components: tuple[LifeImageComponent, ...]) -> bool:
    """No missing prerequisite, no cycle. A component_type can appear at most once per
    manifest in this foundation model (one Life Image = one snapshot per component type)."""
    present_types = {c.component_type for c in components}
    by_type = {c.component_type: c for c in components}
    for component in components:
        for dep in component.dependencies:
            if dep not in present_types:
                return False
    # Cycle check: DFS over the dependency edges.
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[ComponentType, int] = dict.fromkeys(present_types, WHITE)

    def visit(component_type: ComponentType) -> bool:
        color[component_type] = GRAY
        for dep in by_type[component_type].dependencies:
            if color[dep] == GRAY:
                return False  # back-edge -> cycle
            if color[dep] == WHITE and not visit(dep):
                return False
        color[component_type] = BLACK
        return True

    for component_type in present_types:
        if color[component_type] == WHITE and not visit(component_type):
            return False
    return True


def run_backup_verification(
    manifest: LifeImageManifest,
    *,
    owner_root_public_key: bytes,
    available_component_ids: frozenset[uuid.UUID],
    resolvable_key_purposes: frozenset[KeyPurpose],
    current_min_compatible_version: str,
) -> BackupVerificationResult:
    """BACKUP EXISTS != BACKUP RESTORES: this is the ONLY function in this package that
    produces a result a caller may treat as "this backup actually works" -- see service.py's
    determine_restore_drill_state(), which only ever returns RESTORE_TESTED when
    result.passed is True."""
    reasons: list[str] = []

    manifest_valid = verify_manifest_integrity(manifest, owner_root_public_key=owner_root_public_key)
    if not manifest_valid:
        reasons.append("manifest failed integrity verification")

    components_available_ok = all(c.component_id in available_component_ids for c in manifest.components)
    if not components_available_ok:
        reasons.append("one or more manifest components are not available")

    hashes_match = all(hashlib.sha256(c.envelope.ciphertext).hexdigest() == c.content_hash for c in manifest.components)
    if not hashes_match:
        reasons.append("one or more component hashes do not match their recorded content_hash")

    key_references_resolvable = all(c.key_purpose in resolvable_key_purposes for c in manifest.components)
    if not key_references_resolvable:
        reasons.append("one or more component key purposes have no resolvable key reference")

    dependency_graph_valid = _dependency_graph_valid(manifest.components)
    if not dependency_graph_valid:
        reasons.append("component dependency graph is invalid (missing prerequisite or cycle)")

    # Simple, honest semantic-version comparison (major.minor.patch, numeric only) -- this
    # foundation stage does not need full PEP 440 / SemVer prerelease handling.
    def _version_tuple(v: str) -> tuple[int, ...]:
        return tuple(int(part) for part in v.split("."))

    try:
        software_compatible = _version_tuple(manifest.min_compatible_lifeai_version) <= _version_tuple(
            current_min_compatible_version
        )
    except ValueError:
        software_compatible = False
    if not software_compatible:
        reasons.append(
            f"manifest requires min compatible version {manifest.min_compatible_lifeai_version}, "
            f"current is {current_min_compatible_version}"
        )

    return BackupVerificationResult(
        manifest_valid=manifest_valid,
        components_available_ok=components_available_ok,
        hashes_match=hashes_match,
        key_references_resolvable=key_references_resolvable,
        dependency_graph_valid=dependency_graph_valid,
        software_compatible=software_compatible,
        failure_reasons=tuple(reasons),
    )


def check_backup_currentness(*, manifest_version: int, current_min_required_version: int, require_current: bool) -> None:
    """Old backup rollback detection: raises if `require_current` and the manifest is older
    than the currently-required version. A no-op when `require_current` is False -- rollback
    to an older backup is a legitimate owner choice in some contexts, this only blocks it
    when policy says currentness is mandatory."""
    if require_current and manifest_version < current_min_required_version:
        from app.life_recovery.types import RecoveryStateError

        raise RecoveryStateError(
            f"backup manifest version {manifest_version} is older than the currently-required "
            f"version {current_min_required_version}, and policy requires currentness -- rejected "
            "as a possible backup rollback"
        )


__all__ = [
    "build_life_image_component",
    "decrypt_life_image_component",
    "build_life_image_manifest",
    "verify_manifest_integrity",
    "run_backup_verification",
    "check_backup_currentness",
]
