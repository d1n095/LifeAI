# Examination — PR #305 (zod dependency remediation; inherits #304 transport + CF-1)

- **Examined SHA:** `50ca651de9648afaea31273521bba01d82aeace7`. Base: #304 `859f5ec` (verified ancestor). Both exact-SHA CI workflows reported 12/12 (user-verified; not re-readable here — no GitHub credentials).
- **Delta over #304 `859f5ec`:** product change is ONLY the frontend zod pin — `frontend/package.json` (overrides) + `package-lock.json`; the rest is docs. **No backend/transport change**, so CF-1 and CF-2 are inherited byte-identical.
- **Examiner:** Claude, read-only; real app/worker/DB/gateway/persistence; no paid calls, no GitHub writes, no production change, no external activation; `first_cycle_go=false`. Worktree verified unchanged after every suite.
- **Standing distinction (requirement 7):** **Code PASS ≠ CI PASS ≠ independent certification ≠ real vendor connectivity.** #305's CI is green and its zod delta is sound, but CF-2 remains an open code finding, so #305 is NOT a complete integration candidate; and vendor connectivity remains BLOCKED regardless of either.

## 1. Dependency-security delta — **VERIFIED SOUND, audit policy not weakened**
- The pin adds `"zod": "4.6.7"` to the frontend `overrides` block, forcing transitive zod `4.4.3 → 4.6.7` (lockfile confirms).
- **osv.dev (2026-10-24):** zod `4.4.3` **is** affected by `GHSA-mrhx-c7xc-m3w5`; zod `4.6.7` has **no** known vulns. The pin genuinely remediates the advisory.
- **No change** to `frontend/scripts/check-npm-audit.js`, the audit-policy scripts, or `.github/workflows/ci.yml` in the delta — the fix is a real version bump, **not** a suppression/allowlist/audit-level downgrade. Policy intact (production findings still fail; the dated-advisory exception mechanism is untouched).
- Scope hygiene: the fix is isolated to the dependency, matching the repo's own "unrelated advisory → its own change" principle.

## 2. CF-1 / CF-2 on the exact #305 SHA (re-run, no seeding)
`agentbridge_v2` → **14 passed, 1 skipped** on `50ca651`:
- **CF-1 FIXED (inherited):** CS-1 cold resolve after restart `True`; CS-2 HTTP `200`; CS-4 revoke/expiry/replay intact; CS-5 exactly one backing row + all concurrent issuances resolve; CS-6 cross-owner isolated.
- **CF-2 PRESENT (inherited) — RETURN_TO_BUILDER:** CS-3 `real_assignment_reclassified_backing=True`. `persist_delivery → persist_owner_ledger` sets `LEDGER_BACKING_KEY` on `ensure_owner_ledger_backing`'s return, which for a warm owner is `rows[0]`, a **real** assignment whose provenance has `created_without_manual_assignment=None` — i.e. a genuine work assignment is labeled synthetic ledger backing. **Existing-assignment identity/provenance invariant VIOLATED** (the invariant #303 `74ebda9` established).
- **ADV-1..8:** competing runners → one delivered/one empty; worker interruption resumable; revoked-mid-flight → 403; duplicate completion after restart `replay_ignored`; read-only no write-escalation; STOP not cleared by coordination (Codex CO-10 BLOCKED); wrong correlation `forged_rejected`; real network interruption BLOCKED (skipped).

## 5. Safe handling of a misclassified real assignment (reload / restart / reconcile) — **CONTAINED**
`agentbridge_v2` CS-7 (clean single-/fresh-session, re-query by id): after CF-2 flags `rows[0]`:
- the obligation **survives** reload + lifecycle reconcile and reaches `status=completed` (**no coordination data loss** — hence CF-2 is **P3**, not P2);
- the error is **not compounded**: `backing_rows=1` (only `rows[0]`), and the **sibling** real assignment is NOT retroactively misclassified (`wa2_sibling_misclassified=False`);
- a severity probe confirmed a post-retag completion transition still reconciles to `completed`.
CF-2 is therefore a bounded invariant/classification defect (a real assignment carries a `ledger_backing` dump and reads as backing to `_is_backing`), not a data-loss bug — but it must still be fixed before #304/#305 can be a complete integration candidate.

## 3 + 4. CF-2 as an explicit RETURN_TO_BUILDER finding + ready regression for the next correction
| ID | Sev | Invariant | Fix (Cursor; do not duplicate) | Ready regression |
|---|---|---|---|---|
| **CF-2** | **P3, RETURN_TO_BUILDER** | an existing real work assignment must never be reclassified as synthetic ledger backing; its identity/provenance must not gain `ledger_backing`/a `durable_coordination` dump as if it were the synthetic storage row | in `persist_owner_ledger`, set `LEDGER_BACKING_KEY` **only** when the backing row is the synthetic one (`provenance.created_without_manual_assignment` is True), never when it reused a real `rows[0]`; equivalently, `ensure_owner_ledger_backing` should create/designate a dedicated synthetic row rather than returning a real `rows[0]` as the ledger holder | `agentbridge_v2` CS-3 (flips from recording the defect to locking the fix: invert to assert `reclassified is False`), CS-7 (containment), plus the `_is_backing` single-session diagnostic |

The CS-3/CS-7 cases apply immediately to Cursor's next correction candidate: when CF-2 is fixed, CS-3's `real_assignment_reclassified_backing` becomes `False` (its assertion message tells the operator to invert and lock), and CS-7 continues to prove containment and no sibling spread.

## 6. RAI + CR contract currency on #305 (no regression)
RAI-01..27 **24 passed, 3 skipped (BLOCKED)**; cr_contract CR-01..20 **19 passed, 2 skipped**; gh_intake **8 passed** (GH-4 idempotent holds). Codex blockers preserved: `coord_accept_v1` CO-10 **FAIL** (1 failed/10 passed), `fa_core_v1` FAC-1..5 **FAIL**. (connect/delivery/lifecycle/substrate unchanged from `859f5ec` — identical backend — re-verified there.)

## 7. The four distinctions, explicit for #305
- **Code PASS:** NO — CF-2 is an open RETURN_TO_BUILDER finding (the zod delta itself is clean).
- **CI PASS:** YES — both workflows 12/12 (zod advisory remediated; npm audit now green).
- **Independent certification:** NO — not a complete integration candidate while CF-2 is open; and green CI is not certification.
- **Real vendor connectivity:** NO / BLOCKED — vendor transports all fail-closed; no authenticated external receipt (RAI-01/13 BLOCKED).

## Verdict for #305
- **Dependency/security remediation (the stated purpose of #305): PASS** — zod advisory genuinely fixed, audit policy intact, CI green.
- **As the complete agent-side integration candidate: RETURN_TO_BUILDER** — CF-2 (P3) remains unresolved in the inherited source. Per instruction, #305 is **not certified as the complete integration candidate** while CF-2 stands.
- Vendor connectivity (B) BLOCKED; eliminate-forwarding (C) NO; unattended autonomy (D) BLOCKED (Codex FA-9/CO-10 + FA-PRIV).

When Cursor publishes the isolated CF-2 correction with both workflows green, I will examine that exact SHA and issue the final verdict (CS-3 must flip; CS-7 must stay green; RAI/CR/gh_intake/preservation unchanged).

## Reproduce
```
probes/agentbridge_v2/run.sh ../wtcert-50ca651   # CS-1..7 (CF-1 fixed; CS-3 records CF-2; CS-7 containment) + ADV: 14 passed, 1 skipped
probes/rai_v1/run.sh         ../wtcert-50ca651   # 24 passed, 3 skipped (BLOCKED)
probes/cr_contract_v1/run.sh ../wtcert-50ca651   # 19 passed, 2 skipped
probes/gh_intake_v1/run.sh   ../wtcert-50ca651   # 8 passed (GH-4 idempotent)
probes/coord_accept_v1/run.sh ../wtcert-50ca651  # CO-10 FAIL = preserved (Codex)
probes/fa_core_v1/run.sh     ../wtcert-50ca651   # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
# zod advisory: osv.dev confirms 4.4.3 affected by GHSA-mrhx-c7xc-m3w5, 4.6.7 clean
```
