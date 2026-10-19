# First-cycle staging composition

Machine-readable source of truth: `docs/first_cycle_staging_manifest.json`.
Codex follow-up: `docs/FIRST_CYCLE_CODEX_INTEGRATION.md`.

## Reproduce (no local machine state)

```sh
git fetch origin \
  9c978ded05b05ad174ce003a3efad22682ec187d \
  1e3ee3fd604fc540600f393e215a712b283686d4
git checkout -b cursor/first-cycle-staging-91c1 9c978ded05b05ad174ce003a3efad22682ec187d
git merge --no-ff 1e3ee3fd604fc540600f393e215a712b283686d4
```

Verify ancestry:

```sh
git merge-base --is-ancestor e33f494c4fc53704c494cecea788f3e22be36330 HEAD  # #263
git merge-base --is-ancestor 09b87e9b70dc02889d39c2bae3d56458509d49cb HEAD  # #276
git merge-base --is-ancestor abc88b4f479489c8c85111d754756058ab77eb22 HEAD  # #281
git merge-base --is-ancestor 9c978ded05b05ad174ce003a3efad22682ec187d HEAD  # #284
git merge-base --is-ancestor 1e3ee3fd604fc540600f393e215a712b283686d4 HEAD  # #282
```

`#280` @ `37bca08a287f00b6282222047340bffa949ff788` must **not** be an ancestor.

## Merge result

No conflict markers. Git auto-merged `.github/workflows/ci.yml` (activation triggers +
FC-4 npm-audit job) and `docs/BRANCH_REGISTRY.md` (union of rows). Frontend lockfile and
`check-npm-audit.js` came entirely from `#282`.

Alembic head remains `0089_activation_branch_protect`. No new migration in this composition.

## Excluded

- `#280` RETURN_TO_BUILDER (P1). Dual 0089 fork.
- Track B reviewer runtime.
- Merge, deploy, Recall, GitHub settings, paid providers.

## Remaining Codex work

Visible FAIL/BLOCKED in the `#279` harness: CRED-1/2/3, E2E-3, GRANT-PROD, TA-12/13, PUB-2
production publication, and legacy `evaluate_check_runs` skipped-as-pass. Do not implement
those here.
