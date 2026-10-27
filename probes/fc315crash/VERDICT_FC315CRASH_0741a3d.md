# #315 — crash-consistency & finalized-file integrity (O-1 escalation)

- **Subject:** #315 head SHA `0741a3d5d7bda27f895fc382e1b4c8b1971aaa44` (docs-only child of
  product SHA `292a091`; the resumable store tree is byte-identical). The implicated code is
  the inherited `#313` session object `app/large_file_lane/session.py`, unchanged at
  `292a091` and `0741a3d5`.
- **Suite:** `probes/fc315crash_v1` — **6 passed, 1 xfailed(strict)** (`RESULTS_0741a3d.txt`).
  Store-level fault injection on isolated tmp fixtures. No DB, no network, no paid calls, no
  product modification; target worktree unchanged. `first_cycle_go=false`.
- **Does not repeat** fc315_v1 or fc315http_v1 (no HTTP auth/quota/normal-restart here).

## VERDICT: O-1 is REACHABLE through realistic crash/recovery. Severity **P2** (CONFIRMED).

The fc315http_v1 verdict recorded O-1 as *low/informational, not reachable through the HTTP
routes*. That HTTP-reachability statement still holds — **no single crafted HTTP request**
triggers it — but this examination shows O-1 **is** reached by an ordinary operational event:
a **process termination during a write**, followed by a **correct** resumable-client resume.
That is a realistic production path (any `kill -9`, OOM-kill, container stop, deploy, or crash
mid-request), so the real severity is **P2: silent corruption of finalized content, reported
to the client as a complete, consistent, stored file.**

### Mechanism (read-verified + executed)
`write_part()` does: append+`flush()` to `spool.bin` → `manifest.offset +=` → `_persist()`
(atomic manifest rename). There is **no `fsync` anywhere** (no durability confirmation), and
on resume the spool is opened `"ab"` and is **never reconciled/truncated** to
`manifest.offset`. `finalize()` hashes the **whole spool to EOF** (not bounded by
offset/declared_total) and gates completeness on `manifest.offset` only (**never re-stats the
spool length**). Therefore a kill in the window *after the spool flush but before the manifest
persist* leaves the spool **ahead** of `manifest.offset`; a correct client then resumes from
the reported (lower) offset and re-sends those bytes, which are **appended again**. Result:

```
declared_total = 256 KiB
after kill:      spool = 192 KiB, manifest.offset = 128 KiB
correct resume:  re-sends from 128 KiB -> spool = 320 KiB, manifest.offset = 256 KiB
finalize():      outcome=stored, complete=True, finalized=True, offset=256 KiB   <-- says OK
actual file:     320 KiB, with the third 64 KiB chunk DUPLICATED
sha256:          hash of the 320 KiB corrupted bytes, != hash of intended 256 KiB
```

### Evidence tests
| ID | What it proves | Result |
|---|---|---|
| **CC1** | **Reproducible FAILING test for Cursor.** Asserts the integrity invariant that must hold after a kill-between-append-and-persist + correct resume: finalized file length == `declared_total` **and** sha256 == intended content. Currently violated. | **xfail(strict)** — flips to a red XPASS the moment the product is fixed |
| CC2 | Pins the exact corrupt terminal state: status `stored/complete/finalized`, `offset==declared`, yet the file is `declared+64KiB` with a duplicated chunk and the sha is of the corrupted bytes | PASS (evidence) |
| CC3 | A **torn/partial** persisted write corrupts at an arbitrary (non-chunk) offset — file ends `+20000` bytes, still reported complete | PASS (evidence) |
| CC4 | **Item 4:** a declared-complete session can finalize **fewer** bytes than declared (lost last chunk, manifest intact) — reported `stored/complete`, file short, checksum over fewer bytes | PASS (evidence) |
| CC5 | **Item 3:** a corrupt `manifest.json` is **fail-closed** on fresh-process recovery — `get()`/`finalize()` raise (never return a valid/stored row); `reserved_bytes()` swallows it (quota under-accounting, minor) | PASS |
| CC6 | **Item 6:** concurrent `finalize` vs a straggler resume-write take the same per-session flock — manifest stays intact and parseable, offset never advances past `declared_total`, straggler rejected; coherent terminal state | PASS |
| CC7 | **Item 7:** corrupted content **is** presentable downstream as valid stored content — the finalized row carries `stored/finalized/complete + sha256` and exposes **no** actual-spool-length field, and create accepts **no** client expected-checksum to verify against | PASS (trust boundary) |

### Mutual-consistency finding (item 5)
After the crash path, **finalized checksum, actual file length, manifest metadata and
client-visible status are NOT mutually consistent**: `manifest.offset`/`status.offset` =
`declared_total`, but actual file length ≠ `declared_total`, and the sha256 is over the actual
(corrupted) bytes. The only integrity signal the client receives is that sha — and because no
expected checksum is supplied at `create`, there is **no end-to-end integrity contract** by
which either the server or the client can detect the corruption.

### Trust boundary (where it is safe)
- **Out-of-band tampering** inside the owner's session directory remains out of scope (the
  attacker already has owner-scoped filesystem write) — that was the original O-1 framing.
- The corruption requires a **crash in a specific sub-millisecond window** per in-flight part;
  it is probabilistic per upload, but across many large multi-part uploads and ordinary
  deploy/restart churn it is a *when*, not an *if*, and it is **100% deterministic to
  reproduce** (CC1/CC2), so it is a real durability defect, not a theoretical one.
- Concurrency and manifest-corruption recovery are **sound** (CC5/CC6): the flock serializes
  operations and a corrupt manifest fails closed.

### Recommended fix (for Cursor; examiner does not modify product)
Any one of these closes CC1; all three is belt-and-suspenders:
1. **Reconcile on resume:** on `load()`/first `write_part`, truncate `spool.bin` to
   `manifest.offset` (`os.truncate`) before appending — heals an over-long spool.
2. **Bound finalize + verify length:** in `finalize()`, assert
   `spool.stat().st_size == manifest.offset == declared_total_bytes` and hash only
   `[0, declared_total)` — fail closed (e.g. `409 corrupt_spool`) otherwise.
3. **Durability:** `fsync` the spool after each part and `fsync` the directory after the
   manifest rename, so `manifest.offset` can never describe bytes that a crash discarded.
Optionally accept a client `expected_sha256` at `create` and verify it at `finalize` for a
true end-to-end integrity contract.

## Scope / discipline
Read-only; store-level fault injection only; no HTTP re-runs; no DB/network/providers; no 5 GB
run; no merge/deploy/GO; Codex/Cursor branches unmodified; target worktree unchanged.
`first_cycle_go=false`.

## Reproduce
```
probes/fc315crash_v1/run.sh ../../wtcert-0741a3d     # 6 passed, 1 xfailed (CC1 = the fix target)
```
When Cursor publishes a corrected successor, re-run against that exact SHA: **CC1 must flip to
PASS** (XPASS under strict → update this suite), and CC2/CC3/CC4 (which pin the *broken*
behaviour) must then be revised to assert the corrected invariant.

## Next actionable blocker
Codex's effect-specific database-authority candidate `460e8ad8cbd40b7fb0d62702f28c14412688e298`
is to be examined on its exact SHA **only once the verified archive is accessible** — not yet
delivered. No other authorized task is executable without it or a corrected #315 successor.
