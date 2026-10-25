# FINAL Scope-A certification — PR #306 (CF-2 correction + historical ledger repair)

- **Frozen product SHA:** `2444bcc3da8ffa5ca10ef3295bac13aa045769c6`. Freeze formally recorded via PR #308 (docs-only; no product-code change). Both required product-SHA workflows **12/12 green** (user-confirmed). Independently verified here: the product SHA is unchanged and the worktree is clean.
- **Basis:** my completed #306 interim examination at examiner commit `bfe36613` (not re-run — this finalizes it now that the freeze condition is fulfilled).
- **Examiner:** Claude, read-only; no live/paid calls, no GitHub writes, no production change. `first_cycle_go=false`.

## Verdict: **PASS / FREEZE (Scope A — local agent-side bridge)**
CF-2 is independently confirmed CLOSED and no blocking regression remains:
- **CF-2 CLOSED:** `agentbridge_v2` CS-3 `real_assignment_reclassified_backing=False` (the #305 control still FAILS — the test discriminates); `_is_backing` keys solely on `created_without_manual_assignment`; `persist_owner_ledger` stamps the backing flag only on a synthetic row.
- **CF-1 intact; cold-start, revoke/expiry/replay, concurrency, cross-owner** all green (CS-1/2/4/5/6).
- **Historical repair** `repair_misclassified_backing` correct (`repair_v1` 8/8): strips both markers, preserves synthetic rows, erases no history, idempotent, restart-safe, malformed-safe, reconstructable.
- **No regression:** gh_intake 8 (GH-4/GH-4b), RAI 24/3, cr_contract 19/2, connect 5, delivery 6, lifecycle 7, substrate 20, agentbridge_v1 7; Gate A `unattended_v1` 10/1.
- **HD-1** remains **P3 hardening** (synthetic-origin classification trusts one provenance boolean; not reachable via any application path — only via the Codex-owned FA-PRIV DB-write surface). Per instruction, not a #306 blocker.

## Separate classifications (unchanged, preserved BLOCKED)
- **Real external vendor connectivity:** BLOCKED.
- **Unattended autonomy:** BLOCKED — Codex FA-9/CO-10 + FA-PRIV (`coord_accept_v1` CO-10 FAIL, `fa_core_v1` FAC-1..5 FAIL).

## Scope
This certifies **#306's local agent-side bridge implementation only**. It does not certify external vendor connectivity, elimination of Founder forwarding with real agents, or unattended autonomy — those remain BLOCKED and are examined on the #307/#309 line. Reproduce: see `fc306/VERDICT_306_2444bcc.md` (the finalized interim evidence).
