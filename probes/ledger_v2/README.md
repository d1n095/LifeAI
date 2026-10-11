# Orchestration Truth Ledger verification suite (examiner-owned)

Prepared 2026-10-10. Not builder code; never committed.

## Status of record
- Branch `cursor/mainai-orchestration-truth-ledger` remote tip **still 32442758a4a575931c684e959f368e649fb8314b**
  (PR #253, last updated 2026-09-29). No remediation branch/PR exists; only this branch contains
  `backend/app/mainai_orchestration_ledger/`. Last verdict (2026-10-01): **RETURN_TO_BUILDER**. Unchanged code → no new verdict.
- A verdict on a new SHA is issued only after an explicit examination assignment.

## Run
```
./run.sh <target-worktree>        # e.g. ../../wtled2 (detached at 3244275 = negative control)
```
Pure-logic tests need nothing. DB tests load the TARGET's own `tests.conftest` as a pytest plugin
(fixtures `db_session` = real `mainai_app` role with startup privilege narrowing, `superuser_db`,
`make_verified_user`) against local Postgres :5433, from OUTSIDE the target tree; `run.sh` verifies the
target worktree is unchanged afterwards.

Probe-integrity guard: a DB attack that errors for a reason other than a security denial
(privilege/RLS/trigger/constraint) or that matches 0 rows FAILS as "PROBE ERROR" instead of passing.

## Test plan
| Area | Tests | Secure property |
|---|---|---|
| GitHub evidence / exact SHA | `test_snapshot_of_other_branch…`, `test_remote_pushed_requires_output_sha…`, `test_stale_snapshot…`, `test_ci_success_is_bound…`, `test_pytest_evidence_must_name…`, DB `test_remote_pushed_cannot_be_set_without_a_matching_github_snapshot` | remote/CI/test truth binds to branch + exact head SHA + freshness; DB enforces it |
| Duplicate jobs / slots / leases | `test_agent_marked_idle_with_running_task…`, `test_create_new_slot_respects_max_slots`, DB slot tests, `test_slots_have_lease_expiry_and_session_identity` | one job per agent/slot; max_slots; no freeing a running slot; durable lease/heartbeat/fencing |
| Frozen candidate / examiner isolation | `test_frozen_branch_cannot_be_modified_by_any_role[*]`, `test_frozen_rows_recorded_in_ledger…`, DB `test_frozen_candidate_row_cannot_be_unfrozen`, `test_examiner_is_not_the_builder…` | every role; ledger-recorded frozen rows; immutable frozen flag; independent examiner |
| Dependencies | `test_dependency_cycle_is_detected_before_assignment`, `test_unfinished_dependency_blocks_assignment`, DB `test_dependency_cycle_rejected` | cycles refused; failed/unfinished deps block |
| Immutable evidence / provenance | DB `test_historical_test_evidence_is_not_rewritable`, `test_evidence_is_append_only[*]` | claims/snapshots/test evidence append-only |
| Identity / self-cert / founder | `test_agent_claim_never_sets_remote_pushed`, `test_examiner_pass_text_never_certifies`, `test_founder_alpha_regression_scenario`, `test_other_owner_sees_nothing…` | agent text ≠ authority; no founder relay; RLS isolation |

## Baseline on 3244275 (negative control, 2026-10-10): 23 failed / 13 passed
Passes are only properties that genuinely hold (agent claim non-authoritative, running non-parallel
agent refused, hard-coded frozen branch vs builder, examiner ≠ builder request, examiner text not
certification, Founder Alpha regression, 5 append-only evidence denials, cross-owner RLS) + 1 INFO.
New finding while building: planner never checks `depends_on` → assignment with a FAILED dependency is planned.
