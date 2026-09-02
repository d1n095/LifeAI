# MainAI Safe-Internal Boundary

**Phase:** `READY_FOR_SAFE_INTERNAL_RUN`  
Claude/provider gates still block provider and serious autonomy. They do **not**
block safe-internal startup, offline MainAI, local learning, local workforce,
memory, planning, or daily internal use.

## Allowed now

- local reasoning
- memory retrieval
- planning (never authority)
- local dry-run workers (`activate_provider=False`)
- internal analysis
- local school / practice / exams (simulated or offline teachers)
- internal verification
- durable notes / continuity checkpoints
- project/status updates inside safe-internal scope

## Not allowed until later independent gates

- real external provider invocation
- consequential external writes
- autonomous production deploy
- money movement
- deletion
- approval on founder's behalf
- authority widening

## Invariants

EXTERNAL MODEL ≠ MAINAI · TEACHER ≠ TRUTH · LEARNING ≠ AUTHORITY ·
API FAILURE ≠ MAINAI FAILURE · PROVIDER ENABLED = FALSE in this phase
