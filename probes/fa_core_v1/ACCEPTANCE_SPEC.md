# Independent acceptance specification — Codex Founder Authority & Capability Management Core

- **Target (declared):** branch `codex/founder-authority-capability-core`, commit `e68e7f4affc961e1536850cbe52d303c4775ee37`, tree `6208f6efeb5605442fe03f2201ed631f882b264b`, base `a2dcad1c9f1aaa2107ea377864d8db45ba312c77`, migration head `0094_founder_authority_core`, bundle SHA-256 `9c1b8848…13c8`.
- **Access status (2026-10-22):** **NOT ACCESSIBLE.** GitHub returns 422 for commit `e68e7f4` and base `a2dcad1`; the branch is "not found"; the objects are not in the examiner store; **no bundle is attached to this conversation**. Per the protocol I have not examined any of this code and make no claim about its contents. This document and the harness are the preparation deliverable.

## Artifact dependency (what the Founder must attach)

The Git bundle file from the handoff package, attached to this conversation (or placed in a session-readable path and named here). I will not read the Founder's private Codex workspace. On receipt:

```
probes/fa_core_v1/verify_bundle.sh <path-to.bundle>     # SHA-256, bundle verify, exact commit+tree, ancestry, single head
# then import into an isolated store, add a detached worktree at e68e7f4, build the frontend, and run:
probes/security_kernel_v1/run.sh  <worktree>            # FA-1..FA-16  (model-independent enforcement)
probes/fa_core_v1/run.sh          <worktree>            # FAC-1..FAC-7 (new core + legacy regression)
TARGET_BACKEND=<worktree>/backend probes/fa_core_v1/run.sh <worktree> test_fac_core.py -k FAC6
probes/certify_v1/intake.sh       e68e7f4…             # full combined profile from the bundle (2nd arg = bundle path)
```

## Requirement → check map

| Assignment item | Check(s) | PASS criterion |
|---|---|---|
| 1 Verify bundle, SHA, tree, ancestry, checksums, single head | `verify_bundle.sh`; FAC-6 | SHA-256 matches; `git bundle verify` ok; commit present; tree == `6208f6e`; base is an ancestor; single head `0094_founder_authority_core` |
| 2 Capability policies, immutable autonomy ceilings, signed effective policies, step-up, one-use grants | FAC-2, FAC-3; FA-3 (step-up); GC-1..GC-8 via `grant_contract_v1` adapter (one-use, race, expiry, cancel, exact binding, distinct effects, no re-issue, typed denial) | new authority tables not runtime-writable; ceiling immutable or guard-triggered; step-up present for sensitive changes; grants pass GC-1..8 |
| 3 Escalation, policy forgery, replay, approval laundering, lockout, customer→Founder inheritance | FA-4..FA-10, FA-13; FAC-2; new customer-isolation case (to add on sight of the tenancy model) | every tamper attempt as `mainai_app` denied; approval not forgeable or replayable; no customer identity inherits Founder authority |
| 4 Model-generated content cannot modify its own authority or impersonate the Founder | FA-5, FA-10, FA-11; FAC-1, FAC-2 | runtime role cannot self-grant, re-key the Founder, or satisfy a Founder gate; role-only `founder` impostor refused |
| 5 Signing keys, policy records, audit receipts protected independently of the runtime role | FAC-1, FAC-4; FA-12 | signing keys not SELECT/UPDATE/DELETE-able by `mainai_app`; audit/receipt insert-only (guard trigger) |
| 6 Legacy tables still writable by `mainai_app`? | **FAC-5** (FA-PRIV regression) | `users`, `audit_log`, `workforce_authority_epoch`, envelope, spend, `mainai_goals` no longer runtime-writable. **A correct new core with legacy tables still writable is NOT a pass.** |
| 7 Append-only audit, STOP integrity, revocation, installation proposal, rollback | FAC-4; FA-9, FA-12, FA-14, FA-15; new install-proposal case | audit append-only; STOP not runtime-clearable; revoke/rollback present; privileged changes leave receipts; a sensitive install requires a Founder proposal+approval |
| 8 The 20 initially-failing tests and their reruns | run the candidate's **full backend suite once, uninterrupted**, capture pass/fail/xfail counts, and separately list any test run individually; **report the single-run result, never a stitched-together green** | I report the genuine uninterrupted full-suite outcome at the exact SHA and name any test that only passes in isolation |
| 9 Migration compat, erasure, isolation, Recall identity | FAC-6, FAC-7; `migration_integrity_v1`; account-erasure (ER-4) and RLS suites from the combined profile | single head; erasure still works and covers the new tables; RLS intact; Recall unmounted and not re-keyed (FAC-7) |
| 10 Exact-SHA verdict distinguishing new vs legacy defects | this spec + the run logs | verdict labels each finding **NEW (introduced by the core)** or **LEGACY (pre-existing Security Kernel, Codex-owned hardening)** |

## Reporting discipline (item 8, explicit)

I will not present the "20 initially failing, then individually rerun" set as a clean full-suite pass. My verdict will state: the counts from **one** uninterrupted `pytest` run at `e68e7f4`; which tests failed in that single run; and, separately, any test that passes only when rerun alone (a test-isolation dependency is itself a finding, not a pass).

## Calibration (done, on the latest accessible tree #295 `97e2b04`, which lacks the core)

Evidence: `calibration_97e2b04.txt`. The suite behaves correctly on a tree **without** the core:
- FAC-1/FAC-2/FAC-3 → **BLOCKED** (no signing-key / authority-core / autonomy-ceiling tables) — these must turn PASS on the real candidate.
- FAC-5 → **FAIL**, reproducing the exact `security_kernel_v1` FA-PRIV legacy finding (`users`, `audit_log`, `workforce_authority_epoch`, envelope, spend, `mainai_goals` runtime-writable). This must turn PASS for item 6.
- FAC-4 → **FAIL** on `audit_log` / `mainai_supervision_receipts` (rewritable, no guard); `account_erasure_reauth_receipts` already has guard triggers.
- FAC-7 → **PASS** (Recall unmounted, activation off).

So a green `fa_core_v1` run is only achievable on a candidate that both adds the protected core **and** closes the legacy surface — which is exactly item 6's requirement.
