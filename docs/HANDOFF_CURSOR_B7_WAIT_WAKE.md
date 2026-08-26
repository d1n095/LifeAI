# HANDOFF — B7 WAITING_PROVIDER wake release (Cursor)

**SHA:** `95e4b61e`  
**PR:** https://github.com/d1n095/LifeAI/pull/162  
**Base tip:** `27f8562`

## Done

On `WAITING_PROVIDER` defer:
1. Fence-fail mid-flight `mainai_jobs` row (`mark_failed_flush`, capability_unavailable)
2. Return task to `ready` (no fabricated verification failure)
3. Next supervisor tick is the wake; takeover reset fails closed on ready tasks

## Explicit non-goals

- No `waiting_external` producer/clock
- No spend auto-authorization
- PROVIDER_SPEND_NOT_AUTHORIZED still defers without release (avoid job-spam until durable park designed)

## Next

- Merge green rebased PRs (#155→#160) when CI confirms new heads
- Composed autonomy graph test (spend/remote_write false)

```json
{
  "lane": "cursor_autonomy_night",
  "unit": "B7_waiting_provider_wake_release",
  "branch": "cursor/wait-wake-provider-defer-release",
  "local_result": "4 passed",
  "provider_spend_authorized": false,
  "next": ["merge_rebased_prs", "composed_autonomy_test"]
}
```
