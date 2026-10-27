# #315 — ACTUAL HTTP integration acceptance (production routes, authenticated clients)

- **Subject:** #315 head SHA `0741a3d5d7bda27f895fc382e1b4c8b1971aaa44`.
  Its parent is **`292a09110a2528871d2f537987c37050416f3707`** (the #315 product SHA I
  storage-tested in `fc315_v1`); `0741a3d5` is **docs-only** over it
  (`docs/BRANCH_REGISTRY.md`, `docs/LARGE_FILE_RESUMABLE_HTTP.md`), so the resumable
  **router + store tree is byte-identical** to `292a091`. Verified by
  `git diff --stat 292a091 0741a3d5` → 2 docs files, 4 insertions / 3 deletions.
- **Suite:** `probes/fc315http_v1` — **14 passed** (`RESULTS_0741a3d.txt`). Drives the REAL
  FastAPI routes (`app.routers.library_resumable`) through the target's own
  `tests.conftest` `TestClient`: genuine login cookies + CSRF, real HTTP status codes, real
  streamed part bodies, real cross-instance restart, real threaded HTTP concurrency.
  Loopback-only (egress guard refuses non-loopback); no paid calls; no 5 GB run; no
  production mutation; target worktree unchanged. `first_cycle_go=false`.
- **Not a repeat of `fc315_v1`:** S1–S12 drove the **store object directly** + one
  source-inspection auth check. This suite exercises **route-to-store integration** and
  **route-only boundaries** S1–S12 never reached.

| ID | HTTP boundary exercised | Result |
|---|---|---|
| HT1 | Founder full lifecycle over HTTP: POST create → PUT parts (streamed bodies, monotonic offset) → GET → POST finalize (`stored`, sha256 == sha of bytes, `indexed/understood=false`) → GET list → DELETE abort → GET 404 | PASS |
| HT2 | `require_founder` at the wire: **unauthenticated → 401** on all six routes; **authenticated non-founder → 403** (role+id pinned to `FOUNDER_USER_ID`) | PASS |
| HT3 | Mutating routes enforce **CSRF**: POST without `X-CSRF-Token` → 403 even with a valid session cookie; with it → 200 | PASS |
| HT4 | Malformed input mapping: non-UUID id → **400** `invalid_session_id`; oversize via **Content-Length → 413** (before streaming); oversize via **chunked stream → 413** (in-stream guard); empty part → **409** `part_rejected`; overflow declared_total → **409**; finalize incomplete → **409** `incomplete` | PASS |
| HT5 | **Cross-owner by path:** a session existing on disk under a different owner is **404** on GET/PUT/finalize/DELETE and absent from the founder listing — owner derives from the token, never the URL | PASS |
| HT6 | **Concurrent HTTP PUTs** (4 threads × 8 parts, own clients): flock serialization holds through the route — final offset == declared total **exactly**, never exceeded; all 200; finalize `stored` | PASS |
| HT7 | **Restart/resume across client instances:** a fresh `TestClient`/app handle over the same `storage_root` recovers offset purely from `manifest.json`+`spool.bin`, resumes, finalizes with the correct whole-file sha256 | PASS |
| HT8 | **Owner-quota exhaustion → HTTP 413** `owner_quota_exceeded` (store injected with a small quota via the route's own `get_resumable_store` dependency seam) | PASS |
| HT9 | **Disk-headroom gate → HTTP 507** `disk_headroom_insufficient` (injected `disk_free_fn`) | PASS |
| HT10 | **Abort/finalize race** (concurrent finalize + abort): both take the same per-session flock; each returns a coherent status (no 5xx); terminal on-disk state agrees with the responses (abort-won ⇒ GET 404; finalize-only ⇒ stored) | PASS |
| HT11a | **Corrupted same-length spool:** finalize truthfully reports the sha256 of the **bytes actually on disk** (≠ original) — the store never claims the original content; no content hash is declared at create to verify against | PASS |
| HT11b | **Truncated spool** (out-of-band, manifest left intact): finalize reports `stored`/complete with a hash over the shorter bytes — see Observation below | PASS (records observed behaviour) |
| HT12 | **Stored-vs-indexed honesty at the wire:** every create/get/list/finalize body carries `five_gb_supported=false`, `five_by_five_gb_measured=false`, `stored_is_not_indexed=true`, `caddy_request_body_unchanged=true`, `first_cycle_go=false`; finalize `stored=true, indexed=false, understood=false` | PASS |
| HT13 | **Inherited per-minute limiter is wired:** with the limiter re-enabled, the `rate_limit_library_import_per_minute+1`-th create in the window returns **429** (≤ limit succeed) | PASS |

## Observation (not attributed to #315; inherited from the #313 store at `292a091`)

**O-1 — finalize completeness is manifest-driven, not re-verified against live spool length
(HT11b).** `ResumableUploadSession.finalize()` gates on `manifest.offset ==
declared_total_bytes` and then hashes whatever `spool.bin` currently holds; it does not
re-stat the spool against `declared_total_bytes`. If the spool is truncated **out of band**
after its parts were accepted (while the manifest is left intact), finalize still reports
`stored`/`complete` with a sha256 over fewer bytes than declared. Assessment:

- **Severity: low / informational.** Under the normal path `manifest.offset` is only ever
  advanced by a successful `write_part` append, so offset == spool length by construction;
  reaching the divergence requires an actor that can already write arbitrary bytes inside
  the owner's session directory (post-auth, owner-scoped filesystem) — i.e. it is **not**
  reachable through the HTTP routes, which was the subject under test. The route layer adds
  no new exposure here.
- **Not a #315 regression.** The behaviour lives entirely in the inherited `#313` session
  object (`app/large_file_lane/session.py`), unchanged at `292a091` and `0741a3d5`.
- **Hardening option (for whoever owns the store, not required for #315):** at finalize,
  assert `spool_path.stat().st_size == manifest.offset == declared_total_bytes` before
  hashing, so an out-of-band truncation fails closed rather than finalizing short. HT11b is
  written to flip the moment such a check lands.

No defect was found in the route-to-store integration. The HTTP surface fails closed on
auth, CSRF, ownership, part bounds, quota and disk, and never over-claims 5 GB support,
indexing or understanding.

## Verdict
**#315 `0741a3d5` PASSES actual HTTP integration acceptance (fc315http_v1, 14/14).** The
production resumable routes enforce Founder-only + CSRF + owner-scoping at the wire, map
every store error to the correct HTTP status (400/401/403/404/409/413/507/429), serialize
concurrent writes and resume across restarts correctly, and report stored-vs-indexed state
truthfully. One inherited, low-severity, non-HTTP-reachable integrity observation (O-1) is
recorded, not attributed to #315. This is **not** a 5 GB certification and performs no 5 GB
run or production mutation (`five_gb_supported=false`). Read-only; no merge/deploy/GO.
`first_cycle_go=false`.

## Reproduce
```
probes/fc315http_v1/run.sh ../../wtcert-0741a3d     # 14 passed
```
Pending Codex handoff (per the assignment): when the `460e8ad8` archive arrives, independently
examine its new **effect-specific database authority** changes on that exact SHA before any
certification.
