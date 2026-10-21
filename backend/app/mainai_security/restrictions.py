"""Explainable restrictions from actual settings and kernel state. Never invent policy."""

from __future__ import annotations

from typing import Any

from app.config import get_settings

# Each restriction is bound to a real setting or wiring fact. If a requested capability
# has no matching source, the answer is source_unknown — not a fabricated explanation.
_SETTING_SOURCES: dict[str, str] = {
    "github_write_enabled": "app.config.Settings.github_write_enabled",
    "github_auto_merge_enabled": "app.config.Settings.github_auto_merge_enabled",
    "mainai_activation_mode": "app.config.Settings.mainai_activation_mode",
    "mainai_legacy_prepare_pr_writes_enabled": "app.config.Settings.mainai_legacy_prepare_pr_writes_enabled",
    "mainai_allow_live_checkout_writes": "app.config.Settings.mainai_allow_live_checkout_writes",
    "recall_http_mounted": "runtime_wiring.recall_http_mounted",
    "first_cycle_go": "hardcoded_false.first_cycle_go",
}


def explain_restrictions(
    *,
    recall_http_mounted: bool,
    requested: str | None = None,
) -> list[dict[str, Any]]:
    settings = get_settings()
    restrictions: list[dict[str, Any]] = []

    def _add(code: str, setting: str, value: Any, explain: str) -> None:
        restrictions.append(
            {
                "code": code,
                "setting": setting,
                "source": _SETTING_SOURCES[setting],
                "value": value,
                "explain": explain,
                "invented": False,
            }
        )

    if not settings.github_write_enabled:
        _add(
            "github_write_disabled",
            "github_write_enabled",
            False,
            "GitHub-skrivning är avstängd tills grundaren godkänner den här profilen.",
        )
    if not settings.github_auto_merge_enabled:
        _add(
            "auto_merge_disabled",
            "github_auto_merge_enabled",
            False,
            "Automatisk merge är avstängd. Merge kräver separat Founder-beslut.",
        )
    if not settings.mainai_activation_mode:
        _add(
            "activation_mode_off",
            "mainai_activation_mode",
            False,
            "Aktiveringsläge är av. First-cycle GO förblir false.",
        )
    if not settings.mainai_legacy_prepare_pr_writes_enabled:
        _add(
            "legacy_prepare_pr_writes_disabled",
            "mainai_legacy_prepare_pr_writes_enabled",
            False,
            "Legacy Contents-API PR-skrivning är avstängd.",
        )
    if not getattr(settings, "mainai_allow_live_checkout_writes", False):
        _add(
            "live_checkout_writes_disabled",
            "mainai_allow_live_checkout_writes",
            False,
            "Kodagenten får inte ändra den körande serverns checkout utan explicit Founder-auktorisation.",
        )
    if not recall_http_mounted:
        _add(
            "recall_http_unmounted",
            "recall_http_mounted",
            False,
            "Personal Recall HTTP är inte monterad och inte aktiverad.",
        )
    _add(
        "first_cycle_go_false",
        "first_cycle_go",
        False,
        "Första cykelns GO är hårdkodat false i Founder-ops. Den här säkerhetsmodulen lägger inte till något GO-villkor.",
    )

    if requested:
        matches = [item for item in restrictions if item["code"] == requested or item["setting"] == requested]
        if matches:
            return matches
        return [
            {
                "code": "source_unknown",
                "setting": requested,
                "source": None,
                "value": None,
                "explain": "Ingen konfigurerad källa förklarar den här begränsningen.",
                "invented": False,
            }
        ]
    return restrictions
