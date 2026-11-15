# Durable Life Workspaces — gap analysis against existing architecture

## Founder directive

"RESUME ACTIVE DEVELOPMENT WHEN CAPACITY IS AVAILABLE" (2026-11-15), Priority 3: a future
requirement for MainAI to support multiple isolated, persistent workbenches — "like having
several groups of open windows, documents, highlighted passages and notes." A workbench should
preserve current task/progress; bookmarks, annotations and source references; exact document
versions and commit SHAs; pending decisions, blockers and next steps; verification evidence;
recovery checkpoints; owner/tenant permissions — and support safe pause/resume, crash recovery,
source-change detection, and concurrent isolation. *"Before implementing anything, compare this
requirement with existing Memory Threads, Active Context, MainAI checkpoints and work execution
architecture. Record only genuinely missing requirements in the existing documentation. Do not
introduce a duplicate memory, scheduler, task or execution system. This is a bounded
documentation/gap-analysis task, not authorization for a new major implementation."*

**This document is exactly that: a gap analysis, not a design, and not a build.** Nothing below
authorizes a new table, service, or system. `first_cycle_go=false`; no code changes accompany
this document.

## A naming collision to resolve immediately, before anyone names anything "Workbench"

`app/routers/workbench.py` (`/api/workbench`, `WorkbenchAnalyzeIn`/`WorkbenchAnalyzeOut`/
`WorkbenchSaveIn` in `app/schemas.py`, documented further in
`docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md`) **already exists** under the name "Workbench" — a
single-shot AI analysis endpoint (ask a question about a project/source → a structured
conclusion+critique → optionally save as a new `Document`). It is not persistent, not
resumable, and has no notion of multiple concurrent sessions. This is a **naming collision,
not a functional duplicate** — the founder's own directive title already avoids it by saying
"Life Workspaces," not "workbenches." This document uses **"Life Workspace"** throughout and
recommends any future work do the same, to avoid confusing the two in code, docs, or the
router namespace.

## The four named existing systems, read directly (not assumed)

### Active Context (`app/models/active_context.py`, `app/active_context/service.py`)

`ActiveContextSet` (owner_id, label, anchor_type/anchor_ref, idempotency_key) holds
`ActiveContextMember` rows (object_type/object_ref, inclusion_reason, rank, state —
active/pinned/suppressed/stale/superseded, activation_path, source_provenance,
valid_from/valid_until, expires_at), with an append-only `ActiveContextEvent` log. A single
owner can already have **multiple named sets** — no constraint forces exactly one — anchored
to over 30 supported object types (`mainai_goal`, `mainai_plan`, `mainai_task`,
`mainai_checkpoint`, `mainai_recovery_record`, `conversation`, `document`, `knowledge_claim`,
`project`, `life_problem`, and more). This is the closest existing match to "which documents,
goals, tasks and sources are currently in focus" — the nearest thing this codebase has to
"bookmarks and source references" today.

### MainAI checkpoints (`MainAICheckpoint`, `app/models/mainai_execution.py:245-265`;
logic in `app/mainai_execution/checkpoint.py`)

Already stores `goal_id`, `task_id`, `owner_id`, `plan_version`, and an `executor_state` JSON
column with a documented, closed shape: branch, worktree, **exact commit SHA**, last-completed
step, pending step — plus `test_status`, `ci_status`, and a free-text `blocker`. Append-only;
resume always reads the latest row for a goal. The full recovery pipeline
(`recovery_inspector.py` → `recovery_classifier.py` → `recovery_salvage.py` →
`recovery_takeover.py`) already performs real dead-job detection, evidence snapshotting, and
checkpoint-based resume, gated behind an explicit approval step before any takeover. This is
genuine, working crash recovery — but scoped to **one task's one execution attempt**, not a
whole multi-task session.

### Memory Threads (`app/models/memory_thread.py`)

`MemoryThread` (owner_id, manual_label, system_label, state —
active/dormant/completed/superseded/archived) with `MemoryThreadMember`
(member_kind/member_ref_id, membership_basis, provenance, valid_from/until),
`MemoryThreadRelationship`, and an append-only `MemoryThreadEvent` log. Structurally close to
Active Context (grouping + membership + append-only history) but framed around narrative
continuity over time rather than "what's open right now." No dedicated bookmark/annotation/
highlight text field exists anywhere in either system, or anywhere else in the codebase
(grepped directly; nothing found).

### Work execution architecture

`MainAIGoal → MainAIPlan → MainAITask → MainAITaskEvent` (exercised directly, as real rows,
by this session's own #443 test harness) already covers: current task/progress
(`task.status`, `plan.version`), pending decisions/blockers (`task.blocker_reason`,
`checkpoint.blocker`), verification evidence (`MainAITaskEvent.detail` JSON — the exact-SHA
evidence pattern this whole session's own PRs use), and owner/tenant permissions (`owner_id` +
RLS throughout, verified repeatedly this session). `docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md`
does not name "workspace" or "workbench" as an architectural concept anywhere — only the
existing, unrelated router above.

## Per-requirement coverage (the founder's own list, checked one by one)

| Requirement | Existing coverage | Verdict |
|---|---|---|
| Current task and progress | `MainAIGoal`/`MainAITask` (status, plan version) | **Full** |
| Bookmarks, annotations, source references | `ActiveContextMember`/`MemoryThreadMember` (membership + provenance, no free-text annotation field) | **Partial** |
| Exact document versions and commit SHAs | `MainAICheckpoint.executor_state`; #437's `commit_sha`-pinned `repository_file_source_units` | **Full** |
| Pending decisions, blockers, next steps | `task.blocker_reason`, `checkpoint.blocker` | **Full** |
| Verification evidence | `MainAITaskEvent.detail` | **Full** |
| Recovery checkpoints | `MainAICheckpoint` + the full recovery pipeline | **Full, but scoped to one task's one attempt** |
| Owner/tenant permissions | `owner_id` + RLS everywhere | **Full** |
| Safe pause/resume, crash recovery | `MainAICheckpoint`'s own resume path | **Full at the single-task level** |
| Source-change detection | Pass-3's incremental `file_commits` map, #437's `commit_sha` identity | **Exists only inside the knowledge-extraction pipeline, not as a live "this workspace's referenced source moved" check** |
| Concurrent workspace isolation | `ActiveContextSet` already supports multiple named sets per owner, with owner-scoped RLS | **Partial — sets are a flat per-owner collection, not an isolated, independently pause/resumable unit** |

## The genuinely missing requirements (recorded, not built)

Two gaps survive this comparison — everything else above is already covered by reusing
existing architecture, not by building something new:

1. **No single addressable "workspace" unit groups an `ActiveContextSet` + the goal/task(s)
   it is anchored to + a workspace-level checkpoint together as one pause/resumable whole.**
   Today these three already-existing primitives can co-exist for the same owner, but nothing
   ties them into one named, switchable unit a person (or MainAI) could pause, resume, or list
   alongside sibling workspaces. The smallest genuinely additive answer, when this is ever
   authorized, is a thin grouping reference — reusing `ActiveContextSet.id`,
   `MainAIGoal.id`/`MainAITask.id`, and `MainAICheckpoint` exactly as they are — never a
   parallel memory, scheduler, task, or execution system.
2. **No source-change detection exists at the workspace level.** Pass 3's `file_commits` map
   and #437's `commit_sha` pinning both already prove the underlying mechanism (compare a
   recorded commit against the source's current one) — but neither is wired to notice, for a
   *live, open* workspace, that one of its referenced documents or repository files has since
   changed. This is a genuinely new integration point, not a new concept: it would consume the
   existing commit-identity mechanisms, not duplicate them.

Everything else the founder listed — current task/progress, pending decisions/blockers,
verification evidence, exact document versions, owner/tenant permissions, and single-task
crash recovery — already exists, built and (for the execution/checkpoint half) exercised by
real tests this session. No new memory, scheduler, task, or execution system is needed to
satisfy any of those; the two items above are additive integration, not architecture.

## Acceptance tests and dependencies, if this is ever authorized

Per the follow-up founder directive ("CONTINUE ENGINEERING + ECOSYSTEM VISION AUDIT",
2026-11-15): *"Evaluate the smallest additive architecture that closes these gaps... Implement
only a narrowly scoped foundation if justified by verified gaps. Otherwise document the
precise acceptance tests and dependencies."* Having evaluated it, neither gap is justified as
an implementation today — no concrete workspace-consuming feature exists yet to drive the
design, and building the grouping unit or the source-change wiring ahead of such a feature
would be exactly the kind of speculative abstraction this repo's own engineering discipline
rejects. What follows is the second option: precise acceptance tests and dependencies, so a
future authorization does not have to re-derive this analysis.

### Gap 1 — the workspace grouping unit

**Dependencies (must exist/be decided before implementation starts):**
- A concrete, named consumer (a UI surface, an endpoint, or a MainAI-internal caller) that
  actually needs to pause/resume a *group* of goals/tasks together — today nothing calls for
  this; `ActiveContextSet` already serves every caller that exists. Building the grouping unit
  without one is building ahead of demand.
- A founder decision on identity: is a Life Workspace exactly one `ActiveContextSet` plus zero
  or more `MainAIGoal`s (the thin-reference design this document recommends), or does it need
  its own table with its own lifecycle independent of any single `ActiveContextSet`? The
  former requires no new table; the latter does and should not be assumed.
- Confirmation that the existing `ActiveContextSet.label`/`anchor_type`/`anchor_ref` fields are
  sufficient for "list my open workspaces" (likely yes — `anchor_type='life_workspace'` with
  `anchor_ref` as a stable slug would work today) rather than needing a new listing endpoint.

**Acceptance tests (would gate any implementation PR, not required to exist today):**
1. `test_two_workspaces_for_the_same_owner_are_independently_resumable` — create two
   `ActiveContextSet`s each anchored to a different `MainAIGoal`, confirm resuming one (reading
   its members + its goal's latest `MainAICheckpoint`) never reads or mutates the other's rows.
2. `test_pausing_a_workspace_persists_annotations_and_references_across_a_simulated_restart` —
   add `ActiveContextMember` rows with `source_provenance` data representing a reference/
   highlight, confirm they are unchanged after a simulated process restart (fresh DB session).
3. `test_switching_the_active_workspace_does_not_leak_into_the_previously_active_one` —
   confirm no shared mutable "current workspace" global/session field exists that a second
   workspace could silently overwrite (this is a structural assertion, not a behavioral one,
   given no such field exists in the codebase today).
4. `test_a_second_owner_cannot_list_discover_or_resume_the_first_owners_workspace` — the
   existing owner-scoped RLS pattern (already proven for `ActiveContextSet` and
   `MainAIGoal`/`MainAITask`) applied to whatever the grouping unit turns out to be.
5. `test_resuming_a_workspace_after_a_crash_never_re-dispatches_an_already-completed_task` —
   this is not new: it is the existing `recovery_takeover.py` guarantee, re-asserted at the
   grouping level to confirm the thin-reference design doesn't accidentally bypass it.
6. `test_a_workspaces_active_context_members_stay_within_a_bounded_count` — guards against
   unbounded growth of `ActiveContextMember` rows per set becoming an unbounded context/token
   cost at resume time (the founder's "bounded model context/resource usage" requirement);
   the exact bound is a product decision, not a technical one, and is not set here.

### Gap 2 — workspace-level source-change detection

**Dependencies:**
- A decision on where the check runs: polled on workspace resume (cheapest, matches "safe
  pause/resume" framing) vs. a standing watch (would need a new scheduled mechanism — the
  founder directive's "do not introduce another scheduler" makes polling-on-resume the only
  compliant default unless a future directive says otherwise).
- A defined scope of "referenced source": every object type `ActiveContextMember` can anchor
  to (30+) does not have a `commit_sha` or equivalent version marker today — only
  `repository_file_source_units` (#437) and Pass 3's `file_commits` map do. The dependency is
  deciding whether source-change detection covers only those two existing commit-identity
  mechanisms at first, or is deferred until a general per-object-type "current version" concept
  exists (the latter would itself be new architecture, not integration, and should not be
  built speculatively).

**Acceptance tests:**
1. `test_resuming_a_workspace_flags_a_referenced_repository_file_whose_commit_sha_changed` —
   seed an `ActiveContextMember` anchored to a `repository_file_source_units` row pinned to
   commit SHA A, simulate the underlying file now being at commit SHA B, confirm resume
   surfaces this as a flagged, non-blocking notice (never a silent auto-update of the pinned
   SHA, which would defeat the exact-SHA evidence discipline this whole session relies on).
2. `test_an_unchanged_referenced_source_produces_no_flag` — same seed, no commit change,
   confirm no false positive.
3. `test_source_change_detection_never_mutates_the_original_pinned_commit_sha` — the check is
   read-only by construction; any "update the reference" action must be a separate, explicit,
   owner-initiated step, never automatic.
4. `test_source_change_detection_degrades_to_no-op_for_object_types_with_no_version_marker` —
   confirms the check is additive (skips cleanly) rather than erroring for the 28+ anchor
   types that have no commit-identity concept yet, so partial coverage never breaks resume.

Neither gap's acceptance tests are implemented by this document. Recording them now means a
future authorized implementation PR can be verified against a pre-agreed bar instead of an
ad hoc one invented at build time.

## What this document explicitly does not do

- Does not create any new table, model, service, or router.
- Does not authorize building the "thin grouping layer" or the source-change-detection wiring
  named above — both are recorded as future, separately-reviewed work, not started here.
- Does not rename or modify the existing `/api/workbench` router — flags the naming collision
  for future documentation/code to avoid, nothing more.

No merge, deployment, activation, or security bypass. `first_cycle_go=false`.

---
_Generated by [Claude Code](https://claude.ai/code/session_016d4iixQnxxqNhDrxHHxV4Q)_
