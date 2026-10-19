# First-cycle staging composition

Machine-readable source of truth: `docs/first_cycle_staging_manifest.json`.
Codex follow-up: `docs/FIRST_CYCLE_CODEX_INTEGRATION.md`.

This candidate is the isolated successor of frozen `#285`
(`f5d9bf14340b0436d7470b28f6026342aeeaf3e6`) incorporating frozen `#286`
(`a255fa23bf29be624d3f24e6387f7ed25defce1e`). Do not amend `#285` or `#286`.

## Reproduce (no local machine state)

```sh
git fetch origin \
  f5d9bf14340b0436d7470b28f6026342aeeaf3e6 \
  a255fa23bf29be624d3f24e6387f7ed25defce1e
git checkout -b cursor/first-cycle-staging-p1-91c1 f5d9bf14340b0436d7470b28f6026342aeeaf3e6
git merge --no-ff a255fa23bf29be624d3f24e6387f7ed25defce1e
```

Verify ancestry:

```sh
git merge-base --is-ancestor e33f494c4fc53704c494cecea788f3e22be36330 HEAD  # #263
git merge-base --is-ancestor 09b87e9b70dc02889d39c2bae3d56458509d49cb HEAD  # #276
git merge-base --is-ancestor abc88b4f479489c8c85111d754756058ab77eb22 HEAD  # #281
git merge-base --is-ancestor 9c978ded05b05ad174ce003a3efad22682ec187d HEAD  # #284
git merge-base --is-ancestor 1e3ee3fd604fc540600f393e215a712b283686d4 HEAD  # #282
git merge-base --is-ancestor f5d9bf14340b0436d7470b28f6026342aeeaf3e6 HEAD  # #285
git merge-base --is-ancestor a255fa23bf29be624d3f24e6387f7ed25defce1e HEAD  # #286
```

`#280` @ `37bca08a287f00b6282222047340bffa949ff788` must **not** be an ancestor.

Prior `#285` composition (kept as git history, not re-done):

```sh
git checkout -b cursor/first-cycle-staging-91c1 9c978ded05b05ad174ce003a3efad22682ec187d
git merge --no-ff 1e3ee3fd604fc540600f393e215a712b283686d4
```

## Merge result

No conflict markers. Git auto-merged `docs/BRANCH_REGISTRY.md` (union of `#285`
staging/FC-4 rows with `#286`'s exam status). All `#286` P1 code paths landed
without touching FC-4 `frontend/scripts/check-npm-audit.js` or `.github/workflows/ci.yml`.

Alembic head remains `0089_activation_branch_protect`. No new migration in this composition.

## Preserved

- FC-4 `#282` A6/A7 npm-audit policy
- 12 required CI checks including `Frontend — npm audit`
- Path A task authority
- Exact-SHA review receipts and STOP-before-merge
- `#286` P1s: goal-scoped terminal STOP, cause-based paid-replan suppression,
  credential-safe `#279` harness (CRED-8)

## Excluded

- `#280` RETURN_TO_BUILDER (P1). Dual 0089 fork.
- Track B reviewer runtime.
- Merge, deploy, Recall, GitHub settings, paid providers.

## Remaining Codex work

Visible FAIL/BLOCKED in the `#279` harness: CRED-1/2/3, E2E-3, GRANT-PROD, TA-12/13, PUB-2
production publication, and legacy `evaluate_check_runs` skipped-as-pass. Do not implement
those here.
