# Independent examination — PR #311 `c3373f9` (large-file streaming lane)

- **Candidate SHA:** `c3373f93cb6d3881ed6560cdd106ac1852c54a1e`. **Base:** #306 `2444bcc` (parallel to
  #309/#310). Both exact-SHA workflows reported green (user-stated; not re-read here, no GH creds).
- **Delta vs 2444bcc (142 insertions, backend):** new `app/large_file_lane/` — `stream.py` (109,
  `StreamingImportLane` / `ImportCheckpoint` / `five_gb_not_supported`), `isolation.py` (24,
  `BackgroundIsolation`), `__init__.py`.
- **Suite:** `probes/large_file_v1` — **8 passed, 1 xfailed(strict)**. Small fixtures (≤ 2 MB),
  pure functions, no DB/network. **No 5 GB or expensive large-scale run was performed.** `first_cycle_go=false`.

## 1. Actual implementation status
| Requirement | Status | Evidence |
|---|---|---|
| Bounded-memory streaming | **IMPLEMENTED** | LF1 peak buffer ≤ one chunk, bytes_seen correct, size-independent |
| Offset-checkpoint integrity | **DEFECTIVE (hash)** | LF9 xfail — resumed sha256 ≠ single-pass hash |
| Crash/restart recovery (offset/bytes) | **IMPLEMENTED (offset only)** | LF7 resume reaches full `bytes_seen`/`offset` |
| Concurrent workload isolation | **IMPLEMENTED (model)** | LF8 interactive always admitted; background bounded |
| Cancellation + backpressure | **IMPLEMENTED** | LF6 callback cancel stops processing (handle closed); LF8 backpressure |
| File-integrity / corruption handling | **IMPLEMENTED (single pass)** | LF2 hash==content; LF3 flipped byte changes hash |
| Separation of stored / indexed / understood | **IMPLEMENTED** | LF4 `indexed=False, understood=False`; `stored_vs_understood` |
| Honest upload/format-limit reporting | **IMPLEMENTED** | LF5 `five_gb_not_supported()` all False; http limit unchanged |

## 2. Test results & reproduce
`probes/large_file_v1/run.sh ../../wtcert-c3373f9` → 8 passed, 1 xfailed. (DB-free; pure functions.)

## 3. New defects (severity · owner)
- **CHECKPOINT-HASH (P2 · Cursor/MainAI):** `StreamingImportLane.process` on resume resets the sha256
  hasher and `seek(offset)` (stream.py:60-64), so a resumed import's final `sha256` covers only the
  bytes AFTER the checkpoint offset and silently differs from the single-pass full-file hash. A
  resumed large-file import therefore cannot yield a verifiable whole-file checksum — an integrity
  hazard that defeats corruption detection across a crash/resume. (sha256 state is not seekable; the
  checkpoint must persist the rolling hasher state, or the design must re-hash from 0, or store
  per-chunk Merkle/segment digests.) Confirmed executably: LF9 `xfail(strict)`. **This is a Gate-2
  precondition** (G2-PRE-1) — no 5 GB resume can be integrity-verified until it is fixed.
- **BUFFER-GUARD (P4 · Cursor, cosmetic):** the `MemoryError` guard checks a single chunk's length
  (`peak_buffer = max(peak_buffer, len(chunk))`), which can never exceed `chunk_size`, so the
  `> max_buffer_bytes` branch is effectively unreachable. Memory is genuinely bounded (only one chunk
  is held), so there is no leak — but the guard does not measure what its name implies. No functional impact.
- No STOP/spend/privilege surface is touched; no autonomy; `first_cycle_go=false`.

## 4. Missing requirements for first operational handover (Gate-2)
See `LARGE_FILE_GATE2_SPEC.md`. Blocking: **G2-PRE-1 (fix CHECKPOINT-HASH)**, then measured
G2-1..G2-10 on a frozen SHA under **authorized** large-scale resources (≥ 60 GB scratch for 5×5 GB).
`five_gb_not_supported()` must not flip until all pass. No HTTP upload-cap change is implied.

## 5. What can safely proceed while other work is blocked
- Fix CHECKPOINT-HASH (persist rolling hasher state in `ImportCheckpoint`, or segment-hash) — small,
  self-contained, and it flips LF9 to PASS at MB scale immediately, provably, with no large resources.
- The lane's bounded-memory / isolation / state-separation / honest-reporting contracts are sound and
  frozen-able as the Gate-2 miniature proxy; GB-scale measurement waits for Founder resource authorization.

## 6. Independent verdict
**#311 `c3373f9`: the large-file streaming lane is bounded-memory, cleanly separates
stored/indexed/understood, isolates interactive work, handles cancellation/backpressure, detects
single-pass corruption, and reports its 5 GB limit honestly.** It has **one P2 integrity defect
(CHECKPOINT-HASH)**: a resumed import cannot produce a verifiable whole-file checksum — a Gate-2
precondition. **NOT CERTIFIED for 5 GB / Gate-2**; the lane correctly reports 5 GB as unsupported. A
measurable Gate-2 spec (incl. 5×5 GB) is delivered and must run only under authorized resources. No
5 GB test was run. No merge/deploy/GO. `first_cycle_go=false`. Target worktree unchanged.
