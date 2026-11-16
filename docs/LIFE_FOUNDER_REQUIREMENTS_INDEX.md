# Founder requirements & audit findings — consolidated index

## Purpose and scope

This document answers one question only: **for every founder requirement, decision, future-product
idea, and independent-audit finding discussed so far, where is it actually written down, and what is
its current, code-verified status?** It is an index, not a new source of truth — every substantive
requirement already has (or now gets) a home in an existing document, and this file points to that
home instead of restating it. This keeps the project's single-source-of-truth discipline (see
`CLAUDE.md`'s "Grundprincip") intact instead of creating a second, competing description of the same
decisions.

This is a **docs-only** reconciliation. No implementation, merge, deployment, or activation happens
here. `first_cycle_go` is untouched and remains `false`.

### Correction on this sidequest's own source material

The founder directive that requested this index named an attached file,
`LIFEAI_CLAUDE_SIDEQUEST_FOUNDER_NOTES_AND_AUDIT_2026-11-16.md`, as the document to read before
making changes. That file does not exist anywhere in this repository or in any session scratchpad
checked while preparing this index. Nothing in it has been read, assumed, or fabricated. Every
requirement and finding captured below comes from the directive message's own inline text (its
numbered sections 2, 3 and 4) and from this session's own prior, already-merged-or-open work. If the
founder's prepared document contains additional content beyond what was inlined in the directive, it
still needs to be pasted or attached directly in a future message — this index can be extended from it
in a follow-up edit without needing a rewrite, since every section below is structured as an
append-friendly table.

### Correction on local-Mac worktree references

GitHub issue #449 ("Local-recovery tracking: unverified report of 103 local worktree dirs...") is the
only place this repository's documentation describes the founder's Mac. Its own body is explicit that
**nothing about that report has been independently verified** — not the worktree count, not whether
they still exist, not whether they were ever deleted. A repo-wide search of every doc on this branch
found no other place claiming local worktrees currently exist on the founder's Mac, so there was
nothing stale to correct. If the founder has since deleted those local worktrees, that fact belongs as
an update to issue #449 itself (the single authoritative place this is tracked), not duplicated here.

---

## 1. Founder's core requirements — universal to LifeAI, not Life-Builder-only

The founder's directive is explicit that these are universal LifeAI requirements, not merely features
of the Life Builder/Life Code product line. The table below separates **architectural intent**
(decided, not yet fully implemented/verified) from **implemented and verified** (real code, real
tests, real CI), per the founder's own instruction not to blur the two.

| # | Requirement | Authoritative home | Status |
|---|---|---|---|
| R1 | Locally executable AI with no mandatory paid external API | `docs/AI_PROVIDER_ARCHITECTURE.md` §10 "Local models as a first-class citizen, not a fallback" | **Architectural intent, partially implemented.** `OllamaProvider`/`OpenRouterProvider` exist and are registered (`backend/app/providers/`); real-invoke path is still hard-blocked (`ProviderActivationBlocked`) pending first-cycle authorization. No measurement yet of whether the founder's actual Strato VPS (2 GB RAM) can run a useful local model — see R-new-3 below and the "cost and local-model requirements" section of the founder's most recent directive. |
| R2 | Chat and embeddings support self-hosted models | Same as R1, §§2–4 (capability-based interfaces, normalization layer, logical models) | Architectural intent — capability interface already covers chat+embeddings generically; no self-hosted embedding model has been deployed or benchmarked yet. |
| R3 | External paid providers remain strictly optional and require authorization | `docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md`; provider activation block in `app/workforce/provider_worker.py` | **Implemented and verified** — provider real-invoke is fail-closed (`raise ProviderActivationBlocked(...)`) until explicit authorization; this is the same mechanism, not a second one. |
| R4 | LifeAI remains available for conversation while independent workers execute | No dedicated doc found. Closest existing material: `docs/LIFE_MULTI_AGENT_WORK_COORDINATION.md`, `docs/LIFE_SUPERVISOR_PRODUCTION_ENTRY.md` (worker/supervisor split), but neither one specifically addresses *chat* availability during background execution. | **Missing / incomplete** — see §4 below. |
| R5 | Users must be able to send text while responses are generated | No dedicated doc found. | **Missing / incomplete** — see §4. |
| R6 | Users must be able to speak while LifeAI speaks, without automatic abrupt interruption; interrupting speech is different from cancelling a background task | No dedicated doc found anywhere in `docs/*.md` (checked via full-repo grep for "speech"/"röst"/"avbryt"/"interrupt" — zero hits describing this distinction). Cancellation-of-background-task semantics exist (`backend/app/mainai_execution/cancellation.py`), but speech/voice interruption is an entirely separate, currently undocumented concern. | **Missing / incomplete** — see §4. This is the clearest gap found in this pass: a real, explicit founder requirement with no authoritative document and no code. |
| R7 | Long-running work must preserve checkpoints, authorization and progress | `docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md`, `backend/app/mainai_execution/execution_job.py` (checkpoint/resume), `backend/app/mainai_execution/recovery_takeover.py` | **Implemented and verified** — real crash/resume test exists (PR #443's `test_a_real_crash_between_commit_and_push_recovers_through_the_existing_takeover_pipeline_and_the_pr_is_still_created_once`). |
| R8 | Live status must reflect actual worker events | `docs/LIFE_DEVELOPMENT_OPERATOR.md`, `app/worker.py` poll ticks, `MainAITaskEvent` rows | Architectural intent for a *user-facing* live status surface; the underlying event stream (`MainAITaskEvent`) is real and implemented, but no doc confirms a live UI/notification surface consuming it end-to-end yet. |
| R9 | Goals, blueprints, milestones and new ideas must preserve continuity | `docs/LIFE_GOALS_DREAMS_DEPENDENCIES.md`, `docs/LIFE_MEMORY_THREADS.md` ("Active Context bridge"), `docs/LIFE_PARTIAL_PLAN_INSERTION.md` | Architectural intent, substantially designed; `plan_insertion.py` (partial-plan insertion) is real code, not just a doc. |
| R10 | New ideas must not silently modify authorized work | `docs/LIFE_EXECUTION_AUTHORIZATION_ENVELOPE.md` §"Never mutate, always supersede"; `backend/app/mainai_execution/planner.py`'s lesson-application comment ("ever ADDS to verification_plan, never removes a step the planner/founder specified") | **Implemented and verified** for the authorization-envelope and lesson-application mechanisms specifically; not yet verified for every "new idea" entry point (e.g. `plan_insertion.py`'s own validation, PR #444 scope). |

---

## 2. Independent audit findings — current-code-revalidated status

Findings are given both their plain-English description (from the founder's directive, section 3) and
the Freebuff review's short code where one was assigned (base commit `69dc5ea5a9cfc750303ea1a9b335c78612bc8f39`,
same as this branch's own base). Every "open" row below was re-checked against the current `dev` tip
(`claude/det-kommer-mer-879lcm` @ `69dc5ea5`) while preparing this index — not copied from an older
report — and the file:line evidence is given so the assigned remediation package can start from fact,
not re-derive it.

| Finding | Freebuff code | Status | Evidence |
|---|---|---|---|
| Draft PR creation and recovery defects | SC-1 | **Fixed** | PR #450 @ `2cbbc3a5675010aa0f33e31d9e5fb95f651528f6` — `create_pull_request()` now always requests `draft: true`; `_handle_open_pr()` fails closed on any non-`True` draft state and cross-checks reused-PR head commit against the task's real pushed commit. Not yet merged to `dev`. |
| False merge attribution (a human's real non-draft PR silently credited to MainAI) | — (subsumed by SC-1) | **Fixed**, same PR/SHA as above | The exact mechanism this described — `_handle_open_pr()` treating *any* open PR for the head/base as "ours" with no ownership/draft check — is the bug SC-1's fix closes. |
| RLS test false positives (#403's swallowed-assertion cross-owner test) | — | **Fixed** | PR #440 @ `d4542876917edd4ee1f6c739425f2717379e0022` — real `pytest.raises(DBAPIError)` + independent superuser row-count check replaces the `except Exception` pattern that could swallow its own `assert False`. The underlying RLS policy itself was independently confirmed sound; only the test was broken. **Open, not yet re-propagated:** the identical swallowed-assertion pattern still exists, unfixed, in the sibling file `backend/tests/security/test_rls_isolation_work_candidates.py` — flagged in this session, deliberately left for its own follow-up rather than folded into #440's scope. |
| GitHub write credentials stored in worktree remotes | PROV-1 | **Open — confirmed present at current `dev` tip** | `backend/app/mainai_execution/worktree.py:102-103,179` — `_authed_remote_url()` builds `https://x-access-token:{token}@github.com/{repo}.git` and this exact string is passed to `git remote add origin ...`, which writes the live write-scoped GitHub token in cleartext into that worktree's `.git/config` on disk. Any process, backup, or log capture that can read the worktree directory recovers the token. Severity: high (credential exposure, not just logic). No dependency on other open findings; a self-contained fix. |
| Empty verification plans incorrectly passing | VS-1 | **Open — confirmed present at current `dev` tip** | `backend/app/mainai_execution/verify.py:129-136` — `verify_task()`'s own docstring states "An empty verification_plan passes trivially... the planner is responsible for populating verification_plan," i.e. this is enforced by convention, not structurally. `backend/app/mainai_execution/planner.py:198-211`'s plan-time validation checks for malformed steps and unsafe `targeted_tests` targets, but never rejects a `repo_edit` or `open_pr` task (`KNOWN_TASK_TYPES` at line 81) for having an empty `verification_plan`. A `repo_edit`/`open_pr` task with a maliciously or accidentally empty plan can still reach a recorded "verified and passed" completion with zero real checks run. |
| Cancellation and automatic takeover defects | AV-1a / AV-2a | **Open — not independently re-verified this pass** | Not revalidated against current code in this docs-only pass; flagged for the assigned remediation package (`backend/app/mainai_execution/cancellation.py`, `recovery_takeover.py`) to confirm the exact current-code failure mode before fixing. Do not treat as already disproven — it has not yet been checked, only *noted* here. |
| External actions occurring around cancellation | AV-2a (same finding, different angle) | **Open — not independently re-verified this pass** | Same note as above; the concern is whether a `cancel`-requested task can still let an in-flight push/PR-create/CI-wait side effect land after cancellation is recorded. |
| PR head-SHA and provenance verification | (part of SC-1's own scope) | **Fixed**, same PR/SHA as SC-1 | Commit-identity cross-check is part of the same `_handle_open_pr()` fix. |
| Migration lineage conflicts | — | **Resolved for the specific collision found; one separate collision tracked, not yet resolved** | PR #440 resolves the #408/#403/#411/#437 "0069" rename collision (pure rename, zero object-level overlap, verified via PR #417's DDL-parsing proof). A **second, unrelated** 0069 collision exists between this composition and PR #447's own chain (`claude/import-security-integration-on-dev`) — a genuine Alembic graph fork (both declare `down_revision = "0068"` independently), not a string collision. Per this repo's Merge-regel, neither branch has been touched in anticipation — the founder's own merge-order decision is the open dependency, already recorded in `docs/BRANCH_REGISTRY.md`'s own tail entries. |
| Unresolved backend CI failures | — | **Open — root cause not found, diagnostic instrumentation added instead** | PR #447 @ `0b18aeb21bfe759bbcc1ba9c12f75787e15ac343` — 3 (sometimes more) `mark_job_superseded`-related backend test failures survive across two investigation rounds; four concrete hypotheses (clock skew, table-truncation backlog, test-order privilege leakage, RLS-context loss) tested and disproven. Self-diagnosing instrumentation (row-state capture folded into the raised exception) was added so the next real occurrence is self-explaining, rather than continuing to guess blind. Still genuinely open. |
| Missing live GitHub discovery wiring | — | **Open, in progress on an existing branch — do not duplicate** | PR #395 (`claude/github-client-repo-evidence`, still open) adds the three read-only discovery methods (`list_pull_requests`/`list_issues`/`list_commits`) this depends on. PR #450 (SC-1's fix) was deliberately branched from `dev` rather than from #395 specifically to avoid a conflicting edit to the same file — the two should be reconciled by whichever merges second, not by either session pre-emptively rebasing (Merge-regel). |
| Local-model deployment and capability gaps | — | **Open, classified safely-deferred in the readiness matrix** | `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md`'s "Amended 2026-11-16 (round 2)" section (PR #446 @ `de54da658a55f0798d9e4aae4c08178b5ce8695b`, not yet merged to `dev`) already classifies the model-provider-abstraction gap as safely deferred — existing adapters are real code, but no local inference service has actually been deployed/measured against the founder's real Strato VPS. The founder's most recent directive raises this again under "cost and local-model requirements" — see §4/§7 below; this is not a new finding, it is the same one, now with an explicit instruction not to presume hardware upgrades are needed before measuring. |
| AT-1b / AT-1c — LocalCLIAdapter does not enforce the assigned tool/path envelope at the subprocess boundary | AT-1b / AT-1c | **Open — not independently re-verified this pass** | Flagged for the assigned remediation package; not re-read against current code in this docs-only pass. |
| PI-1b / PI-1c — MainAI repo edits require explicit authorized paths, canonical path containment, symlink protection | PI-1b / PI-1c | **Open — not independently re-verified this pass** | Same note. |
| PI-1d — proposal-mode repo edits reportedly modify the backend's live shared checkout | PI-1d | **Open — not independently re-verified this pass; high severity if confirmed** | If true, this is an isolation defect distinct from (and more severe than) the worktree-based execution path this session already exercised end-to-end on PR #443 — that path uses a dedicated `git worktree add`, never the shared checkout. Needs its own targeted reproduction before being assumed true or false. |
| EGRESS-0 / EGRESS-1 / EGRESS-4 — provider egress authorization context | EGRESS-0 / EGRESS-1 / EGRESS-4 | **Open — not independently re-verified this pass** | The founder's correction is explicit: founder-wide egress cannot be authorized merely by resolving `FOUNDER_USER_ID`; needs non-null ownership, RLS, append-only disclosure logs, erasure semantics, and either authenticated founder authority or a verified live worker lease — a durable, explicitly authorized job is preferred over a weak process-role/module-name check for scheduled work. Not re-read against current code in this pass. |

---

## 3. Future ecosystem vision — pointer, not a copy

The founder's directive section 4 (Life Builder/Life Code, user-controlled project blueprints,
expandable ideas, long-running agent work, workbenches, fair free/paid tiers, source-code ownership,
LifeOS, Life Phone, Life Store, Life Dating, Life Social, Life Causes, native business applications,
tenant agents) is **already comprehensively captured**, item by item, in:

- `docs/LIFE_FOUNDER_VISION_BACKLOG.md` (PR #445, branch `claude/ecosystem-vision-reconciliation`,
  not yet merged to `dev`) — an atomic, traceable backlog with one `FV-xxx-NNN` entry per concept:
  - LifeOS / Life Phone / Life App / Life Store → §1 (`FV-OS-001`, `FV-OS-002`, `FV-APP-001`, `FV-APP-002`)
  - Life Dating → §3 (`FV-DAT-001/002/003`)
  - Life Social, Life Causes → §4 (`FV-SOC-001`, `FV-CAU-001`)
  - Life Workspaces (durable, multi-workspace sessions — this is the "Durable Workspaces" item named
    in the founder's reconciliation list) → §5 (`FV-WSP-001`)
  - Life Word / Life Sheets / Life Teams and other native business applications → §6 (`FV-PRD-001/002/003`)
  - Life Engineering/research/patent tools, Life Security Lab, Life Development Monitor → §7
    (`FV-ENG-001/002`, `FV-SEC-001`, `FV-DEV-001`)
  - Portable installation/recovery, multi-tenant business AI agents, provider independence → §8
    (`FV-REC-001`, `FV-TEN-001`, `FV-PRV-001`)
  - Economic philosophy (people/product first, transparent pricing, future-profits-fund-public-services) → §9
  - Multi-model AI workforce → §11
  - Blueprint-driven collaborative AI development (Life Builder/Life Code's actual mechanism) → §13a
    (`FV-BLU-001` through `FV-BLU-005`)
  - §10 of that same document already lists which founder-named items are covered *elsewhere* instead
    of being re-derived there (Life Patent Intelligence, multi-coin Life Mining, voice-synthesis
    research, Life Development Monitor, Life Workspaces, naming collisions) — the same
    don't-duplicate discipline this index is also following.
- `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` — the memory/knowledge architecture (§§3-4, the 8-layer
  memory model) that any of the above ultimately depends on for continuity.

This index intentionally does not restate any of the above. The only action taken here is confirming
that none of the founder's section-4 items lack a home — they do not; every one has a numbered
`FV-xxx` entry already.

---

## 4. Explicit missing or incomplete requirements

Only one item surfaced in this pass that has **no existing document at all**, as opposed to having a
document whose implementation is incomplete:

- **R4/R5/R6 together** — "LifeAI remains available for conversation while independent workers
  execute," "users can send text while responses are generated," and especially "users can speak while
  LifeAI speaks, without automatic abrupt interruption; interrupting speech is different from
  cancelling a background task." A full-repo grep for `speech|röst|avbryt|interrupt` found no document
  describing this chat/voice-concurrency model at all — every hit was about plan interruption,
  construction interruption, or unrelated phrasing. This needs a new, small, dedicated document (a
  natural candidate name: `docs/LIFE_CONVERSATIONAL_CONCURRENCY.md`) once the founder confirms the
  exact intended behavior (e.g.: does "without automatic abrupt interruption" mean LifeAI's speech
  output ducks/pauses and resumes, or that user speech is buffered and merged in after LifeAI finishes
  the current sentence, or something else) — this index does not invent that design, it only records
  that the design does not yet exist anywhere.

Every other item in the founder's section 2 has at least architectural-intent coverage (§1 above).

---

## 5. Priority and dependency classification

Reusing this repo's own established four-way classification (first introduced in
`docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md`'s "Amended 2026-11-16 (round 2)" section, PR #446):

| Category | Items |
|---|---|
| **Mandatory before first supervised cycle** | PROV-1 (credential-in-worktree-remote exposure — self-contained, no dependency); VS-1 (empty-verification-plan trivial pass on `repo_edit`/`open_pr` — self-contained); the already-tracked readiness-matrix row 10/11 blockers (parser isolation, exact-SHA CI/independent acceptance) — unchanged, carried over, not re-derived here. |
| **Conditional on enabled capability** | AT-1b/AT-1c (LocalCLIAdapter envelope enforcement — only matters once that adapter is actually enabled for real execution); EGRESS-0/1/4 (provider egress authorization — only matters once a provider's real-invoke path is unblocked); PI-1d (proposal-mode isolation — needs confirmation it's even reachable in the current, worktree-based execution path before it's "mandatory"). |
| **Safely deferred** | Local-model deployment/capability measurement (R1/R2, needs a resource-measurement pass on the real Strato VPS before any hardware decision, per the founder's explicit "solution-first" instruction); AV-1a/AV-2a (cancellation/takeover — needs its own re-verification pass before it can be classified further; provisionally here pending that). |
| **Future product** | Everything in §3 (the full ecosystem vision) — explicitly out of the launch-critical path, per the founder's own "preserve the vision without allowing future ideas to derail the objective" instruction. |

---

## 6. P0 engineering defects requiring implementation

These are the items from §2 classified "Mandatory" in §5, restated as concrete implementation
assignments (not implemented in this docs-only PR):

1. **PROV-1** — stop writing the raw GitHub token into the worktree's `.git/config` via
   `_authed_remote_url()`. A credential helper (`git config credential.helper`, or a short-lived
   `GIT_ASKPASS` script, or an in-memory HTTP proxy already used elsewhere in this backend for
   provider calls) avoids ever persisting the token to disk inside a worktree directory other
   processes/backups can read. Acceptance test: after `push_worktree_branch()` runs, grep the
   worktree's `.git/config` and confirm the literal token string is absent.
2. **VS-1** — reject (at plan-validation time, in `planner.py`'s existing per-task validation loop,
   next to the `targeted_tests` target check) any `repo_edit` or `open_pr` task whose
   `verification_plan` is empty, unless the task is explicitly marked as having no verifiable side
   effect (mirroring the existing `read_only_audit` exemption `verify_task()`'s docstring already
   describes). Acceptance test: constructing a `repo_edit`/`open_pr` task spec with an empty
   `verification_plan` must raise `PlanValidationError` at plan-creation time, not merely "pass
   trivially" at verification time.

Both are self-contained, non-overlapping, and can be assigned to separate remediation packages without
either blocking the other.

---

## 7. Deferred architecture and product milestones

- Local-model resource measurement on the founder's actual Strato VPS (2 GB RAM) before any hardware
  upgrade or paid-service request — per the founder's explicit "solution-first engineering policy."
  Concretely: measure current RAM/CPU headroom with the existing stack running, try the smallest
  practical local embedding/chat model under `OllamaProvider`, and report real numbers before
  recommending 8 GB/16 GB.
- Everything in §3 (the full future-ecosystem vision) — tracked exhaustively in
  `docs/LIFE_FOUNDER_VISION_BACKLOG.md`, explicitly deferred past the first supervised cycle.
- The "Permanent Founder Requirements" named in the founder's most recent directive (Life Engineering
  Toolkit; Solution Discovery & Resource Optimization; Continuous Learning & Model Evolution) are
  architectural intent, reusing existing Work Driver/goals/plans/jobs/authorization/checkpoint
  machinery per the founder's own instruction not to build a second orchestrator — these do not yet
  have a single dedicated document; the natural next step (not done in this PR, to avoid scope creep)
  is a short doc under `docs/` cross-referencing `LIFE_AUTONOMOUS_DEVELOPMENT_LOOP.md`,
  `LIFE_SELF_OPTIMIZING_WORK_INTELLIGENCE.md`, and `LIFE_STRATEGY_EVALUATION_AND_PROMOTION.md`
  (all three already exist and already cover large parts of this) rather than a new standalone one.

---

## 8. Acceptance criteria and verification evidence

For every "Fixed" row in §2, the evidence is the PR/SHA cited inline — no new evidence collected here,
by design (this is a docs pass, not a re-test pass). For every "Open" row, §6 above states what an
executable acceptance test must prove; the assigned remediation packages own actually writing and
running those tests against real PostgreSQL / real CI, per the founder's standing "never declare a
test green without independent verification" rule already established this session.

---

## 9. Links to related existing PRs and issues

| PR/Issue | What it is | Status |
|---|---|---|
| #395 | `github_client.py` read-only discovery methods (`list_pull_requests`/`list_issues`/`list_commits`) | Open |
| #440 | 0069 migration composition (#408+#403+#411+#437) + RLS false-positive fix | Open, draft |
| #443 | Real end-to-end vertical-slice proof | Open |
| #445 | Founder ecosystem vision backlog | Open (branch `claude/ecosystem-vision-reconciliation`) |
| #446 | First-cycle readiness matrix (amended round 2) | Open |
| #447 | Import-security integration + diagnostic instrumentation | Open |
| #449 | Local-recovery tracking (founder's Mac, unverified) | Open |
| #450 | Draft-PR security contract fix (SC-1) | Open, draft |

---

## See also

- `docs/BRANCH_REGISTRY.md` — the project's living branch/PR map; this index does not replace it.
- `docs/LIFE_FOUNDER_VISION_BACKLOG.md` — the full future-ecosystem backlog this index points to in §3.
- `docs/LIFE_FIRST_CYCLE_READINESS_MATRIX.md` — the exact-SHA readiness matrix this index's §5
  classification reuses rather than re-inventing.
- `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md` — the memory/knowledge architecture.
