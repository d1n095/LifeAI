# MAINAI Self-Model / Capability Ledger (Stage E)

**Branch:** `cursor/mainai-self-model-capability-ledger`  
**Depends on:** Stage D (stacked) / reuses `app.capability_reality` (migration 0048)

## Purpose

Minimum durable self-model so MainAI knows, from **evidence**:

- what she can do (proven)
- what failed / repeatedly fails
- where founder intervention was required
- what improved / regressed / remains weak

## Hard rule

**Model confidence alone is NOT evidence.**  
`proven` requires `verified_available` **and** a real proof timestamp (`last_verified_at` / `last_success_at`).

## API

```python
from app.self_model import build_self_model, record_proven_capability, record_failed_capability

snap = build_self_model(db, owner_id=...)
# snap.proven / .failed / .repeatedly_failing / .regressed / .weak / .improved
# snap.entries[*]: success_count, failure_patterns, regression_history, next_improvement_candidate
```

No second capability store — projects over existing `capability_records` + append-only observation events.
