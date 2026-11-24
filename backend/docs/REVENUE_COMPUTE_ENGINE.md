# Revenue & Compute Engine — Architecture

## Purpose

LifeAI needs a sustainable free tier (ad-funded) without ever compromising privacy,
security, or the local-AI-first product principle. This document specifies the engine
that makes spending governable: who spent what, who may spend how much, and what the
platform can afford — with strict concurrency safety and fail-closed behavior.

Delivered in branch `zai/revenue-engine`, migration 0070 (billing) + 0071 (ads).

## 1. One accounting pipeline (never two)

```
provider call (chat / planning / workforce)
        │
        ▼
app/billing/accounting.record_usage()          ← THE ONLY WRITER
        ├─ usage_log            per-call audit row (pre-existing table; extended
        │                       with tenant_id / agent / task / tier / kind)
        └─ usage_rollups        per-subject (user|tenant) × per-period (day|month)
                                materialized totals, integer micro-USD
```

* `app/providers/pricing.py` remains the ONLY price table.
* The pre-existing per-call writers (`routers/chat.py`, `provider_planning/service.py`)
  now flow through `record_usage()` — no parallel bookkeeping exists anywhere.
* A `single_writer_invariant` test fails the build if anyone re-introduces a direct
  `UsageLog(...)` write outside the pipeline.
* Unknown prices are recorded honestly (`cost_usd = NULL`) in the audit row while
  enforcement still counts them at `billing_unknown_cost_fallback_usd_per_1k` —
  conservative, never silently free.
* Refunds (`refund_usage`) are validated against the subject's recorded consumption for
  the current UTC day and written through the same pipeline as negative events; over-
  refunds are refused with zero rows written.

## 2. Enforcement: reserve → call → settle/release

`ComputeBudgetStore` generalizes the proven openai_consult reserve/settle pattern:

| Property | Mechanism |
|---|---|
| Distributed correctness | Redis MULTI/EXEC pipeline (both counters + TTL atomically); identical in-process fallback for single-process dev/E2E |
| Two windows | rolling UTC **day** AND UTC **month** per subject — monthly caps hold even for thinly-spread usage |
| Two subjects | **user** and (for members with `tenant_id`) their **tenant's shared envelope** — both must admit or nothing is charged; a failed second reserve releases the first |
| Worst-case admission | `reserve()` charges an estimate BEFORE the outbound call; `settle()` reconciles with actuals (unused reservation auto-released); `release()` returns everything on provider failure — no phantom spend |
| Reject = zero writes | caps are checked before any increment; a refused request leaves no partial state |
| Concurrency | 12-thread race test: exactly 10 of 12 × 1000-token requests admitted against a 10 000-token cap — no lost increments, no overshoot |
| Fail-closed | enforcement enabled + counter store unavailable ⇒ `BillingEnforcementUnavailableError` ⇒ HTTP 503 — spending is never admitted uncounted |

Chat wiring (default OFF via `billing_enforcement_enabled`; enabling is a founder
decision): reserve → HTTP 402 on budget exceeded → `chat_with_fallback` → settle with
real usage → `record_usage`.

## 3. Tiers

| Tier | Daily cost | Monthly cost | Daily tokens | Monthly tokens | Funded by |
|---|---|---|---|---|---|
| free | $0.10 | $1.50 | 30 000 | 300 000 | ads (P2) — deliberately small |
| paid | $2.00 | $25.00 | 300 000 | 3 000 000 | subscription |
| enterprise | $20.00 | $250.00 | 3 000 000 | 30 000 000 | contract; per-tenant overrides |

All values are Settings (repriced without code changes); tenant rows may override caps
and margins. Tiers touch ONLY spending ceilings — never authorization, tooling, or
security behavior.

## 4. Revenue-backed allocation (P3)

`revenue_ledger` separates **estimated** (unverified — e.g. an ad network's self-
reported numbers) from **settled** (verified settlement reference, enforced by a DB
CHECK). `allocation.py` then:

1. sums **settled only** — estimated revenue binds NOTHING (tested adversarially);
2. withholds a configurable **safety margin** (platform default 25 %, per-tenant
   override, DB-bounded 0–90 %);
3. **binds** the effective monthly ceiling: `min(tier cap, allocatable)` — but only when
   settled revenue actually exists for the period (bootstrap rule: a subject with no
   revenue rows is governed by tier caps; the founder-funded **platform envelope**
   — ledger rows with `subject_type='platform'` — binds when present);
4. **isolates tenants structurally**: a tenant's envelope is computed from that tenant's
   rows only; no query can mix subject ids; platform grants are explicitly
   platform-scoped.

Advertisements cannot override authorization: the allocation module never reads ad
tables, nothing in the reasoning/tool/authorization graph imports `app.ads` (AST-scanned
dependency-direction test), and allocation can only ever LOWER ceilings.

## 5. Local-model cost (two concepts, deliberately apart)

* User-facing: local inference is **free** (`estimate_cost` returns 0 for ollama) —
  product policy.
* Platform-side: `billing/local_cost.py` models real infrastructure cost
  (`gpu_hour_usd / (tokens_per_second × 3600) × (1 + energy_overhead_pct)`); defaults
  $0.75 GPU-hour, 30 tok/s, +20 % ⇒ ≈ **$8.33 per million tokens**. Used by allocation
  and the simulation — never by user billing.

## 6. Measured overhead

`test_billing_benchmark.py` (in-memory SQLite, median of ≥100 runs): accounting write
(one indexed INSERT + two atomic UPSERTs + commit) **0.48 ms**; enforcement cycle
**0.017 ms**; total **≈ 0.5 ms — under 0.2 %** of the cheapest provider call. On
PostgreSQL the rollup UPSERT is a single unique-index statement; the Redis enforcement
path adds one pipeline round-trip per window per call.

## 7. Deliberate limitations (honest blockers)

* No PostgreSQL available in this sandbox: migration 0070/0071 are verified by parity
  and lineage tests, not by running against a live server (see report blockers).
* `billing_enforcement_enabled` ships OFF; enabling is a founder decision with an ops
  prerequisite (Redis in production — the existing rate-limiter has the same stance).
* No payment processor, no invoicing, no live ad network: the ledger is designed for
  them; nothing integrates them in this slice.
