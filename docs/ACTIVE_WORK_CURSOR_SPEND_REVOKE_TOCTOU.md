# ACTIVE WORK — Cursor Supervisor spend fail-fast (#181)

**Owner:** Cursor  
**Branch:** `cursor/toctou-spend-revoke-before-reserve`  
**PR:** [#181](https://github.com/d1n095/LifeAI/pull/181)  
**Base tip:** `77d3f1e` (#177 merged)  

## Honest scope (Outcome B after Claude negative control)

**NOT claiming:** closes unauthorized-provider-invocation TOCTOU (that fence already exists).

**Claiming:** Supervisor fail-fast / defense-in-depth on stale tick-start `provider_spend_authorized`.

| Layer | Role |
|---|---|
| OUTER `_live_provider_spend_authorized` | Re-read before `plan_with_provider`; skip planning under stale True |
| INNER `reserve_provider_spend_call` | Authoritative final security gate before `adapter.propose` |

## Mutation proof

- Supervisor fail-fast test: **PASSES on #181**, **FAILS on pre-#181** (`plan_with_provider` was entered)
- Inner-gate tests: pass on both (by design — they test reserve, not #181)

## Do not merge until

- PR wording matches Outcome B
- exact head CI green
