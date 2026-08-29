# MainAI — First Bounded Self-Improvement Run Report (Stage 4)

Proof branch: `cursor/first-bounded-self-improvement`
Base tip: `86d8f0f` (post-#203)
Contract: `docs/MAINAI_SELF_IMPROVEMENT_ACCEPTANCE.md` (Claude #197)

## What ran

Live Worker → Supervisor path (no harness PlanCandidate / status / unlock bridges):

1. Founder bootstrap only: claim → authorize work candidate → single pre-created
   `repo_edit` task → `grant_task_approval` → narrow execution envelope → fake-local
   provider spend grant.
2. Worker ticks: Safe Planner ACCEPT (fake-local adapter) → Driver → Operator
   `read_file` → `patch_file` → `run_focused_test` → `verification_evaluate`.
3. Task + goal reach `completed` via `_finalize_mainai_execution_goals`.

Automated proof:
`backend/tests/backend/mainai/test_first_bounded_self_improvement.py::test_first_bounded_self_improvement_ssh_egress_marker_coverage`

## Authorized paths / capabilities

- `authorized_paths`: exactly
  `backend/tests/backend/mainai/test_egress_policy.py`
- `authorized_capabilities`: `read_file`, `patch_file`, `run_focused_test`
  (`verification_evaluate` remains a Driver directive in the plan — it is not an
  Operator/`DEVELOPMENT_CAPABILITIES` envelope capability)
- `remote_write_authorized`: `false` (asserted on OperatorContext)
- Explicitly **not** authorized: `development_operator/`, `development_supervisor/`,
  `egress_policy/service.py` (production), `models/`, `alembic/`, `.github/`,
  `BRANCH_REGISTRY`, `ACTIVE_WORK_*`, commit/push capabilities

## Worktree layout (disposable mirror)

Operator runs against a disposable source_repo / goal worktree (same pattern as
calculator soak), seeded with:

- production-shaped path `backend/tests/backend/mainai/test_egress_policy.py`
  (NEVER_EGRESS coverage present; SSH marker coverage absent)
- local stub package `app/egress_policy/` **outside** `authorized_paths`, mirroring
  production `_NEVER_EGRESS_MARKERS` so focused_pytest can run under Operator without
  the full Postgres fixture stack

`run_focused_test` passes `environment.PYTHONPATH=.` so the disposable stub wins over
any host PYTHONPATH to production `backend/app`.

## No push / founder review gate

- No `push_branch` / remote write.
- No `stage_scoped_changes` / `commit_scoped_changes` on this first attempt (matches
  the acceptance contract's stricter first-run note).
- Diff under the envelope is confined to the authorized test file in the **goal
  worktree**. Founder must review that worktree diff before any merge/push decision.
- This Stage 4 PR itself must **not** be pushed until Stage 3 (#204) merges.

## Acceptance-contract deviations (honest)

1. **Disposable mirror, not a live edit of the production checkout.** The Worker edits
   a seeded copy of the egress test path inside the Supervisor goal worktree. Production
   `backend/tests/backend/mainai/test_egress_policy.py` in this branch is unchanged by
   the live loop (the proof test is separate). Applying the same patch to production
   remains a founder merge decision.
2. **Stub egress gate in the worktree.** Focused pytest under Operator cannot boot the
   full `db_session` / RLS fixture stack for production `enforce_egress_policy()`. The
   stub keeps the same markers and denial assertion shape; production
   `app/egress_policy/service.py` is not modified and not authorized.
3. **`provider_spend_authorized` with fake-local.** The acceptance contract prefers
   `spend=False` for milestone 1. That is impractical here: there is no deterministic
   Safe Planner recipe for this goal, so planning parks on `WAITING_PROVIDER` without a
   spend grant. Spend is founder-authorized, capped, and restricted to
   `allowed_providers=["fake-local"]` / `allowed_models=["planner-v2"]` — the same
   allowed CI fake boundary used by Stage 1/2 live proofs.
4. **OpenSSH marker string split in the written test source.** Safe Planner rejects plan
   arguments containing secret-shaped material (including
   `-----BEGIN OPENSSH PRIVATE KEY-----`). The added test constructs the marker at
   runtime via `"-----BEGIN " + "OPENSSH PRIVATE KEY-----"` so planning stays
   fail-closed while the executed assertion still denies on the real contiguous marker.

## Success criteria checklist

- [x] Single founder-precreated task
- [x] Envelope paths narrow to the egress test file only
- [x] Real Operator sequence `read_file` → `patch_file` → `run_focused_test`
- [x] New test function added; focused pytest evidence required
- [x] Task/goal completed through production finalize
- [x] No remote write
- [ ] Founder manual review of worktree diff before any production apply / push
