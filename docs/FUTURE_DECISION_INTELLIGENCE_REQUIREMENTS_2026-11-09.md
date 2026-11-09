# FUTURE capability requirement — Decision Intelligence & Explainability

**Classification: `FUTURE_REQUIREMENT`. Not authorized implementation work now.** Recorded per the
Founder directive (2026-11-09) alongside the current operational plan, which it does **not**
reopen or restructure. Current single priority is unchanged: getting **Life operational as the
Founder AI** (read/understand → Blueprint → agent coordination). `first_cycle_go=false`.

## Why this is recorded now
So the requirement is durable and source-traceable, and is **not built prematurely**. Advanced
decision intelligence comes **after** Life is operational. This document adds scope to the FUTURE
record only.

## Required future capabilities (Life must eventually be able to)
1. **Explain a recommendation** — why she made it.
2. **Show the actual scoring** — factors, weights, calculated contributions, with percentages
   **only when mathematically justified** (never invented precision).
3. **Surface omitted factors** — important decision factors she failed to consider.
4. **Detect weak evidence** — incomplete, outdated, contradictory, or unreliable sources.
5. **Independently reassess** her own recommendations — **without self-authorizing execution**.
6. **Seek more evidence when uncertain** — search additional **authorized** sources.
7. **Escalating research tiers (permission-controlled, future):** public internet research →
   authorized non-indexed databases → eventually an **isolated, permission-controlled Tor
   research** capability, each used only **when justified**. **Not enabled now**; no unrestricted
   browsing, no Tor access.
8. **Compare alternatives** — and explain what new evidence changed the conclusion.
9. **Learn from recurring mistakes** — through **traceable, tested, Founder-approved** improvements
   only (never self-applied).

## Reuse (do not duplicate) when it is eventually built
- `app.mainai_executive.priority` — ranking/priority signals.
- `app.mainai_executive.why_graph` — explanation/causal structure (capability 1/8).
- `app.mainai_executive.assumption_scan` — omitted/weak-assumption detection (capability 3/4).
- `app.capability_reality` — honest "what is verified vs unknown" grounding (capability 4).
- `app.inspectable_memory` truth-claim receipts — contradiction/staleness signals (capability 4).

## Hard boundaries (permanent, at any future automation level)
- Reassessment and mistake-learning **never self-authorize execution** — they produce proposals
  for Founder/reviewed approval (same doctrine as `founder_memory` and the Blueprint approval
  gate).
- Research tiers are **permission-controlled and default-off**; Tor is isolated and explicitly
  gated; no unrestricted network access.
- Percentages/weights are shown **only when mathematically derived**, never as false precision.

*This is a FUTURE requirement. See `LIFE_OPERATIONAL_PRIORITY_2026-11-09.md` for the current,
unchanged operational priority.*
