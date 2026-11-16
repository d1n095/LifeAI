"""Sovereign Recovery + Encrypted Life Image + Progressive Hydration (MainAI V2, Stages
V2-H1, V2-H2, V2-H4).

Standalone, isolated, NOT imported by any production runtime path (no app.main import, no
app.guardian/app.privacy_boundary/app.sentinel import from this package). DOES intentionally
import from app.sovereign_identity (Part 1 of this same stage) -- see types.py's module
docstring for why that dependency is real and necessary, unlike guardian/privacy_boundary/
sentinel's deliberate mutual independence. See docs/mainai_v2/MAINAI_V2_SOVEREIGN_RECOVERY.md
for the design.
"""

from app.life_recovery.capsule import build_recovery_capsule, require_valid_capsule, verify_recovery_capsule_integrity
from app.life_recovery.hydration import (
    get_continuity_summary,
    mark_tier_complete_if_ready,
    new_hydration_progress,
    restore_component,
    restore_tier,
    unlock_vault,
)
from app.life_recovery.hydration import from_snapshot as hydration_from_snapshot
from app.life_recovery.hydration import to_snapshot as hydration_to_snapshot
from app.life_recovery.life_image import (
    build_life_image_component,
    build_life_image_manifest,
    check_backup_currentness,
    decrypt_life_image_component,
    run_backup_verification,
    verify_manifest_integrity,
)
from app.life_recovery.recovery_state import (
    RecoveryStateMachine,
    advance_recovery,
    enroll_recovery_method,
    new_recovery_state_machine,
    revoke_recovery_method,
    use_recovery_method,
    verify_recovery_identity,
    verify_receipt_chain_intact,
)
from app.life_recovery.reset import (
    LocalKeyStore,
    perform_full_device_reset,
    perform_reset_lifeai,
    perform_secure_reset,
    require_bounded_reset_preauthorization,
)
from app.life_recovery.service import (
    RecoveryEnvironment,
    determine_restore_drill_state,
    from_snapshot,
    new_recovery_environment,
    to_snapshot,
)
from app.life_recovery.types import (
    CRITICAL_TIERS,
    COMPONENT_KEY_PURPOSE,
    BackupMode,
    BackupRecord,
    BackupVerificationResult,
    ComponentCriticality,
    ComponentType,
    EmergencyResetPreauthorization,
    HydrationError,
    HydrationProgress,
    LifeImageComponent,
    LifeImageManifest,
    RecoveryCapsule,
    RecoveryMethodEnrollment,
    RecoveryReceipt,
    RecoveryState,
    RecoveryStateError,
    ResetError,
    ResetLevel,
    RestoreDrillState,
    RestoreTier,
)

__all__ = [
    "CRITICAL_TIERS",
    "COMPONENT_KEY_PURPOSE",
    "BackupMode",
    "BackupRecord",
    "BackupVerificationResult",
    "ComponentCriticality",
    "ComponentType",
    "EmergencyResetPreauthorization",
    "HydrationError",
    "HydrationProgress",
    "LifeImageComponent",
    "LifeImageManifest",
    "LocalKeyStore",
    "RecoveryCapsule",
    "RecoveryEnvironment",
    "RecoveryMethodEnrollment",
    "RecoveryReceipt",
    "RecoveryState",
    "RecoveryStateError",
    "RecoveryStateMachine",
    "ResetError",
    "ResetLevel",
    "RestoreDrillState",
    "RestoreTier",
    "advance_recovery",
    "build_life_image_component",
    "build_life_image_manifest",
    "build_recovery_capsule",
    "check_backup_currentness",
    "decrypt_life_image_component",
    "determine_restore_drill_state",
    "enroll_recovery_method",
    "from_snapshot",
    "get_continuity_summary",
    "hydration_from_snapshot",
    "hydration_to_snapshot",
    "mark_tier_complete_if_ready",
    "new_hydration_progress",
    "new_recovery_environment",
    "new_recovery_state_machine",
    "perform_full_device_reset",
    "perform_reset_lifeai",
    "perform_secure_reset",
    "require_bounded_reset_preauthorization",
    "require_valid_capsule",
    "restore_component",
    "restore_tier",
    "revoke_recovery_method",
    "run_backup_verification",
    "to_snapshot",
    "unlock_vault",
    "use_recovery_method",
    "verify_manifest_integrity",
    "verify_receipt_chain_intact",
    "verify_recovery_capsule_integrity",
    "verify_recovery_identity",
]
