# Independent Regression Gate — session-revocation + timestamp consistency — `460e8ad8` (Priority 9)

- **Subject:** a reusable regression gate requiring explicit session-revocation checks and
  consistent timestamp semantics across the three session-gated authority surfaces named in the
  assignment — **Founder authority**, **account erasure**, and **privileged effect
  authorization** — on candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`.
- **Suite:** `probes/fc_sessionreg_v1` — **3 passed, 3 xfailed** (`RESULTS_460e8ad.txt`),
  deterministic (run 3×). The 3 xfails are the *required* account-erasure behaviours, marked
  `xfail(strict=True)` against the confirmed Priority-8 defect. DB-backed via the target's own
  `tests.conftest`; drives the REAL reset / authority helpers on the exact SHA. Loopback-only;
  no paid calls; no production mutation; target worktree unchanged. `first_cycle_go=false`.

## The invariant under gate
> A session that any real revocation primitive has invalidated (refresh `revoked_at` set,
> `sessions_valid_after` bumped, or token expired) must NOT be treated as current by any
> session-gated authority helper, independent of sub-second truncation, using timestamp
> semantics consistent with the access-token `iat` check.

## How the gate is wired (so it enforces itself once fixed)
- **RG1–RG3 (account erasure, `xfail(strict=True)`):** assert the required rejection for
  `account_erasure_receipt_session_current` (0086). On the current build they fail → reported as
  **XFAIL**, so the gate is green while documenting the open defect. When the one-line fix lands
  (`AND rt.revoked_at IS NULL`, plus aligning the freshness comparison), the assertions start
  passing → strict xfail converts the **XPASS into a FAILURE** → the explicit signal to delete
  the marker and enforce the gate as a hard pass. No permanently-red bar in the examiner record,
  and no risk of the fix shipping without the gate being switched on.
- **RG4–RG6 (positive invariant locks, plain PASS):** pin the correct pattern the codebase
  already ships, so a future change cannot silently regress it.

## Findings

| ID | Surface | Requirement | State on `460e8ad8` |
|---|---|---|---|
| RG1 | account erasure | a same-second reset-revoked session is rejected by the erasure helper | **XFAIL** (defect: returns current) |
| RG2 | account erasure | an EXPIRED refresh session is rejected by the erasure helper | **XFAIL** (helper ignores `expires_at`) |
| RG3 | account erasure | a session with `revoked_at` set is rejected by the erasure helper | **XFAIL** (helper ignores `revoked_at`) |
| RG4 | Founder authority | `personal_recall_founder_authority_current` rejects the same-second reset-revoked session | PASS (already checks `revoked_at`) |
| RG5 | Founder authority | `personal_recall_founder_authority_current` rejects an EXPIRED session | PASS (already checks `expires_at`) |
| RG6 | privileged effect | `revoke_current_founder_sessions` refuses a revoked session | PASS (`live canonical Founder session required`) |

## Why this is grounded, not invented
The correct, consistent pattern is **already present** in the same codebase:
`personal_recall_founder_authority_current` (0083) and `revoke_current_founder_sessions` (0095)
both check `rt.revoked_at IS NULL AND rt.expires_at > now() AND rt.created_at >
u.sessions_valid_after`. `account_erasure_receipt_session_current` (0086) is the lone
session-gated helper that omits them. RG1–RG3 require the erasure helper to match the project's
own established semantics; RG4–RG6 lock those semantics in place.

## Scope / non-duplication
Envelope / spend / push transport effect authorization is STOP-epoch / grant-state gated — it
does **not** use the session-timestamp pattern — and its revoked-stays-revoked behaviour is
certified by `fc_effauth` / `fc_crossauth`; it is represented here only by the session-gated
privileged revocation effect (RG6) and is not otherwise re-tested.

## Scope / discipline
Read-only examiner; drove the real reset and authority helpers through the target's own conftest.
RECALL INACTIVE. No paid calls; loopback only; no publication/deployment/autonomy; no production
branch modified. Candidate materialised detached at the exact SHA and left unchanged.
`first_cycle_go=false`.

## Reproduce
```
probes/fc_sessionreg_v1/run.sh ../../wtcert-460e8ad      # 3 passed, 3 xfailed (deterministic)
```
