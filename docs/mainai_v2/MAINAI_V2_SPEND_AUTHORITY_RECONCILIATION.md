# Spend / Cost Authority Reconciliation — Decision

Follow-up to the Dev Director round's flagged P1: "two pre-existing parallel spend systems
(`provider_spend` and `workforce.cost`) not reconciled." Audit and decision, written before
any code change, same discipline as the LifeIntent state-machine P0 fix and the Intent/Goal
reconciliation.

## 0. What the audit found — confirmed by direct reading, not assumed

**`app.provider_spend`** (migration 0060) is the REAL, LIVE, actively-used, well-tested
canonical ACTUAL SPEND system for provider (LLM) API calls. Real callers confirmed by grep:
`app.development_supervisor.production_entry`/`service`, `app.provider_planning.service`,
`app.workforce.provider_worker`, `app.mainai_startup_readiness`. Five real test files exist
(`test_provider_spend_api.py`, `test_provider_spend_grant_wake.py`,
`test_autonomous_task_provider_spend_e2e.py`, `test_provider_spend_authorization.py`,
`test_provider_spend_defer_park.py`). Real mechanics: goal+execution-envelope-scoped
`ProviderSpendAuthorization` (one active per goal, enforced by a partial unique index + row
lock), a genuine `reserved`/`settled`/`released` `ProviderSpendUsageEvent` lifecycle with a
real unique `(owner_id, source_ref)` idempotency key, atomic DB-level `settle_provider_spend_usage()`/
`release_provider_spend_usage()` SECURITY DEFINER functions, allowlisting, exhaustion
tracking. This is mature, correct, real financial-authority code.

**`app.workforce.cost`** (`WorkforceCostBudget`, T16) is a BUILT-BUT-NEVER-WIRED
organizational budget-ceiling layer. Its own module docstring already says "Reuses
conceptual ceilings; real spend via provider_spend" and the model's own docstring already
says "Organizational cost ceilings (T16). Real provider calls still use app.provider_spend."
**Confirmed by exhaustive grep: `reserve_against_budget()`/`settle_budget_reservation()`/
`release_budget_reservation()`/`assert_scopes_allow_spend()` have ZERO real callers anywhere
in this codebase** — only re-exported by `app/workforce/__init__.py` (a package export list,
not a caller) and referenced by the newly-built, still-isolated `app/dev_director/` package
from the prior round. No test file for this module exists at all.

**Net finding: there is no LIVE "two divergent financial truths" risk in production today.**
The risk is dormant, not active — `workforce.cost` exists, was correctly designed from the
start to compose with (not replace) `provider_spend`, but currently provides zero real
protection because nothing calls it. The founder's own concern ("before MainAI autonomous
development relies on budget/spend decisions") is exactly right as a forward-looking
concern, not a mischaracterization of today's state — this document treats it that way.

**Genuinely missing semantic distinctions found**: no refund path exists anywhere in
`provider_spend` (a settled cost, once recorded, cannot be adjusted downward) — a real gap,
flagged, not built this round (would change live billing semantics, deserves its own
focused, separately-reviewed change). A released reservation records zero cost even if a
provider genuinely charged something before failing (`release_provider_spend_call()` has no
"partial cost incurred before failure" parameter) — a real, narrower gap, also flagged, not
built this round for the same reason.

## 1. The decision

**Option A, confirmed as already the correct, already-documented intent**: `provider_spend`
remains the canonical ACTUAL SPEND authority for provider calls, unchanged. `workforce.cost`
becomes the real, WIRED organizational ceiling layer sitting above it — exactly matching
what its own docstrings already claimed. The fix is to make the already-correct design
real rather than aspirational.

Concretely:

1. **`app.provider_spend.service.reserve_provider_spend_call()`** gains one new,
   backward-compatible check: before holding a reservation, it calls
   `app.workforce.cost.assert_scopes_allow_spend()` for the `("goal", str(goal_id))` and
   `("provider", provider)` scopes. Per `assert_scopes_allow_spend()`'s own existing,
   unchanged logic, an UNSET scope (no `WorkforceCostBudget` row configured) is a no-op —
   **zero behavior change for any goal/provider that has no organizational ceiling
   configured**, which is every caller today, confirmed by the fact that no
   `WorkforceCostBudget` row is ever created in production yet. This closes the dormant gap
   without touching any existing caller's real behavior.
2. **`app.dev_director` gains a new, real integration module**, `budget_integration.py` —
   consistent with `canonical_projection.py`'s own established precedent (the Intent/Goal
   reconciliation round already decided real production-code imports are expected and
   correct for this package, unlike the five mutually-independent sibling packages). This
   module implements the founder's own requested unified contract
   (`can_reserve_budget`/`reserve_budget`/`record_provider_charge`/`settle_job_cost`/
   `release_unused_reservation`/`remaining_budget`) as a thin, real composition over the two
   real systems — never a third parallel ledger, never duplicating either system's own state.
3. **Semantics** (`estimated_cost`/`reserved_budget`/`authorized_budget`/`actual_spend`/
   `settled_spend`) already exist as real, distinct fields on `ProviderSpendAuthorization`/
   `ProviderSpendUsageEvent` (`max_cost_usd`, `reserved_cost_usd`, `spent_cost_usd`) and
   `WorkforceCostBudget` (`cap_usd`, `reserved_usd`, `spent_usd`) — no new fields needed for
   these. `provider_reported_spend` vs. `verified_spend`: currently collapsed (whatever
   `cost_usd` a caller passes to `settle_provider_spend_call()` is trusted as truth) —
   documented honestly as an existing, unresolved limitation (PROVIDER BILLING CLAIM !=
   VERIFIED SPEND is not yet enforced anywhere in this codebase), not fixed this round.
   `refunded_spend`/`failed_attempt_cost`: genuinely missing, flagged per §0, not built.
   `examiner_cost`/`retry_cost`: these are `dev_director`-level categorizations, not concepts
   `provider_spend`/`workforce.cost` need to know about — `dev_director`'s own `Job`/
   `BuilderAssignment`/`ExaminerAssignment` already carry `job_id`/`task_id`, which
   `ProviderSpendUsageEvent` already has real columns for — the categorization is a query-time
   join, not a new stored field.

## 1a. Bonus bug found and fixed during this reconciliation's own test program

While writing the adversarial test program for `budget_integration.py`, a genuine,
pre-existing bug in `settle_provider_spend_call()` was found (three-check verified: the
regression test fails without the fix, passes with it): the function was missing the
`db.refresh(event)` call its sibling `release_provider_spend_call()` already has, with an
explicit comment ("Raw SQL updated the row; refresh so the identity map does not keep
'reserved'") explaining precisely why it's needed. `settle_provider_spend_call()`'s own raw
SQL `SELECT settle_provider_spend_usage(...)` updates the row via a SECURITY DEFINER
function, then re-queries via a plain `select()` — but SQLAlchemy's identity map returns the
already-loaded (stale) Python object for that primary key without refreshing its attributes
unless the object was expired or explicitly refreshed. A caller reading the returned event's
attributes without an intervening `session.commit()` (a completely normal, supported calling
pattern — the module's own docstring explicitly describes `reserve → invoke → settle` as the
intended boundary, with no commit mandated in between) silently got stale `status`/`cost_usd`
values back. Confirmed by direct reading that neither of the two real production call sites
(`app.provider_planning.service`, both call sites) reads the return value at all — so this
was a real, live, latent bug with no actual production symptom yet, closed before this
reconciliation's own new `budget_integration.py` became the first real caller to read it.

## 2. What this reconciliation does NOT do

- Does not build a refund mechanism or partial-failed-attempt-cost tracking — real,
  flagged gaps, deserving their own focused, separately-reviewed rounds given they change
  live billing semantics.
- Does not verify provider-reported cost against independent ground truth — flagged as an
  existing, unresolved limitation.
- Does not modify `WorkforceCostBudget`'s schema or `provider_spend`'s schema.
- Does not wire external providers live, does not modify #245.
