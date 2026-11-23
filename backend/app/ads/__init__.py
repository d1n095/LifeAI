"""Advertising subsystem — ISOLATED by design (P2).

Purpose: enable a future ad-funded free tier WITHOUT giving advertising any foothold in
private life or agent behavior. The hard rules, each structurally enforced (see
tests/backend/test_ads_isolation.py):

1. CONTEXT FIREWALL. The ad layer accepts ONLY app/ads/context.py's AdContext — a frozen
   dataclass whose fields are placement, day-part, locale, tier, consent state. No
   conversation, memory, project, or agent-context object can even be passed (wrong
   types raise TypeError; AdContext has no content fields to fill).
2. NO INFLUENCE OVER REASONING OR TOOLS. Nothing in app/ads/ is imported by the chat,
   MainAI, workforce, or authorization code paths (a dependency-direction test greps the
   import graph). Ad content is presentation-only, delivered to the FRONTEND as clearly
   labeled sponsored slots; it never enters prompts.
3. CLEARLY IDENTIFIED. Every placement payload carries kind="sponsored" and a required
   human-readable label; a source that omits them is rejected by the service.
4. CONSENT FIRST. No impression is served without a granted consent row (or an explicit
   non-personalized regional policy); withdrawal takes effect immediately (latest event
   decides). Personalized ads additionally require personalized=True consent. Regional
   gating is policy configuration, not client input.
5. FRAUD/ABUSE CONTROLS. Per-user per-day impression and click ceilings and an
   impression→click plausibility floor (min interval, session-hash matching) live in the
   service; anomalies are audit events. NO code path clicks ads automatically and no
   incentive loop exists — engagement events originate ONLY from an explicit, separate
   client callback (documented + tested).
6. PROVIDER-AGNOSTIC. Sources implement the AdSource protocol; the only built-ins are
   NullAdSource (default) and ConfiguredStaticSource (operator-supplied house ads).
   There is no ad-network SDK, no tracking pixels, no third-party calls in this slice.
7. DISABLED BY DEFAULT. ads_enabled=False everywhere; the service returns None placements
   and records nothing when disabled.

Read docs/ADS_COMPLIANCE.md for GDPR/advertising-law considerations (no legal
certification is claimed).
"""

from app.ads.context import AdContext
from app.ads.service import (
    AdsEnforcementUnavailableError,
    build_ad_context,
    get_sponsored_placement,
    record_ad_consent,
    record_ad_event,
    current_consent,
    configure_counters,
    session_hash_for,
)

__all__ = [
    "AdContext",
    "AdsEnforcementUnavailableError",
    "build_ad_context",
    "get_sponsored_placement",
    "record_ad_consent",
    "record_ad_event",
    "current_consent",
    "configure_counters",
    "session_hash_for",
]
