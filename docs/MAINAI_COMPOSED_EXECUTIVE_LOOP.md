# MainAI Composed Executive Loop

Status: **IMPLEMENTED (planning/dry-run composition)** on branch
`cursor/mainai-composed-executive-loop`.

## What this is

One composed path that wires existing subsystems into an executive cycle:

UNDERSTAND → CONNECT → PLAN → ACT(dry) → VERIFY → STORE → LEARN → CONTINUE

with durable continuity across restart.

Package: `backend/app/mainai_executive/`

| Module | Role |
|---|---|
| `loop.py` | `run_executive_cycle` / `resume_executive_cycle` |
| `lookaround.py` | active_context + lessons + bounded WorkCandidates |
| `continuity.py` | durable checkpoint via founder_memory (no new table) |
| `missing_piece.py` | reuse existing packages before proposing new systems |
| `completion.py` | CODE WRITTEN ≠ DONE dimensions |
| `observability.py` | founder-facing status snapshot |

## Hard invariants (enforced in results / checkpoints)

- MEMORY ≠ AUTHORITY
- FUTURE PLAN ≠ FUTURE AUTHORITY
- STAFFING DECISION ≠ AUTHORITY
- WORKFORCE DRY RUN ≠ PROVIDER ACTIVATION
- PROCESS MEMORY ≠ AUTHORITY
- MODEL OUTPUT ≠ AUTHORITY

Executive cycle **never** calls `authorize_work_candidate` and **never** sets
`activate_provider=True`.

## Glue wired in this increment

1. Executive lookaround orchestrator (was docs-only §2).
2. `founder_add_memory_note` / `founder_correct_memory_note` → `apply_memory_work_linkage` (park only).
3. `score_candidates` consults `capability_reality` read-only (downrank gaps; never invent status).
4. Durable restart via continuity checkpoints.
5. Scenarios A–F as tests (safe internal).

## Not claimed done

- Claude-owned gates (#218, #229, #213, #224) — still UNKNOWN; do not self-verify.
- Provider invoke — remains disabled (PR #234 staging).
- Full personal-language resolution (#218 path).
- Production “serious autonomous run” readiness.

## Operator note

```python
from app.mainai_executive import run_executive_cycle, resume_executive_cycle, executive_status_snapshot

result = run_executive_cycle(db, owner_id=..., founder_request="...", source_entity_id=...)
# kill process here — resume from durable checkpoint only
resume_executive_cycle(db, owner_id=..., session_id=result.session_id)
executive_status_snapshot(db, owner_id=..., session_id=result.session_id)
```
