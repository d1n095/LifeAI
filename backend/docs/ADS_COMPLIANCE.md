# Advertising — Privacy & Compliance Considerations

> **Not legal advice; no certification claimed.** This document lists the technical and
> process controls LifeAI's advertising subsystem (app/ads/, migration 0071) implements,
> and the regulatory topics a real deployment must review with counsel before enabling
> ads — particularly under GDPR/ePrivacy (EU), and equivalent regimes (e.g. CCPA/CPRA).

## 1. Data-minimization by architecture

* The ad layer receives ONLY `AdContext` — placement, day-part, locale, tier, consent
  flag. It is a frozen dataclass validating every field at construction (including
  direct construction — the bypass route is closed and tested).
* Ad tables carry no content columns and no advertiser free-text beyond bounded
  id/title/body/url; nothing can reference a conversation, memory, or agent context
  (schema-level + AST dependency-direction tests).
* Engagement events use a pseudonymous per-session hash (SHA-256 over user id, client
  nonce, and date — rotates daily, no email, no IP).
* Ads never enter prompts and never influence reasoning, tools, or authorization
  (dependency-direction test enforces the import direction).

## 2. Consent

* Serving requires a granted consent row (latest event decides); withdrawal/denial takes
  effect immediately.
* Personalized ads require an explicit `personalized=True` grant; non-personalized
  contextual serving is the default and only mode in this slice.
* The consent trail is append-only with a policy version and optional region — designed
  for demonstrability ("proof of consent"), which GDPR Art. 7 expects.

## 3. GDPR/ePrivacy topics a real deployment MUST settle with counsel first

* **Legal basis.** Contextual, non-personalized ads may still require consent under
  ePrivacy rules depending on jurisdiction and placement; decide per region.
* **Consent UX.** Consent must be as easy to withdraw as to give, unbundled from
  service access (no dark patterns), with an age-appropriate flow for minors.
* **Data subject rights.** Deletion of a user cascades consent rows (`ON DELETE
  CASCADE`) and nulls ad-event user ids (`SET NULL`); pseudonymous session hashes in
  retained events must be assessed for re-identifiability before claiming anonymization.
* **Processors & transfers.** Any future ad network is a (joint) processor/controller
  relationship requiring Art. 28/29 terms, records of processing (Art. 30), DPIA for
  profiling/ads at scale, and transfer mechanisms (SCCs) if applicable.
* **Nothing here claims GDPR certification** — the controls are necessary-but-not-
  sufficient; legal review is a hard prerequisite for `ads_enabled=true` in production.

## 4. Ad-network policy hygiene (for the future integration)

* Clearly labeled placements (`kind="sponsored"` + visible label, mandatory and
  validated) — required by advertising self-regulation and most network policies.
* NO auto-clicking, no incentivized engagement, no click automation: an AST-based test
  proves no code path records clicks except the explicit client callback; engagement
  vocabulary (rewarded/autoclick/incentivize) is banned from ad-layer code.
* Fraud controls: per-user daily impression/click ceilings, impression→click plausibility
  floor (min interval, session-matched), ceiling-exceeded and implausible-click audit
  events; counters are Redis-backed and fail-closed when ads are enabled.
* Provider-agnostic: an `AdSource` protocol with only NullAdSource and operator-supplied
  house ads today; a future network integrates behind the same consent/firewall/fraud
  gates, not around them.

## 5. Revenue integrity (why ads can never buy authorization)

Ad revenue enters `revenue_ledger` as **estimated** — invisible to compute allocation
until a **settled** row with a verified reference exists. Ads have no import path into
authorization or security code, and allocation can only lower ceilings. An advertiser
cannot spend, escalate, or influence anything by being a payer — the same rules as every
other revenue source.
