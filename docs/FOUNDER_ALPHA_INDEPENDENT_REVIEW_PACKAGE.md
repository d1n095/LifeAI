# Founder Alpha — Independent Review Package

**Exact head this package describes:** `fc2a5ebb89a546d6f729814f3f02889f91b6e713`
(branch `claude/founder-alpha-phase-f-release`, base `integration/founder-alpha-20261121`)

**What this document is not:** a certification. Every property below was implemented and/or
verified by the same Claude session that is now cataloging it. Per this repo's `CLAUDE.md`
("code I change myself cannot count as independently security-approved"), none of this
constitutes independent security sign-off. This package exists to make the claims *checkable*
by someone who was not in that session — a different agent, a human reviewer, or a future
session with no memory of this one — not to stand in for their review.

For each property: **Implementation** (exact file/function), **Test(s)** (exact file/test
name), **Reproduce it yourself** (exact command), **Status**.

**Before running any command below:** `backend/tests/conftest.py` defaults `DATABASE_URL`/
`APP_DATABASE_URL` to port `5433` (the docker-compose Postgres mapping). If you're running
against a bare system Postgres on the default port instead (as this phase's own verification
run did), export both explicitly first, e.g.:
```
export DATABASE_URL="postgresql://lifeos@localhost:5432/lifeos_test_$$"
export APP_DATABASE_URL="postgresql://mainai_app:mainai_app_pw@localhost:5432/lifeos_test_$$"
export REDIS_URL="redis://localhost:6379/5"
```
All reproduction commands below were run and confirmed passing this way against this exact
head before this document was committed — not just written and assumed correct.

---

## 1. Grant issuance (founder authorization → capability)

**Implementation:** `backend/app/routers/agents.py` — every mutating route in this router
(`dispatch_task_route`, `review_task_route`, `prepare_pr_route`, `attempt_merge_route`) is
gated by `Depends(require_founder)` (`backend/app/deps.py`) and threads the *live, authenticated*
`user.id` through as `authorized_by_user_id` into `backend/app/agent_orchestration.py`'s
`dispatch_task()` / `review_task()`. Those functions' own docstrings (`agent_orchestration.py:195`)
state the deliberate constraint: `authorized_by_user_id` must come from the real `require_founder`
dependency, never a hardcoded `FOUNDER_USER_ID` constant or any other resolved-without-a-live-session
value.

**Test(s):** `backend/tests/backend/mainai/test_agent_dispatch_authorization_boundary.py`,
`backend/tests/security/test_system_activation_authorization.py`.

**Reproduce:**
```
pytest backend/tests/backend/mainai/test_agent_dispatch_authorization_boundary.py backend/tests/backend/mainai/test_system_activation_authorization.py -v
```

**Status:** verified-in-this-session (Phase 4/D re-ran this against the real stack with a live
founder session; Phase B confirmed `authorized_by_user_id` threading survived R15/#468/#469
reconciliation).

---

## 2. Owner and tenant isolation (RLS)

**Implementation:** `backend/app/rls.py` — `apply_rls()` enables and FORCEs row-level security
on every tenant-scoped table (conversations, documents, messages, mainai_jobs, etc.) and installs
`CREATE POLICY` statements scoping each to the session's bound owner.

**Test(s):** `backend/tests/security/test_rls_isolation.py` plus the per-table suite
(`test_rls_isolation_cognition_foundation.py`, `test_rls_isolation_execution_envelopes.py`,
`test_rls_isolation_calendar_events.py`, `test_rls_isolation_work_candidates.py`,
`test_rls_isolation_project_entities.py`, `test_rls_isolation_corpus_trial_runs.py`,
`test_rls_isolation_candidate_learning_signals.py`,
`test_rls_isolation_github_evidence_candidates.py`), and the cross-tenant case specifically
exercised end-to-end in `test_chat_with_fallback_central_authorization_boundary.py::test_7_cross_tenant_attempt_is_denied_by_rls`.

**Reproduce:**
```
pytest backend/tests/security/ -k rls_isolation -v
pytest backend/tests/backend/providers/test_chat_with_fallback_central_authorization_boundary.py::test_7_cross_tenant_attempt_is_denied_by_rls -v
```

**Status:** verified-in-this-session (Phase 4 additionally exercised this against a real
Postgres instance with the restricted `mainai_app` role, confirming RLS holds for the actual
runtime role, not just a superuser test fixture).

---

## 3. Egress restrictions (central lease/local-only enforcement)

**Implementation:** `backend/app/providers/registry.py::chat_with_fallback()` (around line 336).
Two checks run before any provider is attempted, in order: (a) `verify_active_lease()` (see §6)
when the call carries worker-lease identifiers, (b) `settings.local_only_mode` provider-chain
filtering (see §4). Content sanitization/denial itself is `backend/app/egress_policy/service.py::enforce_egress_policy()`
— "the enforcement point: every outbound provider call this gate covers must pass its content
through this function before the provider is ever invoked."

**Test(s):** the full `test_chat_with_fallback_central_authorization_boundary.py` suite —
specifically `test_1_missing_invalid_authorization_denies_before_any_provider_call`,
`test_2_expired_or_wrong_worker_lease_denies_before_any_provider_call`,
`test_3_unauthorized_founder_wide_scope_is_denied`,
`test_4_never_egress_marker_denies_before_any_provider_call`,
`test_5_authorized_operation_succeeds_with_correct_disclosure_evidence`. Each spies on every
provider's `.chat()` to assert **zero** calls in the denied cases — not just that an error was
raised.

**Reproduce:**
```
pytest backend/tests/backend/providers/test_chat_with_fallback_central_authorization_boundary.py -v
```

**Status:** verified-in-this-session.

---

## 4. Local-only behavior (`LOCAL_ONLY_MODE`)

**Implementation:** `backend/app/config.py` (`local_only_mode: bool = False`) +
`backend/app/providers/registry.py:448-455` — when true, the provider chain is filtered to
`_LOCAL_PROVIDERS = frozenset({"ollama"})` *before* any attempt; an empty resulting chain raises
`ProviderError` immediately rather than falling through to an external provider.

**Test(s):** `test_chat_with_fallback_central_authorization_boundary.py::test_6_local_only_mode_never_falls_back_to_an_external_provider`,
`backend/tests/backend/test_config_contract.py`.

**Reproduce:**
```
pytest backend/tests/backend/providers/test_chat_with_fallback_central_authorization_boundary.py::test_6_local_only_mode_never_falls_back_to_an_external_provider backend/tests/backend/test_config_contract.py -v
```

**Status:** verified-in-this-session. **Gap for reviewer:** Phase C/D confirmed this sandbox has
no outbound network to actually reach a running Ollama instance, so the *negative* path (chain
correctly empties / raises) is tested, but a real successful local-model call under
`LOCAL_ONLY_MODE=true` has not been exercised end-to-end in this integration effort — only under
mocked providers. A reviewer with real Ollama infrastructure should confirm the positive path too.

---

## 5. Proposal workspace isolation

**Correction to the task brief this phase was dispatched under:** the brief named
`_create_proposal_workspace()` (a plain filesystem copy, built in PR #469). That function is
confirmed **absent** from this head (verified during Phase A's reconciliation) — it was
superseded during R15 reconciliation by a stronger, independently-built mechanism that is the
one actually live today:

**Implementation:** `backend/app/mainai_execution/proposal_worktree.py::create_isolated_proposal_checkout()`,
called from `_propose_repo_edit()` in `backend/app/mainai_execution/execution_job.py` (see its
"Reconciliation note (PI-1d merge)" docstring, `execution_job.py:309-317`, for why this version
was kept over the filesystem-copy one). It builds a bare git repo from a local-path fetch of the
shared checkout's HEAD — no remote, no inherited credentials — then a `git worktree add` from
that bare repo. `assert_no_credentialed_remote()` re-verifies no credentialed remote URL leaked in.
Path-traversal and symlink-escape guards are re-applied against the isolated root. Degrades to the
shared checkout + PI-1d rollback if isolation can't be built (`work_result["isolation"]` records
which mode actually ran — never silently claims isolation that didn't happen).

**Test(s):** `backend/tests/backend/test_proposal_worktree_isolation.py` (bootstrap/credential/
fetch-failure behavior), `backend/tests/security/test_proposal_isolation_adversarial.py`
(`test_a_malicious_attempt_id_never_becomes_a_path`,
`test_a_symlink_inside_the_proposal_tree_cannot_be_used_to_escape`,
`test_a_credentialed_remote_is_refused`), `backend/tests/backend/test_proposal_mode_checkout_restoration.py`
(fallback-path rollback behavior when isolation can't be built).

**Reproduce:**
```
pytest backend/tests/backend/test_proposal_worktree_isolation.py backend/tests/security/test_proposal_isolation_adversarial.py backend/tests/backend/test_proposal_mode_checkout_restoration.py -v
```

**Status:** verified-in-this-session.

---

## 6. Worker lease fencing

**Implementation:** `backend/app/jobs/mainai_job_lease.py::verify_active_lease()` — a pure,
read-only check (never mutates) confirming a job is `status='running'`, `locked_by=worker_id`,
`lease_generation` matches exactly, and `lease_expires_at` is still in the future. Called from
`chat_with_fallback()` so the egress gate can confirm a lease is genuinely still held without
itself claiming/renewing it.

**Test(s):** `test_chat_with_fallback_central_authorization_boundary.py::test_2_expired_or_wrong_worker_lease_denies_before_any_provider_call`;
lease-generation fencing more broadly in `backend/tests/backend/mainai/test_recovery_takeover_authority_fencing.py`
and `backend/tests/backend/mainai/test_operator_lease_effect_time_race.py`.

**Reproduce:**
```
pytest backend/tests/backend/providers/test_chat_with_fallback_central_authorization_boundary.py::test_2_expired_or_wrong_worker_lease_denies_before_any_provider_call backend/tests/backend/mainai/test_recovery_takeover_authority_fencing.py backend/tests/backend/mainai/test_operator_lease_effect_time_race.py -v
```

**Status:** verified-in-this-session.

---

## 7. Revocation

**Implementation:** no single function — revocation is enforced by the *same* lease/envelope
checks used for normal authorization, which treat a revoked grant identically to an
expired/mismatched one. `recovery_takeover.py`'s governance check and
`mainai_job_lease.py::verify_active_lease()` both read current state, so a revoked lease or a
revoked `ExecutionAuthorizationEnvelope` cannot authorize further work without any special-cased
"is this revoked" branch to audit separately — the fail-closed default *is* the revocation
enforcement.

**Test(s):** `test_recovery_takeover_authority_fencing.py::test_takeover_still_declines_once_the_active_envelope_was_revoked_with_no_replacement`,
`::test_goal_has_ever_been_envelope_governed_stays_true_after_the_only_envelope_is_revoked`,
`backend/tests/backend/mainai/test_spend_revoke_before_reserve.py`.

**Reproduce:**
```
pytest backend/tests/backend/mainai/test_recovery_takeover_authority_fencing.py -k revoked -v
pytest backend/tests/backend/mainai/test_spend_revoke_before_reserve.py -v
```

**Status:** verified-in-this-session. **Gap for reviewer:** there is no test exercising
revocation of an in-flight *provider* call specifically (i.e., a lease revoked in the window
between `verify_active_lease()` passing and the provider response returning) — only
revoked-before-the-call is covered. This is a real gap, not a false claim of coverage; flagging
for a reviewer or a future phase rather than inventing a fix outside this phase's scope.

---

## 8. Database privileges (SFA-1 and related)

**Implementation:** `backend/scripts/security/s1a_privilege_policy.py`. SFA-1 specifically:
`_MIGRATION_STATE_TABLES = ["alembic_version"]` (line 256) — `REVOKE ALL ON TABLE
public."alembic_version" FROM mainai_app` runs unconditionally every boot (line 519-524), after
which the runtime role has zero privileges on that table (not SELECT-only, which was the
superseded, independently-reintroduced R15 entry removed during this integration — see the
integration manifest's SFA-1 entry).

**Test(s):** `backend/tests/backend/test_migration_state_privileges.py` — all 7 tests, notably
`test_policy_revokes_every_privilege_on_alembic_version_from_the_runtime_role`,
`test_runtime_role_is_refused_by_postgres_itself` (parametrized over actual SQL statements
attempted as the restricted role), `test_alembic_upgrade_head_still_runs_after_the_revocation`
(confirms the migration tool itself uses a different, privileged role — the revocation doesn't
break migrations).

**Reproduce:**
```
pytest backend/tests/backend/test_migration_state_privileges.py -v
```

**Status:** verified-in-this-session (confirmed against a real Postgres instance during
reconciliation, not just SQL-string assertions).

---

## 9. Secret handling

**Implementation:** no single module — this is a property of the whole request/logging path,
verified operationally rather than by a single enforcement point. Phase F generated real login
traffic against a freshly-installed stack and grepped the actual log output for leaked
secrets/tokens; none were found (see `fc2a5ebb`'s commit message).

**Reproduce (what Phase F actually ran — repeat it yourself rather than trusting the summary):**
```
# Against a running instance with real credentials configured:
curl -s -c /tmp/cookies.txt -X POST http://localhost:8000/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"<test-user>","password":"<test-password>"}' -D -
# Then inspect whatever log sink the deployment uses:
grep -niE 'password|secret|token|bearer|api[_-]?key' <backend-log-output> | grep -v '"password":"\[REDACTED\]"'  # adjust to the actual redaction marker in use
```

**Status:** verified-in-this-session, but **this is the weakest-evidenced property in this
package** — it was a one-time manual grep of one session's traffic, not an automated test that
runs in CI and fails the build on a future regression. **Gap for reviewer:** there is no
committed, repeatable automated test asserting "no secret-shaped string ever appears in a log
line" — a reviewer should treat Phase F's finding as a spot-check, not a guarantee, and consider
whether this warrants an actual CI-enforced test.

---

## 10. Recovery behavior

**Implementation:** spread across `backend/app/mainai_execution/recovery_takeover.py`,
`final_report.py`, `auto_recovery.py`, and `backend/app/jobs/mainai_job_lease.py`. Phase E (this
integration effort) specifically closed two real gaps: session-expiry fail-closed behavior
(`backend/app/deps.py` / session auth path) and a Redis-outage retry loop in
`backend/app/worker.py`.

**Test(s):** `backend/tests/backend/test_database_recovery.py` (13 tests — fresh-DB migration
apply, upgrade-from-prior-state, privilege re-application after restore),
`backend/tests/backend/mainai/test_recovery_takeover_authority_fencing.py`,
`backend/tests/security/test_session_auth.py::test_expired_access_token_is_rejected_cleanly_not_with_a_500`
and `::test_refresh_issues_an_access_token_that_actually_works` (Phase E additions),
`backend/tests/backend/jobs/test_worker.py` (Redis-blip retry test, Phase E addition),
`backend/tests/founder_acceptance/test_founder_acceptance_suite.py` for the full-journey
cancellation/recovery scenarios.

**Reproduce:**
```
pytest backend/tests/backend/test_database_recovery.py backend/tests/backend/mainai/test_recovery_takeover_authority_fencing.py backend/tests/security/test_session_auth.py backend/tests/backend/jobs/test_worker.py backend/tests/founder_acceptance/ -v
```

**Status:** verified-in-this-session, including against a real restored Postgres instance
(Phase F's backup/restore cycle) and real crash/restart conditions (Phase D/E), not only mocked
unit tests.

---

## Explicitly open, not resolved by this package

- **Task #61 — founder-wide (non-goal-scoped) authorization primitive.** `lesson_conflicts.py::detect_conflict()`
  fails closed unconditionally because `EngineeringLesson` is ownerless/founder-wide and no
  existing authorization primitive fits it cleanly (no founder-wide equivalent of
  `ExecutionAuthorizationEnvelope`, which requires a non-null `goal_id`). This package does
  **not** propose or activate a new founder-wide privilege to close this gap — it remains a real
  open architecture question for the founder to decide, and the fail-closed default is the
  correct current state, not a placeholder to be "fixed" by inventing scope here.
- **Independent third-party review.** Nothing in this session's work — including this
  package — constitutes the independent review it is meant to enable. That needs to come from
  outside this session (the founder directly, or another agent such as Freebuff/Z.ai).
- **Cosmetic defang-nesting rendering issue** in `untrusted_content.py` (flagged during Phase 3,
  not a security defect — containment holds, only the rendering of a nested forged marker looks
  odd).
- **§3/§7/§9 gaps above** (real-model positive path under `LOCAL_ONLY_MODE`, in-flight
  revocation race, secret-handling as a repeatable automated test rather than a one-time grep)
  are the most actionable next items for a reviewer or a future phase.
