# Integration-fragmentation reduction plan — the smallest path to one coherent MainAI workflow

Founder directive, 2026-11-15 ("CONTINUE MAINAI INTEGRATION"), Priority 3: *"PR #434 now
provides an integration-readiness assessment. Use it to identify the smallest set of changes
needed to establish one coherent MainAI development workflow. Focus on: canonical execution
authority, approved work-candidate promotion, source-backed project understanding, reliable
CI and security evidence, dependency-safe PR integration. Avoid new overlapping systems.
Prepare independent-review handoffs for mature PRs. Do not independently merge them."*

This builds directly on `docs/LIFE_INTEGRATION_READINESS_ASSESSMENT.md` (#434) — that
document's dependency chains and CI table are not re-derived here, only extended with what
has shipped since (#436, #437) and re-read against the five focus areas below. One of those
five areas was checked for fragmentation by reading the actual code and found to have none —
reported as a verified non-issue, not skipped.

## 1. The five focus areas, each checked against the real code

**Canonical execution authority** — already single-path, already converging. `require_task_
approval()` (`app.mainai_execution.approval`) remains the one real gate; #428 made `app.
workforce` call it, #431 made that call atomic, #433 extended the same atomic pattern to `app.
agent_coordination.dispatch_assignment()` (the one path with a live external call today), and
#436 documented precisely what that does and does not cover for the rest of a dispatched
process's lifetime. Nothing in this chain invents a second authority. The chain is linear
(#423→428→431→433→436) — merging it in order is the only "change" left; no new code is needed
for this area.

**Approved work-candidate promotion** — checked for fragmentation now, by reading the code,
not assumed coherent: grepped every `WorkCandidate(...)` construction site in `app/` and found
exactly ONE — `app.work_candidates.service`'s `record_work_candidate()`. Three different
upstream signal producers call it (`app.project_entities.service._record_work_candidate_if_
actionable()`, triggered only by `promote_interpretation_proposal()`; `app.memory_work_
linkage.service`'s Stage-C founder-memory linkage; `app.mainai_executive.lookaround`'s bounded
executive scan) — each a genuinely different KIND of signal, every one explicitly tagging its
own output as unauthorized (`"authorized": False"`/equivalent), none bypassing the single
write primitive. Promotion to real, executable work has exactly one gate too:
`authorize_work_candidate()`, reached through exactly one router endpoint
(`POST .../work-candidates/{id}/authorize`, `require_founder`-gated). **Verified result: no
fragmentation exists here. No change is needed for this area** — naming it as a real risk
would have been speculation; reading the code found none.

**Source-backed project understanding** — the knowledge chain (#430→432) plus its own two
follow-ons (#435's ingestion design, #437's source-identity foundation). Linear, no
overlapping systems: #437 adds exactly one new `MemorySourceUnit` arm, reusing the
`SIGNAL PRODUCER != TRUTH WRITER` governance `app.project_entities` already enforces — it does
not create a second knowledge store. What's NOT yet done, by design (#437's own stated scope):
wiring Pass 3's extraction output to actually call `get_or_create_repository_file_source_unit()`
for real files, and from there, real `KnowledgeClaim`/`InterpretationProposal` rows. That
wiring is real future work, not a fragmentation risk — it simply hasn't been built yet,
correctly deferred rather than rushed.

**Reliable CI and security evidence** — unchanged from #434's own finding: #323 is the
complete, already-CI-verified fix for the repo-wide `Frontend — npm audit`/E2E drift every PR
in this whole effort inherits; #394 is a subset duplicate to close once #323 merges. Every PR
listed in §2's table below is "backend-green, aggregate-red only from that known drift" —
restated here, not re-derived, per the founder's own instruction not to repeat completed
analysis.

**Dependency-safe PR integration** — §2 below is #434's own recommended order, extended to
include the two chains that didn't exist yet when #434 was written.

## 2. Full current picture (verified against GitHub at time of writing, not memorized)

```
Pre-existing, unrelated to every chain below:
  #323 (frontend-npm-audit-baseline-consolidation) -- not draft, backend+aggregate green,
    ready to merge first; #394 subsumed by it, close once #323 merges.

Chain A -- execution authority (linear, merge in this exact order):
  #419 -> #423 -> #428 -> #431 -> #433 -> #436

Chain B -- project knowledge (linear, merge in this exact order):
  #430 -> #432 -> #435 -> #437

Standalone, no dependency on anything above or on each other:
  #424, #429, #434 (this document's own predecessor), and this document's own PR.
```

Twelve PRs' worth of real engineering (#423, #424, #428, #429, #430, #431, #432, #433, #434,
#435, #436, #437) sit on top of a shared base whose own known CI drift (#323/#394) has an
already-verified fix waiting, unmerged, since before most of this chain even started.

## 3. The smallest set of changes to reach one coherent workflow

**There is no further engineering required to reach "one coherent workflow" as the five focus
areas above define it.** Every piece already named by the founder's five focus areas already
exists, in reviewable form, on an isolated branch, or (work-candidate promotion) was verified
to need nothing. The smallest remaining path is entirely a REVIEW-AND-MERGE sequence, not new
code:

1. Independent review and merge of **#323** (closes the CI-drift focus area for every
   subsequent merge in this list — the aggregate gate turns genuinely green for the first time
   since this effort began). Close #394 as subsumed, once #323 is in.
2. Independent review and merge of **Chain A**, strictly in order (#419→423→428→431→433→436) —
   closes the canonical-execution-authority focus area.
3. Independent review and merge of **Chain B**, strictly in order (#430→432→435→437) — closes
   the source-backed-understanding focus area as far as the foundation goes (the real
   extraction-to-source wiring remains explicitly future work, not blocking "coherent").
4. #424/#429 merge independently, in any order, whenever convenient — they block nothing and
   are blocked by nothing.

**Per `CLAUDE.md`'s own Merge-regel: none of steps 2-4's branches should be rebased onto a
post-#323 base before #323 has actually merged.** Recommending this order is not acting on
it — no rebase, merge, or push to any of these branches was performed while writing this plan.

## 4. Independent-review handoff — which PRs are mature now

**Mature, fully backend-green, ready for independent review now, no known blockers:** #423,
#424, #428, #429, #430, #431, #432, #433, #435, #436, #437 (verified exact-SHA backend CI
status for each already recorded across #434's own table and this session's own exact-SHA
checkpoints — not re-verified here to avoid the repetitive-status-message pattern the founder
explicitly asked to stop).

**Foundations awaiting a follow-on, not blockers to merging as-is:** #437 (repository-source
identity) is a complete, independently-useful foundation on its own merits — it does not need
Pass 3's extraction wiring to be correct or mergeable, that wiring is simply a separate, later
PR.

**Pre-existing, highest leverage to review first:** #323 — merging it is what turns every
other PR's aggregate CI gate from misleadingly red to actually green.

## 5. What this document explicitly does not do

- Does not merge, rebase, or push to any PR named above.
- Does not re-derive #434's own dependency-chain/CI-table analysis — extends it with what
  shipped since and the one new fragmentation check (work-candidate promotion) the founder's
  five focus areas asked for that #434 did not yet cover.
- Does not invent a fragmentation finding where the evidence (grepped, not assumed) showed
  none — "approved work-candidate promotion" is reported coherent because it is.
- Does not propose or implement the Pass-3-to-source-identity wiring — named as real future
  work, not built here.

No merge, deploy, activation, or security bypass. `first_cycle_go=false`.
