# Night run report — MainAI autonomy hardening (2026-08-27)

## PRs

| PR | Head | Status | Claim |
|---|---|---|---|
| [#181](https://github.com/d1n095/LifeAI/pull/181) | `b79219a` → tip `e10ae97` | **MERGED** | Supervisor spend fail-fast / defense-in-depth (Outcome B; mutation-proven) |
| [#182](https://github.com/d1n095/LifeAI/pull/182) | `81a73b7` | OPEN — CI unit pending | Crash before settle → refuse re-invoke; released source_ref fail-closed |
| [#183](https://github.com/d1n095/LifeAI/pull/183) | `17ff34c` | OPEN — CI unit pending | Heal Operator write after crash before audit |
| [#184](https://github.com/d1n095/LifeAI/pull/184) | `8f44941` | OPEN — CI unit pending | Job lease expiry at Operator write effect time |

Tip at report time: `e10ae97` (#181). Claude already merged #178–#180 (concurrent budget / lease / revoke-reserve).

## Adversarial attacks completed (local proof)

1. **#181 reframe** — Claude negative control reproduced; Supervisor mutation test fails pre-#181 / passes post; claim reframed to fail-fast not TOCTOU-close; merged.
2. **Phase 2 concurrent near-exhausted budget** — already on tip via Claude #178 (two-thread barrier). No redesign.
3. **Phase 3 crash-before-settle (#182)** — red→green: second adapter call blocked; conservative settle; released source_ref cannot resurrect; `:aN` allocation for retries.
4. **Phase 4 local write crash (#183)** — red→green: on-disk after-hash heal without rewrite.
5. **Phase 5 lease effect (#184)** — red→green: expired job lease blocks write with zero FS mutation; takeover generation bump same.

## Negative controls

| Test | Pre-fix | Post-fix |
|---|---|---|
| Supervisor fail-fast skips `plan_with_provider` | FAIL (entered) | PASS |
| Crash-before-settle no 2nd invoke | FAIL (2nd call) | PASS |
| Write crash heal | FAIL (before-hash) | PASS |
| Expired job lease write | FAIL (write ok) | PASS |

## Defects found

- Stale tick-start spend boolean entered planning unnecessarily (fail-fast; inner reserve already safe).
- Crash-before-settle allowed **re-invoke** under same reserved source_ref.
- Released source_ref could be returned as if live (free invoke hole) — fail-closed + `:aN`.
- Disk write before audit left resume stuck on before-hash mismatch.
- Operator did not check `job.lease_expires_at` at effect time.

## Unresolved / next

- **CI merge gate:** #182/#183/#184 unit jobs still running (long pytest); merge in order when each exact head is green; rebase promptly on tip drift.
- **Phase 6** founder cancel matrix — not started.
- **Phase 7** recovery empty-cap — partially closed by #177; no recovery path builds OperatorContext today; add explicit regression if a reconstruction path appears.
- **Phase 8** composed soak — not started.
- Highest-risk next edge after open PRs land: **founder cancel mid-flight matrix**, then soak.

## Do not weaken

Authority not weakened to pass tests. Claims matched evidence (#181 reframed when security claim was overstated).
