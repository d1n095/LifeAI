# MainAI — First Self-Improvement Acceptance Contract

Companion to `docs/MAINAI_V1_GOAL_TO_AUTONOMY.md` and `docs/MAINAI_V1_READINESS.md`. This
document is a durable pre-commitment, not a live decision — the answers below must be fixed
BEFORE the first bounded self-improvement run, not decided ad hoc mid-run. Gate: do not
execute this run until Phases 1-5 of the correction pass (`docs/ACTIVE_WORK_CURSOR_CORRECTION_
PASS_182_183.md`) are merged green, per `docs/MAINAI_V1_READINESS.md`'s blocker map.

## What may MainAI choose itself?

- Which specific lines/files to change, within the founder-authorized path/capability ceiling.
- Its own step ordering and retry strategy within `Driver`'s existing bounds
  (`max_actions`/`max_elapsed_seconds`).
- Whether to request provider assistance vs. use a deterministic plan (`plan_founder_request`'s
  existing `DETERMINISTIC_PLAN_AVAILABLE` vs `WAITING_PROVIDER` branch — already real,
  unchanged for this first run).
- What to include in its own free-text rationale/interpretation fields (per this session's
  #190 regression: this is DATA, never authority — confirmed empirically that free text can
  never widen scope regardless of content).

## What must the founder provide (cannot be self-derived)?

Per the standing `LLM MAY DERIVE WORK, LLM MAY NOT DERIVE AUTHORITY` invariant:
- The bounded goal itself (`original_instruction`).
- `authorized_paths` — explicit, narrow, founder-approved (see below for the recommended
  first-goal scope).
- `authorized_capabilities` — explicit list from `DEVELOPMENT_CAPABILITIES`, not "all of them."
- `authorized_risk` tier.
- Provider spend ceiling, if provider assistance is authorized at all for this first run (see
  recommendation below: NOT for the first milestone).
- Explicit task approval per task (`grant_task_approval`) — not blanket pre-approval of
  whatever MainAI decides to propose.

## How narrow must paths be?

**Recommendation: a single, pre-identified, low-blast-radius file or narrow directory** — not
"the whole repo," not even "the whole backend." Concretely: a single existing test file with a
known, narrow gap, OR a single new file with zero existing callers (so a mistake has no
downstream blast radius). Exclude, explicitly, by NOT including in `authorized_paths`:
- Anything under `app/development_operator/`, `app/development_driver/`,
  `app/development_supervisor/`, `app/execution_envelopes/`, `app/provider_spend/`,
  `app/egress_policy/` — the authority/security core itself. MainAI must never be authorized
  to modify the code that enforces its own boundaries, for this first run or any near-term
  one.
- `app/models/` broadly (schema/migration surface).
- `alembic/versions/` (migrations).
- Anything under `.github/` (CI/deploy configuration).
- `CLAUDE.md`, `docs/BRANCH_REGISTRY.md`, and any `docs/ACTIVE_WORK_*` coordination file — the
  swarm's own shared coordination surface. MainAI must not be authorized to edit the documents
  humans/other agents use to coordinate around it.

## What capabilities?

**Recommendation for the first milestone**: `read_file`, `patch_file` or `create_file`
(whichever the goal needs, not both unless genuinely required), `run_focused_test`,
`verification_evaluate`. Explicitly EXCLUDE: `delete_file` (no destructive capability on the
first run), `run_full_suite` (scope verification to the specific touched area — narrower blast
radius, faster feedback, avoids the run being blocked by an unrelated pre-existing failure
elsewhere), `stage_scoped_changes`/`commit_scoped_changes` (see "can it commit locally" below),
`push_branch` (already structurally disabled via `remote_write_authorized=False` — confirm
this stays `False`; don't rely on capability-list omission alone as the only fence).

## What provider disclosure is allowed?

**Recommendation: none, for the first milestone.** `provider_spend_authorized=False`. The
first run should prove the LOCAL execution chain (plan → write → verify → finalize) end to
end without also introducing external-provider disclosure risk in the same experiment — two
different risk categories (execution authority, information disclosure) should not be
validated for the first time simultaneously. If the goal genuinely requires provider-assisted
planning, that itself signals the goal is not narrow enough for a first run — pick a narrower
one.

## Can it create new tasks?

**No, not for the first milestone.** `propose_plan_via_ai()` → `create_plan()` is a real,
production-wired multi-task decomposition path (confirmed in `docs/MAINAI_V1_GOAL_TO_
AUTONOMY.md`) — but for the FIRST self-improvement run specifically, recommend the founder
pre-create a single task rather than letting MainAI decompose its own goal into multiple tasks
on the very first real run against its own codebase. Multi-task autonomous decomposition
against MainAI's own codebase is a reasonable SECOND milestone, not the first.

## Can it modify tests?

**Only tests within the same narrow `authorized_paths` scope, and only if the goal is
specifically about adding/fixing a test.** Never authorize test modification as a blanket
capability separate from the goal itself.

## Can it modify authority code?

**No. Never, for any near-term milestone, not just the first.** This includes
`development_operator/`, `development_driver/`, `development_supervisor/`,
`execution_envelopes/`, `provider_spend/`, `egress_policy/`, and `app/rls.py`. This is a
standing exclusion, not a first-run-only caution — self-modifying its own security boundary is
a fundamentally different risk class than ordinary feature work, and should require a
dedicated, much later, much more heavily-scrutinized milestone with its own separate
acceptance contract.

## Can it touch Vault code?

**No.** `app/egress_policy/`, anything under `app/models/document*.py`/`document_chunk.py`,
and migration 0062's ledger code are off-limits for the same reason as authority code above —
the founder's own private-data protection layer.

## Can it commit locally?

**Yes, `stage_scoped_changes`/`commit_scoped_changes` may be authorized** — local git commits
inside the worktree are a reasonable part of proving the full local-write → verify → finalize
chain, and are NOT remote effects. `push_branch` (`REMOTE_WRITE`) must remain unauthorized
(`remote_write_authorized=False`), matching the standing invariant already enforced throughout
the night-run correction pass.

## What verification threshold is required?

**The goal's own stated verification plan must pass at `run_focused_test`-scope, AND the
founder must manually review the actual diff before any merge/push decision.** No threshold
weaker than "the specific test the goal names actually passes" should ever be accepted as
sufficient, even for a narrow goal.

## What requires founder review before merge/push?

**Everything, for the first milestone.** There is no auto-merge tier for this run. Flow:
MainAI completes the bounded goal locally (plan → write → verify → finalize,
`remote_write_authorized=False` throughout) → founder reviews the actual diff and commit
history in the local worktree → founder decides whether to push, discard, or request changes.
This is a manual gate, not a policy MainAI itself evaluates — matching this project's own
"FOUNDER AUTHORITY CANNOT BE CREATED FROM MODEL OUTPUT" doctrine applied to the final ship
decision, not just execution authority.

## Recommended first founder goal

**"Add a missing edge-case regression test for a specific, already-identified, narrow gap in
an already-reviewed function, in one named test file."** Narrow enough that the expected diff
is a single new test function in a single file; broad enough to genuinely exercise the full
plan → write → verify → finalize chain (a new test is a `create_file`/`patch_file` +
`run_focused_test` + `verification_evaluate` sequence, the same shape as every soak test this
session reviewed); low-risk because a wrong or broken test has zero production blast radius.

**Explicit non-goals for the first milestone** (defer to later, separately-contracted runs):
provider-assisted planning, multi-task decomposition, any push/remote effect, any touch to
authority/Vault code, any capability beyond the narrow list above.
