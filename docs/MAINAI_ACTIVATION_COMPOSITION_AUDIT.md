# MainAI Founder-supervised v1 — composition feasibility audit

Date: 2026-10-14  
Audit branch: `codex/mainai-activation-composition-audit`  
Immutable base: `2fbe20aacf1203fc0e16d216ef55b666b2181619`  
Decision: **NO-GO until the gates in section 10 are evidenced**

This is an evidence-only, read-only composition audit. It neither implements the
activation-security contract nor authorizes an activation, remote write, merge,
deployment, Recall activation, provider call, or agent launch. Builder reports and
clean Git merges are not treated as verification.

The machine-readable companion is
`docs/evidence/mainai_activation_composition_manifest.json`.

## 1. Exact integration ancestry

The immutable production base is Founder Alpha
`2fbe20aacf1203fc0e16d216ef55b666b2181619`.

### Path A

The verified Git ancestry is:

```
2fbe20a (Founder Alpha)
  -> 21ff3cc (#264)
  -> dba9390 (#266)
  -> effafbe (#268)
  -> 66c2204 (#269)
  -> d5153eb (#270; product commit f744607)
  -> 05df3db (#271; product commit 6ed6e49)
  -> bb2172e (#272; product commit a38c227)
```

Use only the independently certified final descendant of this chain. Do not merge
the ancestors separately. At audit time `bb2172e9e6fd1d83402ac1da1d40cc88866ca9da`
is the newest published candidate, but its exact-SHA examination is an unresolved
placeholder. The #271 examination closed SCOPE-1, CAP-1, FC-2b and ROUTE-4 for its
declared scope, while leaving the structured-target insertion follow-up described
in section 7.

### Track B

The verified ancestry is:

```
2fbe20a (Founder Alpha)
  -> 39a7b81 (#265)
  -> da4d96b (#267; product commit c4b2477)
```

Track B supplies MainAI-owned CLI session identity, isolated CLI homes, bounded
conversation turns, read-only reviewer routing and assignment execution. Its use
in the first activation remains subject to the compatibility gates below.

### GitHub read-only discovery

PR #263 head `e33f494c4fc53704c494cecea788f3e22be36330`
(product commit `5bec345a6095a1c1bf72ed12b0772de35813832a`) is a sibling
of Path A and Track B from the Founder Alpha base. It adds paginated read-only
GitHub discovery. It is required, but does not by itself prove rulesets or token
permissions and does not grant write authority.

### Specification evidence only

`8a5bdaf28f3a94c629a296ad9804f0a043165ca7` is the accepted
activation-security contract and 10/10 reference-oracle evidence. It is **not** a
production implementation and must not be merged into the activation candidate.

## 2. Local merge/conflict simulation

All simulations were local and unpushed.

Recommended provisional order:

1. Start at exact Founder Alpha `2fbe20a`.
2. Merge the independently frozen final Path A descendant (currently represented
   by #272, pending its verdict).
3. Merge the independently frozen final Track B descendant (currently #267 or a
   certified successor).
4. Merge #263 GitHub read-only discovery.
5. Merge the independently frozen task-authority correction, once published.
6. Implement/merge activation security against that exact composed parent.
7. Merge the independently frozen frontend audit remediation separately and rerun
   the complete final-SHA battery.

Simulation results:

- base -> #272: clean;
- #272 -> #267: clean, including shared `execution_job.py` and `planner.py`;
- #267 -> #263: two textual conflicts only:
  `backend/tests/backend/test_github_client.py` and
  `docs/BRANCH_REGISTRY.md`.

The test conflict must be resolved by retaining the union of #263 pagination and
read-only assertions and Path A draft-PR assertions. The registry conflict must be
resolved by retaining all current lane records. A conflict-free merge is not a
security verdict: the semantic conflicts below remain.

Expected parentage is a first-parent chain rooted at `2fbe20a`, with merge parents
for the certified Path A, Track B and #263 heads. Task-authority and
activation-security work should be built from the exact resulting composition SHA,
not rebased speculatively. The final SHA cannot be calculated until the active lane
placeholders are frozen.

## 3. Authority-model compatibility

Path A's existing execution envelope, task/job/worktree records, provider-spend
ledger and CI wait path can be extended to implement the accepted contract. A
second orchestration or authority framework is neither required nor allowed.

Minimal compatible extension:

- add durable approved repository/base branch/base SHA and immutable required-check
  set to the existing activation envelope;
- add one-shot, atomically consumed push grants bound to owner, goal, task,
  job/attempt, canonical worktree, repository, branch, base SHA and exact local
  commit SHA;
- represent `repo_edit`, `push` and `open_pr` as separate effects;
- add immutable preflight receipts bound to the envelope authority version,
  activation SHA, configuration digest and Founder/admin attestations;
- check the approved base SHA before paid planning, worktree creation, edit,
  push and PR publication;
- make activation mode fail closed and apply one common remote-effect predicate to
  every GitHub write call site.

#263's client should be extended rather than replaced for ruleset and token-
permission discovery. Unknown required facts produce NO-GO.

### Semantic GO blockers exposed by composition

1. **Worktree credential exposure / push bypass.**
   `backend/app/mainai_execution/worktree.py:create_task_worktree_sync()` writes an
   authenticated remote URL containing `GITHUB_TOKEN` to `.git/config`. Track B
   launches CLI agents inside that worktree. A read-only reviewer can therefore
   observe the credential, and any process with command/network ability can bypass
   the future one-shot push gate. The worktree remote must be credential-free;
   short-lived credentials must be injected only inside the controlled push
   adapter and never persisted in the checkout.

2. **Agent dispatch is not an envelope effect.**
   `planner.KNOWN_TASK_TYPES` and `execution_job.py` accept `agent_assignment`, but
   the Path A envelope has no distinct effect binding role, mode, provider,
   assignment, workspace, task/job, owner and budget. Add an explicit
   `agent_dispatch` transition. For v1, keep Track B builder execution disabled
   and permit a reviewer only after task-authority verification.

3. **Builder path scope is metadata, not filesystem enforcement.**
   Track B correctly forces reviewer/researcher roles read-only, but a builder CLI
   receives a writable worktree; assignment `allowed_paths` does not constrain the
   CLI filesystem sandbox. Before builder use, enforce scoped writes or reject and
   discard any out-of-scope diff before commit/push.

4. **PR existence is reported as merge.**
   `backend/app/agent_coordination/dispatch.py:apply_dispatch_result()` supplies
   `merge_result="merged"` when an execution succeeds and merely has `pr_ref`.
   PR != merge. It must remain unknown/unmerged absent authoritative GitHub merged-
   state evidence.

## 4. Track B compatibility

Preserved and suitable after the blockers above are closed:

- session identifiers are generated/re-derived by MainAI, not accepted from agent
  self-report;
- CLI HOME/config/session storage is isolated and sensitive parent variables are
  not forwarded;
- reviewer/researcher roles are forced through read-only adapter modes;
- builder/reviewer identity and final self-report evidence remain distinct;
- Track B contains no direct merge, deployment or Recall activation operation.

These properties do not create authority. Reviewer use still needs a Founder-bound
task/dispatch grant, and its checkout must contain no reusable GitHub credential.

## 5. Migration compatibility

The composed #263/#272/#267 candidates add no migrations and retain
`0088_erasure_completion_phase` as the head.

The minimum activation-security implementation genuinely needs one atomic migration
covering:

- envelope columns for approved base branch/SHA, required-check-set digest/content
  and authority version (reuse existing repository identity);
- `mainai_push_grants`, including exact bindings, expiry, consumption, cancellation
  and retry semantics;
- `mainai_activation_preflight_receipts`, including immutable configuration,
  authority and attestation digests;
- owner foreign keys, uniqueness/check constraints, indexes, RLS and least-privilege
  runtime grants.

If the activation candidate explicitly excludes the Continuous Conversation and
Founder Sovereignty lineages, reserve
`0089_mainai_activation_security` with down revision
`0088_erasure_completion_phase`. Those other lanes contain conflicting alternative
0090/0091/0092 families and must not be composed here. If another certified lineage
is intentionally selected first, renumber this migration linearly from its actual
single head. Never create parallel Alembic heads or a merge migration merely to
paper over the collision.

## 6. Minimum complete final-SHA battery

Run against the exact composed candidate and preserve commands, environment, counts
and immutable artifacts:

1. accepted activation-security 10/10 oracle probes as contract conformance plus
   production tests for grants, SHA binding, CI-set binding and preflight receipts;
2. all Path A activation, envelope-scope, first-cycle, P1 remediation, routing,
   recovery, worktree, provider-spend and CI-wait adversarial suites;
3. independent Path A examiner probes, not builder claims;
4. Track B session, adapter, assignment-hook, routing and coordination security
   suites, including credential non-disclosure and out-of-scope write negatives;
5. Claude's independently published task-authority probes (placeholder);
6. fresh-database migration upgrade/downgrade, single-head, RLS and runtime-
   privilege tests;
7. the full backend/product CI battery;
8. frontend npm audit (0 high/critical), TypeScript, ESLint, production build,
   frontend tests and canonical E2E on the separately certified remediation;
9. exact-SHA GitHub required checks, waiting for every immutable expected context;
10. independent exact-SHA examination after all composition resolutions.

An inherited CI failure may block publication but must not trigger paid replanning.
The frontend npm-audit evidence lane is not production remediation and is not release
approval; insert the independently frozen implementation SHA in the manifest.

## 7. Legacy isolation and bypass inventory

Before GO, prove all of these fail closed:

- global `GITHUB_WRITE_ENABLED` cannot authorize an unrelated/legacy goal;
- never-governed/standard-repository fallbacks cannot create remote effects;
- legacy `agent_orchestration.prepare_github_pr` remains disabled via
  `MAINAI_LEGACY_PREPARE_PR_WRITES_ENABLED=false`;
- `GITHUB_AUTO_MERGE_ENABLED=false` and no route can merge, deploy, release or
  activate Recall;
- generic GitHub client write methods are guarded at every call site by the common
  effect predicate;
- retry/recovery/takeover cannot reuse an expired, cancelled or consumed push grant;
- CI waits require the immutable set for the exact pushed SHA, not only checks
  currently visible;
- the #271 insertion-path follow-up cannot derive edit scope solely from prose when
  structured targets are absent;
- `agent_assignment` cannot launch without its own exact task authority;
- worktrees and reviewer sessions contain no GitHub credential;
- PR creation cannot be represented as merge evidence;
- Track B builder mode remains disabled until filesystem path enforcement exists;
- agent/provider spend has its own explicit Founder grant and cannot inherit
  planner/provider authority.

## 8. Explicit exclusion list

Do not merge:

- `8a5bdaf...` activation-security spec/evidence into production;
- individual superseded Path A ancestors when the certified descendant is used;
- examiner/evidence branches as production implementation;
- Continuous Conversation 0089/0090/0091/0092 candidates;
- Founder Sovereignty 0090/0091/0092 candidates;
- Grandmaster, Claim-Action Integrity or other authority frameworks as an implicit
  dependency;
- stale frontend audit examiner proposals in place of a certified remediation;
- any active Cursor or Claude candidate before its independent exact-SHA verdict.

## 9. Feature/configuration posture

The implementation may name the activation-mode flag, but its required semantics
are fixed: default off; fail closed; exactly one Founder-supervised goal; no legacy
write fallback. During an authorized run only, GitHub writes may be globally enabled
while still requiring per-effect grants. Auto-merge, legacy PR writes, deploy,
release, credentials administration and Recall remain disabled. Track B builder is
disabled; reviewer mode is read-only and separately authorized. Repository, base
branch, exact base SHA, model/provider and spend limits are immutable receipt inputs.

## 10. Gate before creating `mainai/integration-founder-supervised-v1`

Creation is GO only when all items are evidenced:

- independent FREEZE/exact-SHA verdicts for final Path A, Track B, task-authority,
  activation-security implementation and frontend audit remediation;
- final candidates share the declared Founder Alpha ancestry and no excluded branch;
- the four semantic composition blockers in section 3 are closed with adversarial
  tests;
- one linear Alembic head and the reserved migration strategy are verified on a
  fresh database;
- GitHub ruleset and fine-grained token facts are runtime-proved or immutable
  Founder/admin-attested as required; no bot bypass, force push or branch deletion;
- deterministic preflight returns GO for the exact activation SHA/config/authority
  version and records an immutable receipt; every unknown required fact returns
  NO-GO;
- separate unconsumed `repo_edit`, push and `open_pr` authorities exist; none implies
  another;
- credential-free worktree, clean canonical path, exact base and local commit SHA,
  no conflicting Supervisor lease and bounded spend are verified;
- full local battery and exact-SHA GitHub required-check set are green, followed by
  independent exact-SHA examination;
- a human Founder explicitly authorizes branch creation after reviewing the receipt.

Until every item is satisfied, the deterministic result is **NO-GO**.
