"""MainAI startup readiness — machine-checkable levels (not one boolean).

IMPORTABLE != HEALTHY.
EXPECTED MIGRATION HEAD != VERIFIED MIGRATION HEAD.
UNKNOWN fails closed for higher tiers.
Blocker lists accumulate — never overwritten.
"""

from __future__ import annotations

from app.mainai_startup_readiness.receipts import (
    CheckStatus,
    ReadinessCheck,
    ReadinessLevel,
    StartupReadinessReport,
    evaluate_startup_readiness,
    verify_migration_head,
    verify_provider_disabled,
)

__all__ = [
    "CheckStatus",
    "ReadinessCheck",
    "ReadinessLevel",
    "StartupReadinessReport",
    "evaluate_startup_readiness",
    "verify_migration_head",
    "verify_provider_disabled",
]
