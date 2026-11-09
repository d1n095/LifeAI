# FUTURE_BLUEPRINT_INPUT — Geographic Product Sales & Retailer Intelligence System

**Classification: `FUTURE_BLUEPRINT_INPUT`.**
**This is a captured Founder requirement, not an implementation task.** No code, schema,
migration, UI, data acquisition, or integration is authorized or performed by this document.
Life will design the Blueprint for this later, for Founder approval, once Life is operational as a
coordinator (see `docs/LIFE_OPERATIONAL_PRIORITY_2026-11-09.md`). `first_cycle_go=false`.

- **Source / authority:** Founder directive, 2026-11-09 (GitHub-first corrected directive,
  Mission 3). Authority = `founder`; basis = `manual` (verbatim Founder intent, captured, not
  inferred).
- **Why recorded now:** so the intent is durable and source-traceable, and is **not lost and not
  prematurely built**. It enters the future-work record only; it does not change the operational
  priority, which remains getting Life operational.

---

## 1. Founder intent (captured verbatim in substance)

The Founder wants Life, eventually, to design a geographic map that shows:

- Where individual products are actually sold, by city and geographic area.
- Direct-to-consumer sales versus retailer distribution.
- Retailers stocking products versus verified end-customer sell-through.
- Product presence, geographic coverage, sales trends, and expansion opportunities.
- Interactive navigation between map, cities, retailers, and product details.
- Verified public and authorized business information about cities and stores.
- Opportunities to increase sales and improve store performance.
- Ways to support employees — reduce unnecessary workload, improve working conditions and
  wellbeing, and achieve sustainable business results.

## 2. Hard constraints the future Blueprint must honour (Founder-stated)

- **Use aggregated or otherwise appropriately authorized geographic data only.**
- **Do not expose customers' private addresses.**
- **Do not presume access to retailer sales or employee information** — such data is used only
  where verified and authorized.
- The employee-wellbeing aim is a **first-class objective**, not a surveillance mechanism: it is
  about reducing unnecessary workload and improving conditions, never monitoring individuals.

## 3. Boundary notes for whoever designs the Blueprint later

These are guard-rails, not a design:

- **Privacy by construction.** Any future design must start from the project's existing
  default-deny posture (`docs/LIFE_VAULT_EGRESS_CONTROL.md`, `app.egress_policy`): no private
  customer, retailer-sales, or employee data leaves its boundary, and aggregation/authorization is
  proven before display, not assumed.
- **Source-grounded only.** Consistent with the Blueprint doctrine (PR #340 / migration 0070):
  every datum shown must trace to a verified public or explicitly authorized source; no inferred
  or scraped private data.
- **Reuse, don't invent.** When it is designed, it should sit on top of whatever product/entity
  and mapping primitives exist then (today: `app.project_entities`, `app.life_intents`), not a
  parallel data plane.
- **Founder-approved Blueprint first.** Per the operational model, Life proposes a source-grounded
  Blueprint; the Founder approves before anything is built. This document is the *input* to that
  future proposal, nothing more.

## 4. Status & dependencies

| Field | Value |
|---|---|
| Classification | `FUTURE_BLUEPRINT_INPUT` |
| Implementation authorized? | **No** |
| Depends on | Life being operational as a coordinator (operational priority), then a Founder-approved Blueprint |
| Blocks first start? | **No** — explicitly out of scope for first start |
| Related | `docs/LIFE_OPERATIONAL_PRIORITY_2026-11-09.md`, PR #340 (Blueprint machinery), `docs/LIFE_VAULT_EGRESS_CONTROL.md` |

*Recorded as future input per the Founder directive: "Classify this as FUTURE_BLUEPRINT_INPUT. Do
not implement this business product now. Life will design its Blueprint later."*
