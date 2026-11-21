# V8 — secure delivery and staging integration pipeline

**Status:** implemented and under exact-SHA verification. `first_cycle_go=false`.
**Branch:** `freebuff/v8-integration-pipeline-8917f0c`, based on V7
`8917f0ca4f5a64e57a8eda623d57763145bd9cde`.
**Scope:** independent delivery/verification infrastructure. Claude retains ownership of the
actual R15 reconciliation; this pipeline never resolves a security-critical semantic conflict.

## What was built

| Component | Command | Purpose |
| --- | --- | --- |
| Secure bundle import | `scripts/integration/bundle_import.py` | SHA-256 manifest, bundle integrity, ancestry, duplicate/already-integrated detection, provenance, secret/path scan, namespaced import ref, optional detached worktree |
| Conflict analysis | `scripts/integration/conflict_analysis.py` | Already-included changes, candidate-only functionality, overlapping files, actual `git merge-tree` conflicts, semantic-risk flags, migration graph collisions, security-sensitive review |
| Immutable manifest | `scripts/integration/release_manifest.py` | Integration base, imported SHAs, dependency order, actual changed files, verification status, required checks, blockers, artifact hashes, self-hash; refuses overwrite and untraceable provenance |
| Staging build | `scripts/integration/staging_build.py` | Exact-SHA detached worktree, lockfile validation, static Alembic ancestry, backend compile, existing R15/RLS/egress suites, authorization/egress check, frontend build when dependencies exist, PASS/FAIL/BLOCKED evidence |
| Rollback verification | `scripts/integration/rollback_verify.py` | Abandons a staging worktree/ref and proves the source commit, refs, working tree, repository integrity and test-DB Alembic revision are unchanged; reuses PR #461 backup/restore |

## Safety invariants

- Validation never executes a script from an untrusted bundle. Import reads Git objects and
  the tree only; staging test execution is a separate, explicit allowlisted step.
- The only new ref is `refs/imports/lifeai/<commit>`. No branch is moved, no force-push is
  performed and no agent branch is altered.
- The optional worktree is detached at the exact SHA and is removed with `git worktree
  remove --force`; the original commit remains addressable.
- A symlink, submodule, unsafe path, credential file or high-confidence production secret
  fails the import. Test/documentation placeholders are reported as review findings, not
  silently treated as real credentials; private-key headers require actual key material.
- The staging database must be loopback and named `test`, `staging` or `pytest`. The command
  never runs `alembic upgrade` against it; pytest's disposable fixture may create/drop its
  own database.
- Conflict analysis is read-only. It reports `manual_review_required` and never writes a
  merged tree or resolves a security-sensitive file.

## Exact real-fixture results

| Fixture | Result |
| --- | --- |
| V4 bundle `2265a0e` | import **PASS**; 0 hard findings, 10 review-only placeholders |
| V6 bundle `5619020` | import **PASS**; 0 hard findings, 10 review-only placeholders |
| V7 bundle `8917f0c` | import **PASS**; manifest, ancestry, provenance, security and target checks PASS |
| V6 + V7 in one manifest | **rejected**: V6 is already included in V7 |
| V7 vs `origin/integration/founder-alpha-20261121` | **FAIL**: 9 textual conflicts; migration heads `0079` and `0092` diverge |
| V7 staging build | **FAIL** initially: R15 egress test `test_registry_planning_fallback_forwards_owner` used a fake DB without the constructor's active-provider query; fixed in V8 |
| V8 staging build | rerun at the V8 exact SHA after the fix; frontend remains **BLOCKED** when `node_modules` is absent |
| V8 rollback verification | **PASS** against `lifeos_v6_test`; DB revision unchanged, refs/worktree clean, `git fsck` clean |

The V7 conflict set is intentionally unresolved. The known semantic areas are the prompt-fencing
/chat path, the integration Founder Console versus older overview, provider bounded-output
logic, and RLS/egress-sensitive files. Claude's R15 reconciliation remains the owner.

## Reproduction

See `scripts/integration/README.md` for the complete command sequence. The delivery artifacts
are under `delivery/v8-integration/`: import/conflict evidence, staging/rollback evidence, the
immutable staging manifest, README, checksums and the complete-history Git bundle.

## Remaining blockers

- Docker daemon and Playwright system libraries are unavailable in this environment.
- No local Ollama; F23/F24 are not claimed PASS.
- `frontend/node_modules` is absent, so the staging build reports frontend compatibility as
  BLOCKED rather than inventing a PASS.
- GitHub write access is absent; no push or PR is attempted. The verified bundle is the
  transfer path.
- The V7 delivery is divergent from the integration branch and has real conflicts; it must not
  be merged or auto-reconciled by this pipeline.
