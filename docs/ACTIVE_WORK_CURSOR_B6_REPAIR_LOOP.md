# ACTIVE WORK — Cursor Autonomy Night Shift (B6)

**Owner:** Cursor  
**Branch:** `cursor/repair-loop-across-ticks`  
**Integration tip at branch create:** `4c86e58` (#154)

## Claimed now

| Surface | Purpose |
|---|---|
| `backend/app/development_supervisor/service.py` (`_augment_bindings_with_gap_children` + reverify rebuild) | Durable repair across production-shaped ticks |
| `backend/tests/backend/mainai/test_repair_loop_across_ticks.py` | Adversarial proofs |

## Standing boundaries

- `provider_spend_authorized=false`
- `remote_write_authorized=false`
- Do not touch Claude-owned lanes

## Evidence

- Local: 3 new B6 tests + existing live repair test = **4 passed**
- Root cause: production plain WorkBindings shadowed gap children (`if task.id in known: continue`)
- Fix: replace plain bindings with durable gap-derived / reverify bindings across ticks
