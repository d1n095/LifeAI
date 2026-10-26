# Independent closure verification — PR #313 `5cb9b46` (large-file corrections)

- **Candidate SHA:** `5cb9b46d6471213ec430c88fccc936514c4e0e39`. **Base:** #311 `c3373f9`.
- **Delta:** `large_file_lane/stream.py` (+15, resume re-hash), `session.py` (+133, new resumable
  session), `isolation.py` (+65), `__init__.py` (+11).
- **Suite:** `probes/fc313_v1` — **7 passed** on SMALL fixtures (≤ ~1 MB). **No 5 GB run.**
  Read-only; no DB/network/paid calls. `first_cycle_go=false`.

## Verdict: CHECKPOINT-HASH CLOSED + resumable-session hardening verified
| Property | #313 behaviour (inspected + tested) | Evidence |
|---|---|---|
| **CHECKPOINT-HASH (P2) closed** | On resume, `process` re-reads the prefix `[0, offset)` from the source to rebuild the sha256 state, then seeks to `offset`; resumed whole-file hash == single-pass hash | FC313_resume_hash_equals_single_pass |
| Resume stays bounded-memory | Prefix re-hash reads in `chunk_size` units; `peak_buffer ≤ one chunk` after resume | FC313_resume_stays_bounded_memory |
| Resumable sessions + crash recovery | `ResumableUploadSession` persists a manifest; `load()` from disk resumes at the durable offset; `finalize()` sha == content sha | FC313_session_crash_recovery |
| Integrity / backpressure guards | `write_part` rejects empty / > chunk_size / > declared_total / after finalize; `finalize` refuses when incomplete | FC313_session_backpressure, FC313_finalize_incomplete |
| Input validation | `create` rejects non-positive `declared_total_bytes` / `chunk_size` | FC313_create_validates |
| Honest 5 GB reporting preserved | `five_gb_not_supported()` still all-False; no 5 GB claim introduced | FC313_five_gb_still_unsupported |

### Detail
- **CHECKPOINT-HASH fix is correct and bounded:** the prior defect (resume reset the hasher and
  hashed only post-offset bytes) is fixed by re-hashing the prefix from the same source in
  `chunk_size` reads. The resumed hash now equals both the single-pass lane hash and
  `hashlib.sha256(full)`. Peak buffer remains one chunk, so the fix does not reintroduce an
  unbounded read. Cost note: re-hashing the prefix is O(offset) I/O on each resume — acceptable for
  correctness; at 5 GB this is measured by Gate-2 G2-3, not asserted here.
- **ResumableUploadSession** is filesystem-backed (no new Alembic table, no production HTTP route per
  its own docstring), durable across a simulated process restart, and enforces the declared-total and
  chunk-size bounds plus post-finalize immutability. `stored/indexed/understood` separation preserved
  (`indexed=False`, `understood=False` on finalize).

## Scope / non-claims
- Verified at **MB scale only**. Small-fixture success is **not** 5 GB verification. The measurable
  Gate-2 spec (`LARGE_FILE_GATE2_SPEC.md`, G2-1..10 incl. 5×5 GB) still governs 5 GB acceptance and
  must run under **authorized** large-scale resources; its precondition **G2-PRE-1 (CHECKPOINT-HASH)
  is now satisfied**, so the GB-scale resume-integrity criterion (G2-3) can be exercised when resources
  are authorized. The other #311 properties (single-pass bounded memory, corruption detection,
  interactive isolation) are in `stream.py`/`isolation.py` core that is unchanged except the resume
  block; fc313 re-confirmed bounded memory + state separation, so no regression is indicated.

## Verdict
**#313 `5cb9b46`: CHECKPOINT-HASH is independently CONFIRMED CLOSED; resumable sessions, crash
recovery, integrity/backpressure guards, input validation and bounded-memory-on-resume are verified at
MB scale, and the lane still honestly reports 5 GB as unsupported.** No new defect. **NOT CERTIFIED for
5 GB / Gate-2** — that requires the authorized large-scale measurement (G2-1..10). No merge/deploy/GO,
no 5 GB run, no paid calls. Target worktree unchanged.
