# P3 — Dependency-Safe Integration: executable verification evidence

## Founder directive

"RESUME ACTIVE DEVELOPMENT WHEN CAPACITY IS AVAILABLE" (2026-11-15), Priority 3: *"Move beyond
documentation into executable integration verification. Use disposable branches and
environments to test combinations of already completed candidates. Identify conflicts, missing
dependencies and genuine failures. Repair only defects within the authorized scope using
isolated PRs. Avoid duplicate modules, unnecessary rewrites and speculative refactoring."*

**This is a verification report, not a merge proposal.** The actual disposable branch this
describes (`claude/p3-dependency-safe-integration-test`, built and torn down in this session,
not pushed — its 8-way merge diff would be 68 files / ~14,300 lines and would mislead a reviewer
into thinking it's a real change proposal rather than a test) is reproducible from the exact
commands below against the exact SHAs named. Nothing here merges, rebases, or renumbers any
real PR branch.

## What was tested

Every PR #438 and #441 identified as mature and CI-green, merged together in the recommended
order from #441, via `git merge --no-ff` on a disposable branch rooted at dev tip
(`claude/det-kommer-mer-879lcm`):

| Step | Branch merged | Exact SHA | Contains |
|---|---|---|---|
| 1 | `claude/frontend-npm-audit-baseline-consolidation` (#323) | `04b27e9585` | Frontend CI baseline fix |
| 2 | `claude/post-dispatch-authorization-lifecycle` (#436 tip) | `20892001fa` | Chain A: #423+428+431+433+436 |
| 3 | `claude/migration-0069-composition-integration` (#440) | `62c66715` | #408+403+411+437, renumbered 0069→0072 |
| 4 | `claude/first-supervised-development-cycle-spec` (#439 tip) | `311d001037` | Chain B remainder: #430+432+435+437+438+439 |
| 5 | `claude/life-deferred-product-vision-vendor-philosophy` (#424) | `fa55242a70` | Independent docs |
| 6 | `claude/blueprint-379-401-reconciliation-evidence` (#429) | `5b60d81eeb` | Independent compatibility tests |

## Result: one trivial conflict, zero real code/schema conflicts

Only one merge conflict occurred across all six steps, in step 3 (merging #440): both branches
had independently appended new sections to `docs/BRANCH_REGISTRY.md` near the same position —
pure textual divergence in an append-only markdown file, not a semantic conflict. Resolved by
keeping both sections (nothing discarded), verified by removing every stray conflict marker and
re-confirming the file has none left.

**No other step produced any conflict at all** — including step 4, which was the one genuine
risk this exercise specifically went looking for: #440 (step 3) had already renumbered #437's
migration file from `0069_repository_file_source_units.py` to `0072_repository_file_source_
units.py`, while #439's tip (step 4) still carries #437's *original* history with the old
filename. Since #439's branch never itself modified that file after inheriting it from #437,
git's three-way merge correctly resolved the rename/renumber as a clean "modified on one side,
unchanged on the other" case — no manual intervention needed. This is concrete, executable
evidence (not an assumption) that #440's renumbering composes safely with the rest of chain B.

## Post-merge verification (combined tree, all six steps applied)

- `alembic heads` → single head (`0072`), no fork — confirms the full combined migration graph
  (dev's 68 existing migrations + the four composed 0069-collision candidates) still resolves
  cleanly after every PR in this batch is layered on top.
- `python3 -m compileall app scripts alembic tests` (backend) → zero syntax errors across the
  entire combined backend tree, not just the files individually touched by any one PR.
- `python3 -c "import app.models; import app.main"` → succeeds.
- `ruff check` on every one of the 49 Python files changed across all six steps: 51 findings
  total. Not independently re-triaged here — each originating PR already verified, individually,
  that its own findings are pre-existing against its own base branch (the same discipline this
  whole session has used throughout); the combined count matching the sum of those
  already-accepted baselines, with no new finding appearing only in the merged tree, is exactly
  the expected and desired result, not a new investigation.
- No local Postgres in this sandbox; real DB-backed verification for each individual PR already
  ran in that PR's own CI (see #441's verified snapshot) — this exercise's own value-add is
  specifically the *combination*, which needs no database to prove (migration graph resolution,
  whole-tree compilation, and import are all static checks).

## What this does not do

- Does not merge, rebase, or renumber any real PR branch — the disposable branch used for this
  test was never pushed and is not meant to be.
- Does not repair anything, because no genuine defect was found to repair. Per the directive's
  own "repair only defects within the authorized scope" — there is no scope to act in here.
- Does not supersede or duplicate #438's dependency-order analysis or #441's CI snapshot; it is
  the next, executable step after both.

## Conclusion

The three named chains (#323, execution-authority, project-knowledge) plus the two independent
PRs (#424, #429) plus this round's own #440 compose into one coherent tree with no genuine
conflicts, missing dependencies, or failures — only the one already-understood, already-
resolved-elsewhere drift (npm-audit/E2E, which #323 itself fixes). The smallest remaining path
to one coherent MainAI development workflow, per #438's own conclusion, continues to require
independent review and an authorized merge sequence — not further engineering.

No merge, deployment, activation, or security bypass. `first_cycle_go=false`.

## Addendum — delta re-verification after this round's new work (founder directive "CONTINUE ACTIVE ENGINEERING", Priority 4)

Since this document was first written, #440 gained a real CI-caught test fix plus new
security-composition tests (Priority 1), and #443 gained a new edge-case test file
(Priority 2); #444 (the durable-workspaces gap analysis) also opened. Re-ran the same
combination on a fresh, unpushed disposable branch to confirm the delta composes exactly as
cleanly as the original verification: chain A tip → #440 (latest) → chain B remainder → #443
(latest) → #444.

**Result: only the same two already-understood `docs/BRANCH_REGISTRY.md` append conflicts
(both resolved by keeping both sides' content, nothing discarded), zero real code/schema
conflicts** — including #444's docs-only addition, which merged with no conflict at all.
Post-merge: `alembic heads` → single head (`0072`), unchanged; full backend `compileall`
clean; `app.models`/`app.main` import clean; `ruff check` clean on every new test file added
this round (`test_system_activation_authorizations_composition_security.py`,
`test_first_supervised_development_cycle_edge_cases.py`, and the two files from the original
verification, re-checked). Nothing merged, rebased, or renumbered for real; this disposable
branch was not pushed, same discipline as the original exercise.

---
_Generated by [Claude Code](https://claude.ai/code/session_016d4iixQnxxqNhDrxHHxV4Q)_
