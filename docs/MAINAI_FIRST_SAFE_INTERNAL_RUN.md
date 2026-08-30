# First SAFE INTERNAL MainAI Run — Runbook

Milestone: **READY_FOR_SAFE_INTERNAL_RUN**  
Separate from **LOW_RISK_PROVIDER_RUN**. Never silently escalate.

## Preconditions

- Integration tip includes workforce stack (#230–#232) and activation-prep (#234 when merged).
- `evaluate_startup_readiness()` is not `BLOCKED`.
- Kill switch not active.
- **No** activation gates need to be VERIFIED for this run.
- **No** external provider calls.
- **No** consequential writes (deploy/merge/delete/purchase/remote write).

## Command (library entrypoint)

```python
from app.workforce.safe_internal_run import run_first_safe_internal_mainai_run

report = run_first_safe_internal_mainai_run(db, owner_id=owner.id)
assert report.provider_invoked is False
assert report.consequential_writes is False
print(report.as_dict())
```

Or via pytest:

```bash
cd backend
unset DATABASE_URL APP_DATABASE_URL
pytest tests/backend/mainai/test_safe_internal_run.py -q
```

## What the run proves

1. Startup readiness inspectable (multi-level, not one boolean).
2. Organization / 9 departments inspectable (candidates, no fake promotion).
3. Workforce selector chooses from evidence for one harmless classify task.
4. Minimized context; credentials denied.
5. Dry-run worker → UNVERIFIED → independent verifier → VERIFIED.
6. Audit receipt covers the full chain.
7. Restart checkpoint recorded.
8. Kill switch arms and revokes live authority.

## After Claude verifies #218/#229/#213/#224

1. `record_gate_verification(key, status=verified, evidence_ref=...)` for each gate.
2. Re-run `evaluate_startup_readiness(claude_reviews_satisfied=True)`.
3. Require `READY_FOR_LOW_RISK_PROVIDER_RUN`.
4. Land dedicated enablement commit (`PROVIDER_INVOKE_ENABLED=True` only then).
5. One harmless real provider delegation → independent verify → stop and checkpoint.
