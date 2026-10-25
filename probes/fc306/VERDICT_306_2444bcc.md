# Independent examination — PR #306 (CF-2 correction + historical ledger repair)

- **Candidate SHA:** `2444bcc3da8ffa5ca10ef3295bac13aa045769c6`. Parent: #305 `50ca651` (verified ancestor).
- **Status:** INTERIM — **CF-2 independently confirmed CLOSED and no new blocking regression found**, but a final PASS/FREEZE is withheld pending (a) Cursor's explicit freeze of this exact SHA and (b) both required workflows green on it. Those two conditions were not confirmed to me; the user directed me to start now even if CI is running.
- **Delta over #305:** isolated to `backend/app/agent_coordination/persist.py` (+52) + a candidate test + docs. No other product change — so GH-4, the transport/delivery lifecycle, RAI, CR and the zod pin are inherited.
- **Examiner:** Claude, read-only; real app/worker/DB/gateway/persistence; no paid calls, no GitHub writes, no production change, no external activation; `first_cycle_go=false`. Worktree verified unchanged after every suite.

## Priority 1 — CF-2 closure: **independently CONFIRMED CLOSED**
I inspected the actual `persist.py` change (not Cursor's description). The fix redefines `_is_backing(row)` to return **`created_without_manual_assignment(row)` only** — a real work assignment is never backing even if a stray `ledger_backing` marker exists; `persist_owner_ledger` now stamps `LEDGER_BACKING_KEY` only when the backing row is genuinely synthetic (strips it otherwise); and the reconcile skip predicate keys on the same `created_without_manual_assignment`.

Re-ran my previously-failing probes on the exact SHA (`agentbridge_v2`, **14 passed, 1 skipped**):
| # | Check | Result @ 2444bcc | (control @ 50ca651) |
|---|---|---|---|
| CS-3 | existing real assignment NOT reclassified as synthetic backing | **PASS** `reclassified=False` | FAIL (CF-2 present) — test discriminates |
| CS-7 | containment across fresh session + reconcile; sibling clean; 0 synthetic rows for a warm owner | **PASS** `wa1_backing=False wa2=False backing_rows=0` | — |
| CS-1 | CF-1 still works for a zero-assignment owner (cold resolve after restart) | **PASS** `True` | — |
| CS-2 | cold-start token authenticates over HTTP | **PASS** `200` | — |
| CS-4 | revoke / expiry / replay / signature integrity | **PASS** | — |
| CS-5 | multiple legitimate assignments: identities preserved, exactly one backing row | **PASS** `backing_rows=1, all_resolve` | — |
| CS-6 | cross-owner credential isolation | **PASS** | — |
| ADV-1..8 | competing runners, interruption-resumable, revoked-mid-flight, dup-completion, no write-escalation, STOP-not-cleared, wrong-correlation; real-network BLOCKED | **PASS / BLOCKED(skip)** | — |

CF-2 is closed, CF-1 is intact, and legitimate assignments keep their identities, obligations and completion events across restart/reconcile/concurrent issuance.

## Priority 2 — historical repair (`repair_misclassified_backing`): **CORRECT** (`repair_v1`, 8 passed)
Tested against records contaminated exactly as CF-2 did (a REAL assignment stamped `ledger_backing` top-level AND inside the `durable_coordination` dump, alongside genuine history):
| RP | Property | Result |
|---|---|---|
| RP-1 | strips BOTH top-level and nested `ledger_backing` markers | **PASS** (`repaired=1`, both gone) |
| RP-2 | preserves genuine synthetic backing rows (`created_without_manual_assignment`) | **PASS** (`repaired=0`, still backing) |
| RP-3 | erases no unrelated provenance or historical evidence | **PASS** (unrelated key + dump obligations/delivery intact) |
| RP-4 | idempotent | **PASS** (`1, 0, 0`) |
| RP-5 | repeated repairs incl. a fresh-session reload don't corrupt | **PASS** (`1, 0, 0`, assignment intact, not backing) |
| RP-6 | missing/malformed provenance fails safely | **PASS** (empty `{}` ⇒ not backing; non-dict dump left intact; a NULL provenance is rejected by the DB CHECK `ck_agent_work_assignments_provenance_object` — a safety guarantee) |
| RP-7 | historical obligations/delivery/completion remain reconstructable | **PASS** (obligation present, status `completed` after repair+reload) |
| RP-8 | untrusted/incorrect `created_without_manual_assignment` marker | **HARDENING** (see below) |

### Finding HD-1 (hardening, not a reachable exploit on #306) — classification trusts one provenance boolean
`_is_backing` and `reconcile_owner`'s event-skip now key **solely** on `created_without_manual_assignment`, and `repair_misclassified_backing` deliberately does **not** demote a row carrying it (RP-8: `real_with_untrusted_marker_is_backing=True, repair_fixed_it=False`). So IF that marker were ever set on a real assignment by untrusted input, the assignment would be silently treated as synthetic and its lifecycle events skipped, and repair would not recover it. **Verified not reachable today:** the only writer of `created_without_manual_assignment` is `_create_backing_assignment` (grep across `app/`); no agent/message/receipt/HTTP path writes it, and `create_work_assignment`'s provenance is a Founder/coordination-supplied argument, never agent-controlled. **Severity: P3 hardening** — recommend corroborating the synthetic classification with `agent_key == LEDGER_AGENT_KEY` + `supports_write == False` (defense in depth) so a single stray/forged boolean cannot silently sideline a real assignment. Owner: Cursor/MainAI. Not a #306 blocker.

## Priority 3 — full integration contract preserved on #306 (no regression)
`gh_intake_v1` **8** (GH-4 idempotent + GH-4b survive-restart), `agentbridge_v2` **14/1** (CF-1/CF-2 + delivery lifecycle), `agentbridge_v1` **7** (transport negatives), `rai_v1` **24/3** (RAI-01..27), `cr_contract_v1` **19/2** (CR-01..20), `connect_v1` **5**, `delivery_v1` **6**, `lifecycle_v1` **7**, `substrate_v1` **20**, `repair_v1` **8**. Codex blockers preserved: `coord_accept_v1` CO-10 **FAIL** (1 failed/10 passed), `fa_core_v1` FAC-1..5 **FAIL**. **zod pin intact** (`"zod": "4.6.7"` present in `frontend/package.json`; #305 remediation carried forward). A local agent-side runner is NOT conflated with a connected Cursor/Claude/Codex vendor session (RAI/AT enforce this).

## Release classifications (kept separate)
| Scope | State | Basis |
|---|---|---|
| **A. Local agent-side bridge implementation** | **interim PASS — ready to freeze; final PASS/FREEZE pending freeze+CI** | CF-2 closed, CF-1 works, repair correct, no regression; one P3 hardening (HD-1), non-blocking |
| **B. Real external vendor connectivity** | **BLOCKED** | vendor transports fail-closed; no authenticated external receipt (RAI-01/13) |
| **C. Elimination of Founder manual forwarding** | **NO** | depends on B |
| **D. Unattended autonomy** | **BLOCKED** | Codex FA-9/CO-10 + FA-PRIV (preserved, still failing) |

## Verdict
**Interim: #306 is PASS-quality for scope A — CF-2 is independently confirmed closed, the historical repair is correct, and the full regression/acceptance battery is green with Codex blockers preserved.** I am **not** returning the old #305 CF-2 finding as a new instruction — this implementation genuinely fixes it. No new blocking regression was found; HD-1 is a P3 hardening note, not a blocker. **Final PASS/FREEZE will issue once Cursor confirms this exact SHA is frozen and both required workflows pass on it.** If a further code revision is published, the superseding SHA must be re-examined (affected scope: `persist.py` → `agentbridge_v2` CS-3/CS-7 + `repair_v1`). B/C/D remain BLOCKED/NO independent of A.

## Reproduce (all on 2444bcc)
```
probes/agentbridge_v2/run.sh ../wtcert-2444bcc   # CF-1 + CF-2(CS-3=False) + CS-7 + ADV: 14 passed, 1 skipped
probes/repair_v1/run.sh      ../wtcert-2444bcc   # RP-1..8 historical repair: 8 passed
probes/gh_intake_v1/run.sh   ../wtcert-2444bcc   # 8 passed (GH-4/GH-4b)
probes/rai_v1/run.sh         ../wtcert-2444bcc   # 24 passed, 3 skipped
probes/cr_contract_v1/run.sh ../wtcert-2444bcc   # 19 passed, 2 skipped
probes/agentbridge_v1/run.sh ../wtcert-2444bcc   # 7 passed (transport negatives)
probes/connect_v1/run.sh     ../wtcert-2444bcc   # 5 ; delivery_v1 6 ; lifecycle_v1 7 ; substrate_v1 20
probes/coord_accept_v1/run.sh ../wtcert-2444bcc  # CO-10 FAIL = preserved (Codex)
probes/fa_core_v1/run.sh     ../wtcert-2444bcc   # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
# CF-2 discrimination control: probes/agentbridge_v2/run.sh ../wtcert-50ca651 test_ab2.py -k CS3  -> FAILS (CF-2 present)
```
