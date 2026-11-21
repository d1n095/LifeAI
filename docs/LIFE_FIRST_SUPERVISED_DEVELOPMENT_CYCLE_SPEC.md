# Life's first supervised development cycle — specification and verification plan

Founder directive, 2026-11-15 ("CONTINUE MAINAI INTEGRATION"), Priority 4: *"Define a
concrete, minimal end-to-end acceptance scenario where MainAI: (1) reads authorized GitHub
project information, (2) recognizes existing code, decisions and requirements, (3) identifies
one genuine engineering problem, (4) produces a source-backed work proposal, (5) requests
authorization through the existing security boundary, (6) executes only within the approved
scope, (7) collects tests and exact-SHA evidence, (8) presents results for independent
review. This is a specification and verification plan, NOT permission to activate autonomous
execution. Reuse the existing architecture."*

**This document does not activate anything.** `first_cycle_go` stays `false`. Every primitive
named below already exists, already reviewed, already in this codebase — this document's own
job is mapping the founder's 8 steps onto them precisely, naming exactly which step is already
fully built versus which step genuinely needs a human in the loop for a FIRST cycle, and never
quietly assuming more automation exists than actually does.

## 0. The one framing decision this spec makes, stated up front

**The safest, smallest, most-reuse-existing-architecture version of "the first cycle" is to
formalize the exact process this very session has been enacting manually, as real database
rows instead of only a chat transcript — not to activate the dormant automated-dispatch
machinery (`app.workforce`'s `ProviderActivationBlocked`, `app.agent_coordination`'s
unreached-by-any-router `dispatch_assignment()`) as this cycle's EXECUTION step.** Those two
subsystems remain exactly as dormant as #428/#431/#433/#436 already found and left them — this
spec does not ask for or imply lifting either gate. Step 6 ("executes only within the approved
scope") is satisfied by a human-operated Claude Code session (of the kind writing this very
document) doing the work under a REAL, recorded `MainAITask` approval — the same accountability
structure, just made durable and queryable instead of living only in a conversation. Full
automated dispatch through a real adapter is real future work, named in #436, not this cycle's
scope.

## 1. Step-by-step mapping onto existing primitives

| # | Founder's step | Existing primitive | Status |
|---|---|---|---|
| 1 | Reads authorized GitHub project information | `mcp__github__*` tools / `app`'s own GitHub evidence reads (this session's own repeated PR/CI/check-run reads) | **Fully built, exercised every notification this session.** |
| 2 | Recognizes existing code, decisions, requirements | Pass 1→2→3 corpus extraction (#430/#432/#435's design) + (#437, once merged) real `MemorySourceUnit`/`KnowledgeClaim` grounding | **Partially built.** Extraction exists and is measured (precision/recall documented in #432). The extraction-to-`KnowledgeClaim` wiring #437 makes possible is NOT yet built (named explicitly in #437's own scope, not this cycle's job either). |
| 3 | Identifies ONE genuine engineering problem | — | **Deliberately human-gated for the first cycle** — see §2. |
| 4 | Produces a source-backed work proposal | `app.project_entities.service.record_interpretation_proposal()` → human-reviewed → `promote_interpretation_proposal()` → `app.work_candidates.service.record_work_candidate()`, OR (simpler, for a first cycle) a draft PR whose description cites exact `source_file`/`line_no`/`commit_sha` the way every PR in this session already does | **Fully built** (the promotion chain) **or already-practiced** (the draft-PR-with-citations pattern this whole session used). |
| 5 | Requests authorization through the existing security boundary | `app.mainai_execution.planner.create_goal()`/`create_plan()` → `POST /api/mainai/execution/tasks/{id}/approve` (`require_founder`) → `app.mainai_execution.approval.grant_task_approval()` | **Fully built and tested** (the entire approval chain #428/#431/#433 hardened). |
| 6 | Executes only within the approved scope | A human-operated Claude Code session, scoped to exactly the approved `MainAITask`'s description, same accountability model this whole session already follows | **Already-practiced, not yet formally RECORDED against a real `MainAITask` row** — see §0. |
| 7 | Collects tests and exact-SHA evidence | This session's own established discipline: exact commit SHAs, real CI check-run ids, `ruff` before/after diffs, real test files | **Fully built, exercised in every PR this session opened.** |
| 8 | Presents results for independent review | A draft PR, `first_cycle_go=false`, no self-certification | **Fully built, exercised in every PR this session opened.** |

Five of eight steps are already fully built and already exercised by this very session; two
(step 2's extraction-to-claim wiring, step 6's formal recording) are real, named, not-yet-built
work; one (step 3) is a deliberate design choice, not a gap.

## 2. Why step 3 is deliberately human-gated, not automated, for the first cycle

This session's own measured evidence (`docs/LIFE_PROJECT_KNOWLEDGE_INVENTORY_PASS2.md`) found
real classifier false-positive rates as high as 70% for the single largest extraction category
(`superseded_marker`). Letting MainAI autonomously PICK "one genuine engineering problem" from
noisy, unreviewed extraction output for the FIRST cycle this project ever runs would mean the
first cycle's own starting point is exactly the kind of unverified signal
`docs/LIFE_PROJECT_KNOWLEDGE_INGESTION_DESIGN.md` §5 already commits to never auto-promoting.
The correct, smallest, already-consistent answer: MainAI surfaces a short, ranked list of
candidates (e.g. `high`-confidence `requirement`/`open_question` records from the Pass-3 JSON,
each with its real `source_file`/`line_no`/`commit_sha` citation), and the **founder picks or
confirms exactly one**. This is not a weaker cycle — it is the identical "review before
promotion" discipline this whole project already enforces everywhere else, applied to the
starting problem itself, not only to the proposal that follows it.

## 3. The concrete minimal acceptance scenario, end to end

1. A human (founder) runs (or a Claude Code session runs, read-only) the existing Pass-3
   script against the current corpus; its JSON output's `high`-confidence records are the
   candidate list.
2. The founder picks ONE record (or supplies their own problem statement with its own real
   source citation — either is acceptable, the founder's own confirmation is what matters,
   not which direction the suggestion came from).
3. A `MainAIGoal` is created (`create_goal()`) with that problem statement as its
   `original_instruction`, citing the exact `source_file`/`line_no`/`commit_sha` from step 2
   in its own description — this is what makes the goal source-backed, not just stated.
4. `create_plan()` proposes one or more `MainAITask` rows under that goal.
5. The founder calls `POST /tasks/{id}/approve` for each task that needs it (per
   `APPROVAL_POLICIES`'s existing `standard_repo_work` defaults) — this is the REAL security
   boundary, already built, already hardened by #428/#431/#433.
6. A human-operated Claude Code session does the actual work (read, code, test, commit),
   scoped to exactly that task's description — never wider.
7. The session records real evidence: exact commit SHA, `ruff` diff, real test results —
   exactly this session's own established pattern, now additionally linked to the real
   `MainAITask`/`MainAIGoal` row via `MainAITaskEvent`, not only described in a PR body.
8. A draft PR is opened, citing the task/goal ids and the exact-SHA evidence from step 7, for
   independent review. No merge without separate, explicit authorization.

**Acceptance criteria for calling this scenario successfully run once:** a real `MainAIGoal`
row exists whose `original_instruction` cites a real source commit; a real `MainAITaskEvent`
with `event_type=approval_granted` exists for its task, recorded by the founder's own action,
not by the executing session; a draft PR exists whose description traces back to that exact
goal/task id and to the exact source citation from step 2; and `first_cycle_go` remains
`false` throughout, with no provider-activation gate touched.

## 4. What remains genuinely unbuilt, named for whoever picks this up next

- The extraction-to-`KnowledgeClaim`/`InterpretationProposal` wiring (#437's own named future
  work) — not required for the scenario in §3, which can cite a Pass-3 JSON record's raw
  `source_file`/`line_no` directly in a goal's own text; required only if the founder wants
  step 2 itself to produce a real, queryable `InterpretationProposal` row rather than a cited
  JSON line.
- A formal "this Claude Code session is now operating under approved MainAITask X" linkage —
  today, a human-operated session's own PR description is the record; a tighter version would
  have the session itself record a `MainAITaskEvent` at start and completion, tied to the
  task id, rather than relying on the PR body alone.

## 5. What this document explicitly does not do

- Does not set `first_cycle_go=true` or propose a timeline for doing so.
- Does not activate `app.workforce`'s or `app.agent_coordination`'s dormant real-invoke paths
  — step 6 is satisfied by a human-operated session, not an automated adapter dispatch.
- Does not build the extraction-to-claim wiring named in §4 — named as future work, not done.
- Does not pick a real engineering problem on its own — §2 explains why that is deliberate.
