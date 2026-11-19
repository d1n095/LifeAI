# Z.ai Round 10 — integration status (blocked on the artifact)

Recorded 2026-11-19 on `freebuff/founder-alpha-ops-recovery-20261118`.

## What was checked

Z.ai reported Round 10 at commit `9b0cb7ac` (eight cognitive milestones, 1,550 batched tests plus
200 cognitive tests on its final HEAD). None of that is treated as present until the exact commit
can be read.

| Probe | Result |
|---|---|
| `git cat-file -t 9b0cb7ac` in the working clone | `fatal: Not a valid object name 9b0cb7ac` |
| `git for-each-ref \| grep -iE 'zai\|cognitive\|round'` | no matching ref |
| `git branch -a` / `git tag` | no Z.ai or Round 10 ref; no tags at all |
| `find /tmp -maxdepth 2 -name '*.bundle'` | no bundle |
| bundle / patch-series / scratch-ref supplied out of band | none present |

The earlier probe of `8be704ea` (and `a1a0e073…`) failed the same way (`No commit found for SHA`,
`upload-pack: not our ref`), so this is the same blocker, not a new transient failure. No further
SHA probes were made: without a bundle, a patch series or a pushed ref there is nothing new to
learn from probing.

## Exact missing artifact

Any one of these makes the delivery verifiable:

1. a real bundle: `git bundle create zai-round10.bundle <range>` plus `shasum -a 256`, with
   `9b0cb7ac` inside it;
2. a `git format-patch` series whose cover letter names the produced SHAs;
3. a pushed ref in `d1n095/LifeAI` (a scratch ref such as `refs/zai/round10` is sufficient).

A branch with similar content is not a substitute: the commit id must match what Z.ai named.

## What was deliberately not done

No reconstruction of Z.ai's cognitive implementation from prose, and no replacement
implementation. The mission explicitly forbids duplicating it, and an invented substitute would be
indistinguishable from the real thing in a test report while being unverified.

## Compatibility plan (prepared, not executed)

Once a verifiable artifact exists:

1. **Identity first.** Verify the bundle checksum, `git bundle verify`, and that `9b0cb7ac` is an
   ancestor of (or equal to) the delivered head. Refuse any substitution.
2. **Isolation.** Create `freebuff/zai-round10-integration-<date>` from the current branch head.
   Never merge into `main`, never push over Z.ai's branch, never touch frozen PRs #459/#460.
3. **Migration DAG.** AST-parse every added `backend/alembic/versions/*.py`, read `revision` and
   `down_revision`, and merge with the current single-root/single-head graph (`0079`). Renumber only
   on a genuine collision, never from an outdated numbering plan.
4. **Schema and API conflicts.** Diff each touched table/router against the current implementation.
   Report an overlap as a conflict; do not silently discard either side to make Git clean. The
   billing/`accounting.py` area must be compared path by path — the frozen tree contains no
   `accounting.py`, so it is either net-new or a rename, never inferred from a column name.
5. **Dependencies and security.** Review new packages, network calls, subprocess use, credential
   access and any provider activation. The current default remains: provider activation is disabled
   and `first_cycle_go=false`.
6. **Combined acceptance.** Real PostgreSQL, Redis, local models, the founder workflow harness and
   the Chromium spec, plus Z.ai's own batched/cognitive suites on the merged SHA. A green unit run
   alone is not acceptance.
7. **Review.** Independent review before any merge decision; the founder retains final
   authorization.

## Current status

Integration not started; blocked on the artifact above. Independent operational work (startup
bootstrap, founder workflow, 30-document retrieval corpus, encrypted backup tooling) is unaffected
and recorded in `docs/FOUNDER_ALPHA_READINESS.md`.
