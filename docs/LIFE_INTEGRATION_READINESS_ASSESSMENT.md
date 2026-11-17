# Integration readiness assessment — the Priority 1/2/3 PR chain

Founder directive, 2026-11-15 ("CONTINUE ACTIVE ENGINEERING"), Priority 2: *"Review the
dependency chain for #323, #423, #428, #431, #429, #430/#432. Produce an executable
integration-readiness assessment. Identify which PRs can be independently reviewed and safely
integrated, which depend on preceding PRs, and which remain blocked. Do not confuse
backend-green results with complete repository-wide CI approval. Do not merge without
authorization."*

This document is an isolated draft PR, not a direct push to `docs/BRANCH_REGISTRY.md` — the
registry already carries the living per-PR detail this assessment is built from (verified
against it and against GitHub directly, not memorized); this is the requested standalone,
reviewer-facing synthesis across all of it. **No merge, rebase, or branch update was performed
while producing this document** — recommending an order is not acting on it (see "Merge-regeln"
in `CLAUDE.md`: a branch is not touched in advance of its dependency actually merging).

## 1. The two kinds of "green" — why this distinction matters for every row below

Every PR in this chain inherits the shared base's own pre-existing CI drift:
`Frontend — npm audit`, `E2E — Playwright (full stack)`, and `E2E — same-origin proxy` fail
identically on every PR regardless of diff content, which is why the aggregate
**"All required checks passed"** gate reads failure on every single row in the table below —
including rows with zero real backend issues. This is **not** a per-PR defect; it is a base-
branch problem with an already-identified, already-CI-verified fix sitting unmerged (§3).
**"Backend-green" below means the four real backend jobs** (`Backend — Alembic migration
check`, `Backend — unit/integration tests`, `Backend — RLS & session-security tests`,
`Backend — account lifecycle & rate-limit tests`) **all passed at the PR's current head.** It
does **not** mean the aggregate gate is green, and it does not mean independent review or
founder authorization has happened — those remain required regardless of CI color.

## 2. Dependency chains (verified against GitHub, not memorized)

Three independent chains plus two standalone PRs exist in the work this session produced:

```
Chain A (execution authority / authorization race):
  #419 (three-execution-systems-boundary-map)
    -> #423 (execution-authority-convergence-recommendation)
      -> #428 (workforce-task-approval-enforcement)      [merges #423 via --no-ff]
        -> #431 (workforce-authorization-dispatch-atomicity)  [merges #428 via --no-ff]
          -> #433 (agent-coordination-dispatch-authorization-boundary) [merges #431 via --no-ff]

Chain B (project knowledge inventory):
  #430 (project-knowledge-inventory-pass1)
    -> #432 (project-knowledge-inventory-pass2)  [merges #430 via --no-ff]

Standalone:
  #424 (life-deferred-product-vision-vendor-philosophy) -- based on dev tip directly, no
    dependency on anything above, nothing depends on it.
  #429 (blueprint-379-401-reconciliation-evidence) -- based on dev tip directly, no
    dependency on anything above, nothing depends on it.

Pre-existing, independent of all of the above (not created this session):
  #323 (frontend-npm-audit-baseline-consolidation) -- NOT a draft, NOT part of any chain above.
  #394 (ci-playwright-ubuntu2604-pin) -- draft, subsumed by half of #323 (see §3).
```

Every link above was created by `git merge --no-ff` of the dependency's branch into a brand
new branch, **never** by pushing to the dependency's own branch — so every PR's own diff on
GitHub shows only its own real additions, never the whole chain's.

## 3. #323/#394 — the actual, already-verified fix for the CI drift described in §1

Already investigated and recorded in `docs/BRANCH_REGISTRY.md` ("Uppdatering 2026-11-15 —
#394 är en delmängdsduplikat av #323"); restated here because it is the single highest-leverage
fact in this whole assessment and belongs in the reviewer-facing synthesis, not only in the
registry's own log. Re-verified directly against GitHub for this document (not re-derived from
scratch):

- **#323** (`claude/frontend-npm-audit-baseline-consolidation`) is **not a draft**, has
  `mergeable_state` reported as `unknown` by the API at query time (GitHub computes this
  lazily; its last real CI run, 2026-11-01, was fully green across npm-audit, both E2E jobs,
  and the aggregate gate). It is based on an **older** dev tip
  (`64c07d5b0c4821bfb175700283021aa6e95cb61c`) than today's
  (`010fb444...` as of this document). Nothing has touched `frontend/package.json` on dev
  since #323 branched, so the fix should still apply cleanly, but this has not been re-verified
  by actually attempting the merge (that is a reviewer/maintainer action, not taken here).
- **#394** (`claude/ci-playwright-ubuntu2604-pin`, draft) duplicates exactly the Playwright/
  ubuntu-24.04 half of #323's fix, on its own separate branch, also based on the same older
  dev tip. Its own CI confirms the ubuntu pin alone fixes both E2E jobs but, by design, leaves
  npm-audit (and the aggregate gate) red, since that is outside its scope.
- **Recommendation (restated from the registry, not a new decision):** #323 is the complete
  fix and is the PR that should go through independent review and merge first; #394 should be
  closed as subsumed by #323 once #323 merges — not before, and not by this session acting
  unilaterally.

**Why this belongs first in the merge order below:** every other PR in this assessment would
show its aggregate gate green, not just its backend jobs, the moment #323 merges and each
branch is updated against the new tip — at that point this entire "backend-green vs.
aggregate-green" distinction (§1) stops applying to new work. Nothing here merges on this
session's own authority; this is a recommendation for whoever reviews and merges #323.

## 4. Per-PR status table

| PR | Depends on | Backend CI (4 real jobs) | Aggregate gate | Independent-review readiness |
|---|---|---|---|---|
| #323 | none (pre-existing, older base) | green (last run 2026-11-01) | **green** — the one exception | Ready now; not part of this session's own chains |
| #394 | none (pre-existing, older base) | green | red (by design, npm-audit out of scope) | Superseded by #323 — recommend closing once #323 merges |
| #424 | none (standalone) | green | red (known drift only) | Ready now |
| #429 | none (standalone) | green (both runs) | red (known drift only) | Ready now |
| #423 | #419 | green | red (known drift only) | Ready now |
| #428 | #423 | green (confirmed at exact head `404ec7710d`) | red (known drift only) | Ready now |
| #431 | #428 | green (both runs, exact head `b143ec16`) | red (known drift only) | Ready now |
| #430 | none (standalone) | green (both runs) | red (known drift only) | Ready now |
| #432 | #430 | green (confirmed at exact head `4c3f30b9`) | red (known drift only) | Ready now |
| #433 | #431 | CI just started at time of writing — not yet confirmed | red (known drift, expected) | Pending its own first CI result; code + tests already reviewed internally |

Nine of ten rows show the identical "backend green, aggregate red because of #323/#394's
not-yet-merged fix" pattern described in §1 — this is the single biggest reason this
assessment exists: without reading past the aggregate gate, every one of these would
misleadingly read as blocked.

## 5. Recommended review/merge order — a recommendation only, not an action taken

1. **#323** independently reviewed and merged first (closes the CI drift at its actual root —
   the shared base — rather than each PR carrying its own copy of the same "known drift"
   caveat forever). **#394 closed as subsumed**, once #323 is in.
2. **#424 and #429** — standalone, no dependency on anything else in this list, can be
   reviewed and integrated in any order relative to each other or to the rest of this list.
3. **Chain A, in strict order**: #423, then #428, then #431, then #433 — each one's own diff
   depends on its predecessor's actual code (not just on dev tip), so reviewing out of order
   would mean reviewing a diff that doesn't yet include what it's built on. This does **not**
   mean waiting for #323 first; chains A and B are independent of the CI-drift fix and of each
   other.
4. **Chain B, in strict order**: #430, then #432.

**Per "Merge-regeln" in `CLAUDE.md`: none of these branches should be rebased or have their
base updated in advance of the PR immediately before them in its own chain actually merging.**
Concretely: #428 should not be rebased onto a post-#323 dev tip until #423 has actually merged
into dev; #431 should not be touched until #428 has merged; and so on down each chain. Doing
so pre-emptively would just move today's "known drift" caveat around without closing it any
sooner, and risks exactly the repeated-rebase/merge-conflict churn that rule exists to prevent.

## 6. What remains blocked, named so it is not mistaken for part of this list

The 0069 migration-revision five-way collision (#412/#413, #408, #403, #411) is **unrelated**
to every PR in §2 and §4 — no shared files, no shared dependency — and remains governed by the
founder's own stated order ("#412/#413 composition, then #408 activation authority, then #403
GitHub evidence, then #411 calendar"), with an explicit "do not merge or renumber in isolation"
instruction already recorded in `docs/BRANCH_REGISTRY.md`. Named here only so a reader of this
assessment does not assume its absence from §2 means it was overlooked.

## 7. What this document explicitly does not do

- Does not merge, rebase, or push to any branch named above.
- Does not substitute for independent security/architecture review on any PR — every row in
  §4 still requires that, regardless of CI color.
- Does not re-run or re-derive the #323/#394 analysis from scratch — §3 restates and
  re-verifies against GitHub what `docs/BRANCH_REGISTRY.md` already recorded, per the
  founder's own instruction not to repeat completed analysis.
- Does not claim repository-wide CI approval for any PR — see §1's explicit distinction,
  which this document's entire §4 table is built to preserve rather than collapse.
