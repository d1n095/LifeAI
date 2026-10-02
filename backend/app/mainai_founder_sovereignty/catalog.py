"""Extensible capability classification.

Do not hard-code every future product action. Callers register new capabilities
with an explicit risk tier. A capability never implies another unless an
implication is registered separately — calendar.create does not imply
calendar.delete, purchases.create, or mainai.policy.change.
"""

from __future__ import annotations

from app.mainai_founder_sovereignty.types import CapabilitySpec, RiskTier, SovereigntyError

_CATALOG: dict[str, CapabilitySpec] = {}
_IMPLICATIONS: dict[str, frozenset[str]] = {}


def register_capability(spec: CapabilitySpec) -> None:
    _CATALOG[spec.key] = spec


def register_implication(*, source: str, target: str) -> None:
    if source not in _CATALOG or target not in _CATALOG:
        raise SovereigntyError("unknown_capability", "implication endpoints must already be registered")
    current = set(_IMPLICATIONS.get(source, frozenset()))
    current.add(target)
    _IMPLICATIONS[source] = frozenset(current)


def get_capability(key: str) -> CapabilitySpec:
    spec = _CATALOG.get(key)
    if spec is None:
        raise SovereigntyError("unknown_capability", f"capability {key!r} is not registered")
    return spec


def implies(source: str, target: str) -> bool:
    if source == target:
        return True
    return target in _IMPLICATIONS.get(source, frozenset())


def _seed() -> None:
    if _CATALOG:
        return
    for spec in (
        CapabilitySpec("lights.control", "home_lights", "control", RiskTier.LOW, "harmless home lighting"),
        CapabilitySpec("tv.control", "home_tv", "control", RiskTier.LOW, "harmless TV control"),
        CapabilitySpec("family_calendar.read", "family_calendar", "read", RiskTier.MEDIUM),
        CapabilitySpec("family_calendar.create", "family_calendar", "create", RiskTier.MEDIUM),
        CapabilitySpec("family_calendar.update", "family_calendar", "update", RiskTier.MEDIUM),
        CapabilitySpec("family_calendar.create_event", "family_calendar", "create_event", RiskTier.MEDIUM),
        CapabilitySpec("family_calendar.delete", "family_calendar", "delete", RiskTier.MEDIUM),
        CapabilitySpec("shopping_list.update", "shopping_list", "update", RiskTier.MEDIUM),
        CapabilitySpec("founder_private_calendar.read", "founder_private_calendar", "read", RiskTier.FOUNDER_ONLY),
        CapabilitySpec("purchases.create", "purchases", "create", RiskTier.HIGH),
        CapabilitySpec("economy.read", "economy", "read", RiskTier.HIGH),
        CapabilitySpec("private_documents.read", "private_documents", "read", RiskTier.HIGH),
        CapabilitySpec("vehicle.security", "vehicle", "security", RiskTier.HIGH),
        CapabilitySpec("account.settings", "account", "settings", RiskTier.HIGH),
        CapabilitySpec("mainai.policy.change", "mainai_policy", "change", RiskTier.FOUNDER_ONLY),
        CapabilitySpec("mainai.security.policy", "security_policy", "change", RiskTier.FOUNDER_ONLY),
        CapabilitySpec("credentials.read", "credentials", "read", RiskTier.FOUNDER_ONLY),
        CapabilitySpec("system.authority", "system", "authority", RiskTier.FOUNDER_ONLY),
        CapabilitySpec("agent_control.root", "agent_control", "root", RiskTier.FOUNDER_ONLY),
        CapabilitySpec("delegation.root", "delegation", "root", RiskTier.FOUNDER_ONLY),
        CapabilitySpec("founder.identity.change", "founder_identity", "change", RiskTier.FOUNDER_ONLY),
    ):
        register_capability(spec)


_seed()
