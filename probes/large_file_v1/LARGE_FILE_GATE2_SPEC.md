# Gate-2 acceptance specification — multiple 5 GB files (incl. 5 × 5 GB)

Measurable acceptance for the #311 large-file lane at production scale. **This spec defines what must
be measured; it is NOT a claim that 5 GB works, and these criteria must NOT be run without authorized
large-scale resources** (≥ ~60 GB scratch disk for 5×5 GB + checkpoints, a time budget of tens of
minutes, and Founder sign-off on the resource spend). The examiner proved the lane's *shape* at MB
scale (`large_file_v1`, 8 passed / 1 xfailed); the criteria below are the same properties measured at
GB scale. `five_gb_not_supported()` must stop returning False only after ALL criteria below pass on a
frozen SHA.

## Preconditions (must hold before any GB-scale run is authorized)
- **G2-PRE-1 CHECKPOINT-HASH fixed:** a resumed import MUST produce a sha256 identical to the
  single-pass full-file hash (today it does not — see finding CHECKPOINT-HASH, P2). Without this, no
  5 GB resume can be integrity-verified, so Gate-2 cannot start.
- **G2-PRE-2** HTTP upload path limits are documented and the lane's ingestion source (direct file /
  object store) is explicit — the 5 GB target does not change the HTTP body cap.

## Measured criteria (each a pass/fail with a recorded number)
| ID | Criterion | Measurement | Threshold |
|---|---|---|---|
| G2-1 | Bounded memory, single 5 GB | peak RSS of the worker over one 5 GB stream | ≤ `k · chunk_size · in_flight` and **independent of file size** (e.g. ≤ 256 MB with 64 KB chunks); no growth with bytes_seen |
| G2-2 | Bounded memory, 5 × 5 GB concurrent | peak RSS with 5 concurrent lanes | ≤ `k · chunk_size · total_in_flight`; still size-independent; no OOM |
| G2-3 | Offset-checkpoint integrity | kill -9 at random offsets, resume; final sha256 | == single-pass sha256 of the same file (requires G2-PRE-1) for every file |
| G2-4 | Crash/restart recovery | crash each of the 5 lanes once, resume all | all 5 reach `bytes_seen == file_size`, exactly-once (no re-processed bytes, no truncation) |
| G2-5 | Concurrent isolation / responsiveness | interactive request p99 latency while 5×5 GB run | ≤ an agreed bound (e.g. p99 ≤ 250 ms), i.e. large-file work never starves interactive |
| G2-6 | Cancellation + backpressure | cancel 1 of 5 mid-stream; submit a 6th | cancelled lane stops promptly and frees its slot; 6th is queued/backpressured per `BackgroundIsolation`, never silently dropped |
| G2-7 | File-integrity / corruption | flip bytes in a stored 5 GB file, re-verify | hash mismatch detected and surfaced (not silently accepted) |
| G2-8 | Separation of states | after ingest of all 5 | each checkpoint: `stored=True, indexed=False, understood=False` unless a separate indexing step ran; no implicit "understood" |
| G2-9 | Honest limit reporting | API/status during and after | reports stored≠indexed≠understood, the real file-size/format limits, and never claims 5 GB "supported" until G2-1..8 pass on a frozen SHA |
| G2-10 | Disk/exhaustion behavior | fill scratch toward capacity during 5×5 GB | fails closed with a clear error + resumable checkpoint; no partial/corrupt "success" |

## Resource authorization (required, not examiner-grantable)
- Dedicated scratch volume ≥ 60 GB; CPU/IO budget for ~25 GB of hashing; wall-clock budget agreed by
  the Founder. These are paid/holdable resources → **out of examiner scope**; do not provision or run
  without explicit Founder authorization. `first_cycle_go=false`.

## Miniature proxy already runnable (no authorization needed)
`probes/large_file_v1` runs G2-1/3/4/6/7/8/9 shapes at ≤ 2 MB (bounded memory independent of size,
corruption detection, state separation, cancellation, crash-resume offset recovery, honest limits).
G2-3 (resume-hash integrity) is the `xfail(strict)` CHECKPOINT-HASH today; it flips to PASS when
G2-PRE-1 is fixed, and the same assertion then scales to GB under authorization.
```
probes/large_file_v1/run.sh ../../wtcert-c3373f9   # 8 passed, 1 xfailed (CHECKPOINT-HASH)
```
