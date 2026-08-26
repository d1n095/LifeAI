# ACTIVE WORK — Cursor Autonomy Night Shift (B7)

**Owner:** Cursor  
**Branch:** `cursor/wait-wake-provider-defer-release`  
**Integration tip at claim:** `27f8562` (#161 lease-release observability)

## Claimed

| Surface | Purpose |
|---|---|
| `backend/app/jobs/service.py` (`mark_failed_flush`) | Flush-only fenced job fail for supervisor-owned txs |
| `backend/app/development_supervisor/service.py` (`_release_provider_wait_midflight`) | Release mid-flight job + return task to ready on WAITING_PROVIDER |
| `backend/tests/backend/mainai/test_waiting_provider_wake_release.py` | B7 proofs |

## Boundaries

- `provider_spend_authorized` / `remote_write_authorized` remain false in production_entry
- Do not invent `waiting_external` producer
- Do not auto-authorize spend

## Evidence

- Local: 3 new + 1 regression = **4 passed**
