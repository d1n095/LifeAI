# Grandmaster remediation verification suite (examiner-owned)

Prepared 2026-10-10 by the independent examiner for the NEXT Grandmaster builder submission.
It is **not** builder code and is never committed to the repository.

## Status of record
- 851fdc74b9827f3cb67dc60ab19ad9cd6e40d0b1: examined, **RETURN_TO_BUILDER** (9 P1). Not passing.
- No newer Grandmaster SHA has been submitted (remote branch still at 851fdc7 when this suite was prepared).
- A verdict on a new SHA is issued **only** after an explicit examination assignment.

## How to run (never inside an examined worktree's tree)
```
./run.sh <path-to-target-worktree>      # e.g. ../../wtgm  (frozen 851fdc7 = negative control)
```
`run.sh` puts `<target>/backend` on PYTHONPATH, disables bytecode writes, uses this directory as
pytest rootdir (the backend DB conftest is NOT loaded; the kernel is pure Python) and then confirms the
target worktree is still clean.

## Adapting to a new API
Edit **only** `adapter.py` (examiner file) and record the change here:
- injected clock: `make_kernel(..., clock=)` looks for a `clock`/`now_fn`/`time_source`/`utc_now` kwarg.
- durable leases: `make_kernels_sharing_durable_lease_store()` must return two kernels wired to the
  builder's shared durable store (two "processes"). Fails closed until wired.
- fencing token: `fencing_token(lease)` looks for `fencing_token`/`generation`/`lease_generation`/`epoch`/`token`.
Hooks that cannot exercise the property raise `PropertyNotExposed` → the test **fails** ("P1-n OPEN").
Nothing is skipped or xfailed.

## Test plan → tests
| # | Property (secure behaviour required) | Tests |
|---|---|---|
| P1-1 | Kernel default paths use an injected clock; fixtures never expire with real time | `test_p1_1_*` (wall clock skewed ±10y; fixtures anchored to 2031) |
| P1-2 | One active execution/lease per agent | `test_p1_2_*`, concurrent test |
| P1-3 | One active lease per task; planner batch ≤1 move per task | `test_p1_3_*`, concurrent test |
| P1-4 | Canonical workspace path isolation incl. aliases (`/`, `//`, `/./`, `..`) and duplicate keys | `test_p1_4_*` (case-variant recorded as INFO only) |
| P1-5 | RESERVED/UNDER_EXAMINATION/CERTIFIED: examiner workspace + examined/frozen branch never mutable | `test_p1_5_*` (+ fixture guard proving ownership/collision doesn't mask) |
| P1-6 | After RETURN_TO_BUILDER the gate itself (validate_move + acquire_lease) requires new child branch AND workspace | `test_p1_6_*` (+ control) |
| P1-7 | Explicit allowlist; merge/deploy/release/activation/credential variants never ASSIGNABLE | `test_p1_7_*` (23 variants + control) |
| P1-8 | Durable lease coordination across processes/restart; monotonic fencing; stale owner cannot heartbeat/release | `test_p1_8_*` |
| P1-9 | Builder of any execution on the task can't certify; FREEZE only from trusted verdict provenance | `test_p1_9_*` (+ control) |
| P2 | Dependency truth from task state, cycles (2/3-node), self-dependency | `test_dep_*` |
| P2 | Idempotent re-ingestion; reports never mutate authority; distinct events; reports survive refresh | `test_reports_*` |
| P1/P2 | Concurrent writers: leases/ingest/refresh race — no exceptions, no duplicate workspace/agent/task leases | `test_concurrent_writers_keep_invariants` |

Fixture realism notes: examiner execution/workspace are DETACHED (no branch), as real examiner
worktrees are; the original builder (codex) owns the examined branch. `test_p1_5_fixture_guard_*`
proves rejections come from the examination/freeze rules, not incidental branch ownership/collision.

## Baseline on 851fdc7 (negative control, 2026-10-10)
51 failed / 16 passed. Every P1-1..P1-9 failure reproduces; the 16 passes are the legitimate
already-holding behaviours (active UNDER_EXAMINATION branch protection, 7 exact denylist strings,
controls, report non-authority, distinct events, meta/fixture guards, case-variant INFO).

New findings surfaced while building the suite (on 851fdc7):
- P2: `refresh()` discards every previously ingested report event (rebuilt board keeps only snapshot reports).
- P2: self-dependency (t1 → t1) accepted.
- Concurrency: under thread contention one agent received two leases and one task was leased twice
  (P1-2/P1-3 reproduce concurrently); no exceptions and no workspace double-lease.

## Examination of 6e08430 (codex/mainai-grandmaster-orchestration-kernel-p1-fix), 2026-10-10
Adapter rebound to the new API (examiner files only; 851fdc7 versions kept as adapter_851fdc7.py / *_851fdc7.py.txt):
Clock object wrapper; every kernel gets a fresh SqliteCoordinationStore; shared-store kernels = two store objects on one
file; registry_evidence() builds the payload-bound pointer; fencing test passes tokens (old AND new); P1-1 move uses a
registered execution id (artifact fix); utc_now monkeypatch tolerant (raising=False).
New file test_gm_6e08430_architecture.py (questions C-F, self-cert variants, freeze durability, real OS-process race).
Result: regression 64 passed / 3 failed; architecture 6 passed / 14 failed (17 failed / 70 passed total).
Builder's own test file: 57 passed. Target worktree unchanged.
