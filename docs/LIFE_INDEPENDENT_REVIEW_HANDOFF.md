# Independent Review Handoff — verified CI snapshot + concrete reviewer packets

## Founder directive

"RESUME ACTIVE DEVELOPMENT WHEN CAPACITY IS AVAILABLE" (2026-11-15), Priority 1: *"Identify
completed PRs that can proceed through independent review... Prepare concrete reviewer
handoffs... Do not self-certify your own security-sensitive changes."*

This document does not re-derive the dependency chains or the "no fragmentation" findings —
`docs/LIFE_MAINAI_INTEGRATION_FRAGMENTATION_REDUCTION_PLAN.md` (PR #438) already did that work
and its conclusions stand. What this document adds, and what #438 could not yet have (it was
written before these PRs opened or before CI finished): a **verified-right-now** CI snapshot for
every PR in all three named chains, pulled directly from GitHub's check-run API at the time of
writing — not restated from each PR's own body text, which can go stale — plus a concrete,
per-chain packet telling a reviewer exactly what to look at first.

**This is preparation for review, not a review.** No finding here constitutes security
clearance, and nothing here authorizes a merge. `first_cycle_go=false`.

## Verified CI snapshot (pulled live via `get_check_runs`, not assumed)

Every PR below shows the exact same shape: every real backend signal (`Backend — Alembic
migration check`, `Backend — unit/integration tests`, `Backend — RLS & session-security
tests`, `Backend — account lifecycle & rate-limit tests`) is **green**, and the only red checks
are the pre-existing, already-tracked baseline drift (`Frontend — npm audit`, `E2E — Playwright
(full stack)`, `E2E — same-origin proxy`) — the exact defect PR #323 fixes. This was re-verified
PR by PR, not inferred from one PR's result.

| PR | Head SHA (verified) | Backend signal | Known-drift-only? |
|---|---|---|---|
| **#323** (npm-audit/E2E fix itself) | `04b27e9585` | green | **N/A — fully green**, including npm audit + both E2E jobs. This is the PR that removes the drift for every PR below. |
| #423 | `a50e9ad184` | green | yes |
| #428 | `404ec7710d` | green | yes |
| #431 | `b143ec1676` | green | yes |
| #433 | `c12373cbcd` (corrected head) | green | yes |
| #436 | `20892001fa` | green | yes |
| #430 | `1b9b9219cb` | green | yes |
| #432 | `4c3f30b92e` | green | yes |
| #435 | `a49c8c9b0f` | green | yes |
| #437 | `a9949740787f` | green | yes |
| #424 | `fa55242a70` | green | yes |
| #429 | `5b60d81eeb` | green | yes |
| #438 | `ccac5ffadd` | green | yes |
| #439 | `311d001037` | green | yes |
| #440 (0069 composition, this round) | `35735620*` on dev / `62c66715` on branch | CI in progress at time of writing | not yet known — verify before including in a review batch |

\* `docs/BRANCH_REGISTRY.md`'s own update landed on dev directly as a side effect of this PR's
registry commit; see #440's own PR body for its actual code head.

## Recommended review order (reaffirms #438's own conclusion, not a new decision)

1. **#323 first.** It is the only PR in this set that is fully green on its own, and merging it
   is what turns every other PR's aggregate gate from misleadingly red to actually green. No
   other PR in this batch depends on #323 functionally — the dependency is purely about CI
   signal clarity for reviewers, not code.
2. **Chain A — canonical execution authority**, strict order: #423 → #428 → #431 → #433 → #436.
   Each is stacked on the previous via `git merge --no-ff`; reviewing out of order means
   re-reviewing earlier PRs' diffs embedded in the later ones.
3. **Chain B — source-backed project understanding**, strict order: #430 → #432 → #435 → #437,
   **then #440** (new this round — composes #437 together with #408/#403/#411, the three other
   independent "0069" claimants; not part of #438's original chain-B list since it postdates
   that document).
4. **#424 / #429 / #438 / #439** — independent of the above two chains and of each other; any
   order, any time.

Per `CLAUDE.md`'s Merge-regel: recommending this order is not acting on it — no branch has been
rebased or updated in anticipation of another merging.

## Concrete reviewer packets

### Packet 1 — #323 (frontend CI baseline)

- **What changed:** `next`/`eslint-config-next` 16.2.11→16.4.3 (controlled minor, not a major
  bump), four dependency-override pins (`sharp`, `js-yaml`, `browserslist`, `zod`,
  `source-map-js`), `brace-expansion` patched via in-range `npm audit fix` (no `--force`), and a
  second, independently-reviewable commit pinning the three Playwright-installing CI jobs to
  `ubuntu-24.04` (the floating `ubuntu-latest` resolved to Ubuntu 26.04, which has no Playwright
  1.56.1-compatible Chromium build — a runner/tooling defect, not an application one).
- **What to check:** that the dependency bump is genuinely minor-only (not a disguised major),
  that no audit allowlist/suppression was added, that `npm audit` after the change reports 0
  high/critical (confirmed in CI above), and that the `ubuntu-24.04` pin is scoped to only the
  three Playwright-installing jobs, not applied as a blanket change.
- **Risk level:** low — dependency versions and CI runner pins, no application code touched.

### Packet 2 — Chain A (execution authority: #423 → #428 → #431 → #433 → #436)

- **What changed, end to end:** #423 found (with two new executable regression tests, not
  assertion) that `app.workforce`'s real-dispatch path never checked its linked `MainAITask`'s
  approval state, unlike `app.agent_coordination`'s equivalent path. #428 closed that gap by
  calling the exact same `require_task_approval()` gate `app.agent_coordination` already uses —
  no second approval engine. #431 closed a TOCTOU race in that same gate (sequential check vs.
  atomic row-lock), reusing `_lock_task()`'s existing `SELECT ... FOR UPDATE` primitive, proven
  with four genuine two-connection concurrency tests. #433 extended the identical atomic-lock
  pattern to `app.agent_coordination.dispatch_assignment()` itself, closing the mirror-image gap
  there. #436 is pure documentation: names three concrete, unimplemented post-dispatch needs
  (cancellation propagation, periodic re-authorization, lease/fencing), explicitly noting the
  whole subsystem it describes is currently unreachable from any live router/worker path.
- **Highest-risk files to review first:** `backend/app/mainai_execution/approval.py` (the one
  real authorization gate, touched by #433), `backend/app/workforce/provider_worker.py` (new
  `require_live_task_authorization_for_workforce_dispatch()`, #428/#431), `backend/app/
  agent_coordination/dispatch.py` (#433's atomic re-check). In every case, confirm:
  `ProviderActivationBlocked`'s unconditional staged-raise is untouched (it still blocks every
  real invocation regardless of any approval state), and no new authorization engine/dispatcher
  was introduced — only calls into the existing `require_task_approval()`.
- **Concurrency tests to actually read, not just see pass:** #431's
  `test_workforce_task_authorization_concurrency.py` and #433's
  `test_agent_dispatch_authorization_boundary.py` — both use two real, separate database
  connections with `SET LOCAL lock_timeout`, not a sequential simulation.
- **Risk level:** high — authorization/dispatch boundary. Independent review required before
  any further work builds on this chain.

### Packet 3 — Chain B (source-backed understanding: #430 → #432 → #435 → #437 → #440)

- **What changed, end to end:** #430/#432/#435 are a measured, three-pass *reconnaissance*
  tool chain (Pass 1 regex extraction → Pass 2 stable-ID/dedup/confidence enrichment → Pass 3
  incremental re-scan) over the repo's own docs corpus — every record hardcodes `promoted:
  false`; nothing is written to `knowledge_claims`/`interpretation_proposals`/`project_entities`/
  `work_candidates` anywhere in this chain. #437 resolves the one real architectural blocker
  #435 named (repository files have no `document_id`, so the existing document-upload source
  path can't represent them) by adding a fourth `MemorySourceUnit` subtype arm
  (`repository_file`), the same extension pattern `message_source_units` already proved safe
  once. #440 (this round) resolves the resulting four-way "0069" migration-revision collision
  between #437 and three *other*, unrelated candidates (#408, #403, #411) that happened to claim
  the same revision string from the same dev tip — a pure rename, not a content reconciliation
  (already proven disjoint by PR #417).
- **Highest-risk/highest-value files to review first:** `backend/alembic/versions/0072_
  repository_file_source_units.py` (on #440's branch; `0069_repository_file_source_units.py` on
  #437's own branch) — the new table, its `commit_sha`/`file_path` format-enforcing CHECK
  constraints, and the widened `trg_msu_check_subtype_exists()` trigger. Confirm: the
  exclusive-arc invariant (exactly one subtype row per `memory_source_units` row) still holds
  with a fourth arm, and `document_source_units`/`message_source_units` are untouched.
- **What is explicitly NOT yet built, so a reviewer doesn't assume it is:** the actual wiring
  from Pass 1-3's extraction output into `get_or_create_repository_file_source_unit()` and real
  `KnowledgeClaim` rows. #437 builds only the source-identity foundation; the ingestion step is
  named future work in its own body.
- **Risk level:** medium — new table/trigger/RLS surface, but zero write path into the
  governed knowledge-promotion chain exists yet, so there is no exploitable authority gap today.

### Packet 4 — #424 / #429 / #438 / #439 (documentation, independent of the above)

- **#424:** pure documentation (long-term product vision, explicitly deferred areas). No code,
  no execution path. Lowest-risk review in this entire set.
- **#429:** five new tests proving #379 and #401's merge conflict is exactly two independent
  kwarg additions with zero real semantic overlap; documents (does not perform) the three-step
  reconciliation path. Neither #379's nor #401's own branch is touched.
- **#438:** the integration-fragmentation analysis this document itself builds on. Its own
  "approved work-candidate promotion has zero fragmentation" finding was verified by grepping
  every `WorkCandidate(...)` construction site directly — worth a reviewer independently
  re-running that grep rather than trusting the count.
- **#439:** specifies a human-operated, approval-recorded first development cycle — explicitly
  not an autonomous-dispatch activation. Confirm the one framing decision it states (step 6,
  "execute in scope," is satisfied by a human session under a real approval record, not by
  activating `app.workforce`'s or `app.agent_coordination`'s dormant adapter-dispatch paths) is
  actually how a reviewer reads it too, since this is the one place a misreading could be
  mistaken for a request to activate automation.

## Final acceptance handoff (2026-11-16) — engineering status, who approves, and what acceptance looks like

Founder directive ("CONFIRMED. CONTINUE."), 2026-11-16: *"If engineering is complete and
independent security acceptance is the only remaining blocker, provide the exact acceptance
handoff and identify who must approve it."* This section answers that directly, building on
`docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` (PR #446) rather than re-deriving it.

### Is engineering complete?

**Yes, for the first supervised development cycle's own actual scope** (PR #439's scenario:
one human-operated session, one GitHub-evidence-derived task, explicit approval, execution
within that approved scope, evidence collected for review). PR #446's matrix classified all
11 founder-named security areas; every item is either **RESOLVED** or correctly scoped
**CONDITIONAL** on a capability (autonomous dispatch, document ingestion, STRATO terminal
execution) that is not part of this scope and has not been activated. Nothing engineering-side
remains open for THIS scope.

### Fresh exact-SHA snapshot (pulled live via `get_check_runs` at the time of this update, not
restated from the table above, which predates #440 finishing CI and predates #442-#446 existing
entirely)

| PR | Head SHA | Backend signal (Alembic / unit+integration / RLS / account-lifecycle) | Drift-only red? |
|---|---|---|---|
| #440 (0069 composition + the two test-construction fixes) | `73a9619451` | all four green | yes — only `Frontend — npm audit` + both E2E jobs |
| #442 (P3 integration verification evidence) | `d0c669ad58` | all four green | yes |
| #443 (first-cycle harness, two genuine test-construction bugs found and fixed this round) | `3f7caa6325` | all four green | yes |
| #444 (Life Workspaces gap analysis + acceptance-tests addendum) | `6c71866b3a` | all four green | yes |
| #445 (ecosystem vision reconciliation + founder-vision backlog, incl. multi-model workforce + STRATO terminal items) | `74e6542c7a` | all four green | yes |
| #446 (first-cycle readiness matrix) | `abb729edb2` | all four green | yes |

Every PR above shows the identical shape already established by the table at the top of this
document: real backend security signal green, only the pre-existing, already-tracked baseline
drift (PR #323) red. No new, undocumented CI failure exists anywhere in this chain as of this
snapshot.

### The one true remaining blocker, and who must close it

**Independent security acceptance has not actually happened.** Every document this session has
produced — this handoff, the reviewer packets above, `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md`,
`docs/LIFE_EXECUTION_AUTHORITY_CANONICAL_MAPPING.md`, every exact-SHA CI snapshot — is
**preparation for review**, produced by the same AI system whose own work is under review. None
of it is, or can be, the independent acceptance itself. Self-certification is explicitly what
this entire handoff exists to avoid.

**Who must approve:** the founder, as the sole human principal with authority over this
single-operator system — there is no other internal stakeholder today. The founder may
optionally designate a genuinely external, independent reviewer (a person, not another Claude/
Codex/Copilot session, which would just move the self-certification problem rather than solve
it) to perform the technical review on their behalf, but the ACCEPTANCE decision itself —
whether to permit `first_cycle_go=true` — is the founder's to make or delegate explicitly, not
something any AI session can confer on itself or on another AI session.

**What the acceptance artifact concretely looks like** (a proposal, since none has existed
until now — the founder may adopt, amend, or replace this):
1. The founder (or their designated reviewer) reads, at minimum: this handoff's fresh snapshot
   above, `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` in full, and Packet 2 above (the
   highest-risk authorization/dispatch chain).
2. The founder records an explicit, dated acceptance decision — e.g. a comment on this PR
   naming exactly what is being accepted (by PR number and head SHA, matching the snapshot
   above) — not a verbal or implied go-ahead elsewhere.
3. Only AFTER that recorded acceptance does flipping `first_cycle_go` become a founder-
   authorized action, implemented as its own small, isolated, separately-reviewable change
   (never bundled into a documentation PR, and never performed by this session without that
   explicit, already-recorded authorization preceding it).
4. Merging the documentation PRs in this chain (#440-#446) is independent of step 3 and does
   not itself grant or imply activation — they can be merged as ordinary documentation/fixes
   whenever the founder is ready, per the review order already recommended above, without that
   constituting acceptance.

### What this section does not do

- Does not grant acceptance, perform it on the founder's behalf, or assume it.
- Does not flip `first_cycle_go`, and recommends against any future session doing so without a
  prior, explicitly recorded founder acceptance decision matching the shape above.
- Does not invent urgency — the founder's own prior directives have consistently kept
  `first_cycle_go=false` deliberately, and this section changes none of that.

## Reconciliation addendum (2026-11-16) — PR #425 and the import-security lineage

Founder directive ("FOUNDER RESPONSE: ACCEPTANCE HANDOFF RECEIVED"): the founder correctly
flagged that document import is active and required, and asked for #425 and its dependencies
to be reconciled against the acceptance scope above, with completed engineering separated from
unmerged changes, independently-unverified evidence, and known drift — and for any genuine
safety-critical issue to be named rather than assumed resolved because a PR exists.

**This addendum corrects the prior section above where it was wrong.** The snapshot in this
document's acceptance-handoff section, and `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md`'s
original row 10, both scoped "document ingestion" as a future, conditional capability using
PR #439's narrow GitHub-evidence scenario as the test. The founder's correction, independently
confirmed by PR #425's own body text (*"Founder Alpha requires `POST /api/library/import`; it
is active and has no startup disable boundary"*), shows that scoping was wrong: Library import
is an ordinary, already-operating founder-only feature, not gated behind MainAI's own
activation gate, so the underlying exposure is live today. `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md`
has been amended accordingly (commit `9e4067ec`) — this addendum is the detailed evidence
behind that amendment.

### A. Completed engineering (independently verified by this session, based on `dev`)

Unchanged from the snapshot above — restated here only to anchor the contrast with sections
B-D below, not re-verified again: #440/#442/#443/#444/#445/#446 (plus the earlier chain
#423/#428/#431/#433/#436/#430/#432/#435/#437/#424/#429/#438/#439/#323), all green on real
backend signal, all based on `dev`, all pulled live via `get_check_runs` by this session.

### B. Unmerged changes — a separate lineage, not based on `dev`, never previously reconciled

The full dependency chain, verified by walking each PR's actual `base`/`head` via
`list_pull_requests`, not assumed from titles:

```
codex/life-founder-alpha-integration (root, not itself a numbered PR)
  -> #391 (approved precomposition)
    -> #392 (parser isolation)          head 92d4ad3d76634473d1dc4a7a85913ff676090947
      -> #402 (import lease-loss fencing)  head 5b66814b35a4889c68acab9ed9e0ed3edd9dfbf5
        -> #413 (lease/write fencing, parser stderr bounds)  head 23022662a78abb141792ea7439b715d670cdc640
          -> #415 (workflow permission hardening, sibling)   head 8538f0f362f062f917debcdaab38935f13fc99cc
          -> #420 (test corrections)      head 31dd0cfe0a6127e21ea6bb9720f845da6cde155d
            -> #421 (exact-SHA validation workflow, evidence-only)  head f1de4ad417d1ac4c1252a36030c9d9ee0184cf3f
            -> #422 (parser procfs boundary reproducer, evidence-only)  head a4f6819fff04d6919d4b46b1c018ba38aad1ab88
              -> #425 (parser isolated-sidecar — the actual fix)  head 367ece55cf245250c061865f0c07f54e94913c34
```

None of these nine PRs are based on `dev` (`claude/det-kommer-mer-879lcm`); none are merged;
#425's own `mergeable_state` is `clean` only against its immediate base (#422's branch), not
against `dev`. Before #425 could ever reach `dev`, this entire chain would need its own review
and merge, in order, exactly as `docs/IMPORT_SECURITY_VALIDATION_MATRIX.md` (on #425's branch)
itself already documents — a thorough, pre-existing artifact this addendum cites rather than
duplicates.

### C. Independently-unverified evidence — real, substantial, but not confirmed by this repository's own CI

Every test count #425's chain reports (64 passed / 52 passed / 219 passed / 54 passed, etc.)
was run by the authoring session in disposable environments **outside this repository's GitHub
Actions**, not confirmed by it. Verified live, just now, not assumed from the matrix's own
claim:

- Run `43713479509` (#421, frozen-SHA import regression workflow): `status: completed,
  conclusion: action_required`, zero jobs executed.
- Run `43700873775` (#413, main CI workflow): `status: completed, conclusion: action_required`,
  zero jobs executed.

`docs/IMPORT_SECURITY_VALIDATION_MATRIX.md` itself names the cause and the remedy: *"One
repository administrator action is required: open Actions -> Import security regression tests
(frozen SHA) -> run 43713479509. If GitHub presents Approve and run, approve this restricted
run... The repository Actions-settings API returns 403 to this installation, so the exact
approval policy is not visible here."* **This is not something any AI session can do** — not
this one, not the one that wrote that line. It requires the founder, signed in to GitHub
directly, to approve the run or adjust the repository's Actions approval policy. Until that
happens, every test-count claim in this chain remains self-reported, not independently
confirmed — exactly the distinction the founder's own instruction (#5: "AI reviews are
supporting evidence, not final acceptance") already anticipates, now with a concrete, named
blocker rather than a general caution.

PR #425's own body is unusually explicit about this limitation in its own words, repeated
across multiple of its update sections: *"is not security acceptance,"* *"Local tests are not
production certification,"* *"Formal independent security acceptance remains pending,"*
*"External host-port reachability, sustained full-stack resource exhaustion, and deployment-
equivalent fault injection remain unverified."* None of this is this session's own finding —
it is the authoring session's own, already-documented honesty about its work's limits, cited
here rather than softened or restated as more final than it is.

### D. Known CI drift — unchanged, unrelated

`Frontend — npm audit` / both E2E jobs (PR #323) — present on PRs in both lineages, already
understood, not reconciled further here.

### The genuine safety-critical issue (per the founder's instruction not to assume resolved because a PR exists)

**Document parsing on `dev`, today, runs in-process with the backend's full secrets, for an
already-active, required, undisableable endpoint.** `backend/app/rag/extract.py` calls
`PdfReader`/`DocxDocument`/`BeautifulSoup` directly, in the same process as everything else —
no subprocess boundary, no sandboxing. The proposed fix (#425's dedicated, secretless,
namespace-isolated parser container) is real, substantially built, and extensively
self-tested — but is NOT merged, NOT based on `dev`, and NOT independently CI-verified in this
repository for the reason in section C above. One containment gap PR #425's OWN adversarial
testing already found (not this session's finding, cited from `docs/IMPORT_SECURITY_VALIDATION_MATRIX.md`):
an adversarial child process with code execution can read a same-UID parent's
`/proc/<pid>/environ` under the CURRENT (pre-sidecar) boundary — the same general class of
exposure as `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` row 5's `run_tests` finding, but on the
path that is actually live on `dev` right now, not a conditional one.

### The precise acceptance decision — options and residual risk, not a recommendation this session is making

**Option 1 — Accept the `dev`-based chain narrowly, explicitly excluding document import from
that acceptance.** Residual risk: the unsandboxed parsing exposure continues exactly as it is
today for as long as Library import keeps being used, independent of whatever is decided about
`first_cycle_go`. This option answers "is MainAI's own supervised-cycle execution authority
ready for review" without answering "is document import safe" — the founder would need to
treat those as two separate acceptances, not one.

**Option 2 — Treat document-import security as an equally required precondition before calling
any part of LifeAI operational.** Concrete, sequenced path: (a) the founder personally approves
the blocked Actions runs (or adjusts the repository's workflow-approval policy) so the #425
chain's claims become independently verifiable the way #440-#446's already are; (b) the
nine-PR chain in section B is reviewed and merged in its documented order, reconciled with
`dev`'s own extensive, independent evolution since that chain was opened; (c) the resulting
`dev`-based code is independently reviewed the same way the rest of this handoff asks for.
Residual risk during this path: longer timeline, and `dev` has moved substantially since this
chain branched (new tables, new RLS policies, new migrations this session added) — reconciling
the two is real integration work, not a fast-forward merge.

**Option 3 — A narrower interim mitigation, faster than Option 2, not yet attempted by anyone.**
The specific exposure (full-environment inheritance for untrusted code, whether in the parser
path or `run_tests`) could potentially be narrowed with a smaller, `dev`-native change — an
environment allowlist for both subprocess call sites — without importing the full 3200-line,
38-file sidecar architecture. This has not been designed, scoped, or attempted by this session
or any other; naming it as a possibility is not a claim that it is sufficient or easy.

This session makes none of these choices. The founder's own words govern here: *"Do not make
that decision on my behalf."*

## What this addendum does not do

- Does not approve, merge, or request the blocked Actions runs be approved on the founder's
  behalf — that action belongs to the founder alone.
- Does not merge, rebase, or build on any PR in the #425 chain.
- Does not implement Option 3 or any other mitigation.
- Does not change `docs/IMPORT_SECURITY_VALIDATION_MATRIX.md` on #425's own branch — that
  document is cited, not duplicated or edited.

## What this document does not do

- Does not perform independent review itself — every finding above is restated from each
  source PR's own executable evidence, not independently re-verified security analysis.
- Does not merge, rebase, or renumber any branch.
- Does not authorize `first_cycle_go=true` or any production activation.

---
_Generated by [Claude Code](https://claude.ai/code/session_016d4iixQnxxqNhDrxHHxV4Q)_
