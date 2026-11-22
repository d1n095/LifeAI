"""The context firewall: the ONLY data shape the ad layer may receive.

AdContext is frozen and holds exclusively coarse, non-private fields. build_ad_context()
constructs it from a user row reading ONLY tenant/tier/region-ish fields — it never
opens a conversation, memory, or agent context, and the type system makes smuggling
private objects impossible: any caller passing a Conversation/Memory/User object where
a scalar is expected gets a TypeError at construction time (str-only fields).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdContext:
    """Coarse, non-private targeting surface. Deliberately tiny; extending it requires
    touching this file (and the firewall tests), not any ad source.

    Validation lives in __post_init__ — i.e. ON the dataclass itself, not only in
    build_ad_context — so direct construction (the bypass a future contributor would
    reach for first) is firewalled exactly as hard as the factory."""

    placement: str            # e.g. "sidebar", "footer" — where, not what
    day_part: str             # "morning" | "afternoon" | "evening" | "night" (UTC)
    locale: str               # BCP-47-ish tag from request settings, e.g. "sv-SE"
    tier: str                 # "free" | "paid" | "enterprise"
    personalized_allowed: bool  # consent decision, resolved by the service

    def __post_init__(self) -> None:
        for name in ("placement", "day_part", "locale", "tier"):
            object.__setattr__(self, name, _require_scalar(getattr(self, name), name))
        if not isinstance(self.personalized_allowed, bool):
            raise TypeError("AdContext.personalized_allowed must be a bool")


def _require_scalar(value, name: str) -> str:
    # Only plain strings pass. ORM model instances, dataclasses with content, dicts
    # (potential payloads) and bytes are all rejected — the firewall is type-level.
    if not isinstance(value, str):
        raise TypeError(f"AdContext.{name} must be a plain string, got {type(value).__name__}")
    if not value:
        raise ValueError(f"AdContext.{name} must be non-empty")
    if len(value) > 128:
        raise ValueError(f"AdContext.{name} too long (max 128 chars)")
    if any(ch < " " or ch == "\x7f" for ch in value):
        # Control characters (NUL, newlines, escapes) have no legitimate meaning in a
        # targeting scalar and are a classic injection/obfuscation tool — reject.
        raise ValueError(f"AdContext.{name} must not contain control characters")
    return value


def build_ad_context(*, placement: str, day_part: str, locale: str, tier: str,
                     personalized_allowed: bool) -> AdContext:
    return AdContext(
        placement=_require_scalar(placement, "placement"),
        day_part=_require_scalar(day_part, "day_part"),
        locale=_require_scalar(locale, "locale"),
        tier=_require_scalar(tier, "tier"),
        personalized_allowed=personalized_allowed,
    )
