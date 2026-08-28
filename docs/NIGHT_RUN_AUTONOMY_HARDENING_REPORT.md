# Night run report — MainAI autonomy hardening (2026-08-27/28)

## PRs

| PR | Merge / head | Status | Claim |
|---|---|---|---|
| [#181](https://github.com/d1n095/LifeAI/pull/181) | tip `e10ae97` | **MERGED** | Supervisor spend fail-fast / defense-in-depth (Outcome B; mutation-proven) |
| [#182](https://github.com/d1n095/LifeAI/pull/182) | tip `f9cedcc` | **MERGED** | Crash before settle → refuse re-invoke; released source_ref fail-closed |
| [#184](https://github.com/d1n095/LifeAI/pull/184) | tip `0d12d54` | **MERGED** | Job lease expiry at Operator write effect time |
| [#183](https://github.com/d1n095/LifeAI/pull/183) | tip `bd04934` | **MERGED** | Heal Operator write after crash before durable audit |
| cancel-before-write | branch `cursor/founder-cancel-after-accept-before-write` | WIP → PR | Founder cancel after ACCEPT / before write; mid-driver stop; past effects preserved |

Current tip: **`bd04934`**. Claude #178–#180 already cover concurrent budget/lease/revoke-reserve.

## Adversarial attacks completed

1. **#181 reframe** — negative control reproduced; claim reframed to fail-fast not TOCTOU-close; merged.
2. **Phase 2 concurrent near-exhausted budget** — tip via Claude #178.
3. **Phase 3 crash-before-settle (#182)** — refuse 2nd adapter call; conservative settle; `:aN` retries.
4. **Phase 4 local write crash (#183)** — on-disk after-hash heal without rewrite.
5. **Phase 5 lease effect (#184)** — expired lease / takeover generation → zero FS mutation.
6. **Phase 6 slice (cancel after ACCEPT / before write)** — local proof green; negative control fails on tip without fix.

## Negative controls

| Test | Pre-fix | Post-fix |
|---|---|---|
| Supervisor fail-fast skips `plan_with_provider` | FAIL | PASS |
| Crash-before-settle no 2nd invoke | FAIL | PASS |
| Write crash heal | FAIL | PASS |
| Expired job lease write | FAIL | PASS |
| Cancel before write_file → zero FS | FAIL | PASS |
| Mid-driver cancel → 2nd write blocked / CANCELLED | FAIL | PASS |

## Defects found

- Stale tick-start spend boolean entered planning unnecessarily (fail-fast; inner reserve already safe).
- Crash-before-settle allowed re-invoke under same reserved source_ref.
- Released source_ref could resurrect as free hold — fail-closed + `:aN`.
- Disk write before audit left resume stuck on before-hash mismatch.
- Operator did not check `job.lease_expires_at` at effect time.
- Operator/driver did not refuse filesystem effects after mid-flight `cancel_requested` (checkpoint of past work must still be allowed).

## Unresolved / next

- Open/merge cancel-before-write PR on exact-head CI.
- Remaining cancel matrix boundaries (before planning; after verify; late finalize) if not already covered elsewhere.
- **Phase 7** recovery empty-cap — partially closed by #177; inspect worktree recovery reconstruction.
- **Phase 8** composed soak — not started.
- Highest-risk next edge after cancel PR: **governed-on-recovery**, then soak.

## Do not weaken

Authority not weakened to pass tests. Claims matched evidence (#181 reframed when security claim was overstated). Cancel fence is effect-time refuse for future mutations; past effects remain historical.
