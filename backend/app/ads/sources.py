"""Ad sources: provider-agnostic protocol + the two built-ins.

AdSource implementations return SponsoredCreative objects — self-describing and required
to carry kind="sponsored" plus a display label. There is no SDK, no network call, and no
advertiser-controlled free-text beyond the two validated strings (title, body ≤ limits).

* NullAdSource — the default; serves nothing. Used whenever ads are disabled or no
  operator source is configured.
* ConfiguredStaticSource — operator-supplied house ads via Settings.ads_house_ads_json
  (a JSON array of {id, title, body, url}). Provider-agnostic: swapping in a real ad
  network later means implementing AdSource behind this same interface — and the
  firewall, consent, labeling and fraud controls apply to it identically.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.ads.context import AdContext
from app.config import get_settings

MAX_TITLE = 80
MAX_BODY = 200
MAX_URL = 512


@dataclass(frozen=True)
class SponsoredCreative:
    """A clearly-identified sponsored slot. `kind` and `label` are MANDATORY and are
    re-validated by the service layer — a source cannot serve an unlabeled ad."""

    creative_id: str
    kind: str                 # must be "sponsored"
    label: str                # human-readable disclosure, e.g. "Sponsored"
    title: str
    body: str
    url: str

    def __post_init__(self) -> None:
        if self.kind != "sponsored":
            raise ValueError("SponsoredCreative.kind must be 'sponsored' — unlabeled ads are refused")
        if not self.label:
            raise ValueError("SponsoredCreative requires a visible disclosure label")
        if len(self.title) > MAX_TITLE or len(self.body) > MAX_BODY or len(self.url) > MAX_URL:
            raise ValueError("sponsored creative exceeds size limits (title 80 / body 200 / url 512)")


class AdSource:
    """Protocol: match(context) -> SponsoredCreative | None. Implementations MUST be
    side-effect free — all consent/limits/fraud decisions happen in the service layer."""

    def match(self, context: AdContext) -> SponsoredCreative | None:  # pragma: no cover - interface
        raise NotImplementedError


class NullAdSource(AdSource):
    def match(self, context: AdContext) -> SponsoredCreative | None:
        return None


class ConfiguredStaticSource(AdSource):
    """Serves operator-configured house ads. Selection is deterministic on coarse context
    (round-robin by day-part+placement hash) — no profile targeting exists in this slice."""

    def __init__(self, pool_json: str):
        parsed = json.loads(pool_json)
        if not isinstance(parsed, list):
            raise ValueError("ads_house_ads_json must be a JSON array of creatives")
        self._pool = [
            SponsoredCreative(
                creative_id=str(entry["id"]),
                kind="sponsored",
                label=str(entry.get("label", "Sponsored")),
                title=str(entry["title"]),
                body=str(entry.get("body", "")),
                url=str(entry.get("url", "")),
            )
            for entry in parsed
        ]

    def match(self, context: AdContext) -> SponsoredCreative | None:
        if not self._pool:
            return None
        index = (hash((context.day_part, context.placement)) + len(self._pool)) % len(self._pool)
        return self._pool[index]


def source_from_settings(settings=None) -> AdSource:
    settings = settings or get_settings()
    if settings.ads_house_ads_json:
        return ConfiguredStaticSource(settings.ads_house_ads_json)
    return NullAdSource()
