# Interim examination — PR #304 revised candidate (Agent-Side Communication Bridge)

- **Candidate SHA (MOVING, not frozen):** `859f5ec5bf0eea4662b43caa061ff67967bba3aa`. Prior candidate `4102c167` (superseded). Baseline: certified #303 `74ebda9`.
- **Status:** INTERIM — no final verdict on a moving candidate. CI: product tests reported passing; `Frontend — npm audit` remains red (transitive-`zod` `GHSA-mrhx-c7xc-m3w5`, Cursor-owned). My final #304 obligation stays open.
- **Delta over `4102c167`:** one commit — `delivery.py` `persist_delivery` now delegates to `persist_owner_ledger` (the CF-1 fix) + test + docs.
- **Examiner:** Claude, read-only; real app/worker/DB/gateway/transport/persistence; no paid calls, no GitHub writes, no production change, no external activation; `first_cycle_go=false`. Worktree verified unchanged after every suite.

## Priority 1 — CF-1 independent regression verdict: **FIXED**, with one new P3 defect (CF-2)
I verified the actual new code (not Cursor's description): `persist_delivery` now calls `persist_owner_ledger`, the cold-start-capable path that creates an owner-scoped backing row when none exists. Re-ran my previously-failing CS scenarios on `859f5ec` **without creating a prior assignment**:

| Check | Result @ 859f5ec | 
|---|---|
| CS-1 cold owner: credential persists + resolves after fresh DB session | **PASS** `resolve_cold_after_restart=True` (was False on 4102c16) |
| CS-2 cold owner: token authenticates over HTTP after restart | **PASS** `inbox=200` (was 403) |
| CS-4 revocation + expiry + replay/signature integrity | **PASS** |
| CS-5 concurrent issuance (cursor+claude+codex) → exactly ONE backing row, none lost | **PASS** `backing_rows=1, all_resolve=True` |
| CS-6 cross-owner credential isolation | **PASS** (owner-2 token leaks 0 of owner-1's inbox) |
| CS-3 existing real assignment never reclassified as synthetic backing | **FAIL → CF-2 (P3)** `real_assignment_reclassified_backing=True` |

**CF-1 is genuinely closed.** The earlier RAI-06 seed did mask a real limitation, now fixed.

### Finding CF-2 (P3) — the CF-1 fix reintroduces the real-assignment reclassification invariant violation
`persist_owner_ledger` sets `LEDGER_BACKING_KEY=True` on whatever `ensure_owner_ledger_backing` returns. For a **warm** owner (real assignments, no synthetic backing row) that is `rows[0]` — a **real** work assignment. Routing `persist_delivery` → `persist_owner_ledger` (the CF-1 fix) now reaches this for warm owners during bridge issuance, so a real assignment is flagged `ledger_backing`. This violates the exact invariant #303 `74ebda9` established ("existing legitimate work assignments are never reclassified as synthetic backing").

**Severity P3, not P2:** I probed the downstream consequence — after the retag, a real builder transitioned to `completed` still reconciles to `status=completed` (the obligation is reconstructed from the persisted dump), so **no coordination data loss was demonstrated**. The defect is a correctness/invariant violation: a real assignment is mislabeled synthetic ledger backing (carrying a `durable_coordination` dump), which `reconcile_owner` skips for event re-ingestion and which any `_is_backing`-based classification (Founder-ops views, cleanup, "real vs backing" counts) would misread. **Smallest fix (Cursor, do not duplicate):** `persist_owner_ledger` should set `LEDGER_BACKING_KEY` only when the backing row is the SYNTHETIC one (`created_without_manual_assignment`), never when it reused an existing real `rows[0]`. Reproduction: `agentbridge_v2` CS-3 (asserts the current defective state; flips to catch the fix).

## Priority 2 — expanded adversarial suite (`agentbridge_v2`, 13 passed, 1 skipped)
Real gateway/transport/persistence boundaries; vendor-dependent checks kept BLOCKED.
| ADV | Scenario | Result |
|---|---|---|
| ADV-1 | two runners (same token) race for one message | **PASS** — exactly one `delivered`, the other `empty`; the assignment is never delivered as two work items |
| ADV-2 | worker interruption between claim/ack and completion | **PASS** — fresh session shows `acknowledged` (not fabricated complete, not lost); resumable |
| ADV-3 | authorization revoked mid-flight | **PASS** — before=200, after-revoke=403 |
| ADV-4 | duplicate completion receipt after restart | **PASS** — `replay_ignored` |
| ADV-5 | read-only execution escalating to repo write via message content | **PASS** — executor `write_scope=none`, content never interpolated |
| ADV-6 | Founder STOP while work pending/executing | **PASS** — `override_stop` event `stale_rejected` (authenticated STOP-clear = Codex CO-10, BLOCKED) |
| ADV-7 | wrong assignment↔result correlation | **PASS** — receipt for a foreign `message_id` `forged_rejected` |
| ADV-8 | network interruption/recovery against a REAL agent | **BLOCKED** (skipped) — no real external transport; substitutes covered by ADV-2 / no-live-bridge |
Plus the #304 transport negatives from `agentbridge_v1` AT-1..AT-7 (vendor fail-closed, no simulation, runner scoped) still hold on 859f5ec.

Forged vendor identity/capability (AT-1/RAI-04), malicious shell/injection (AT-4/RAI-17), missing/inconsistent evidence (RAI-03/AT-4/ADV-7), duplicate events (RAI-11/12/ADV-4), cross-owner (RAI-07/CS-6) — all covered and passing.

## Priority 4 — RAI-01..27 and regression on 859f5ec (no regression)
RAI **24 passed, 3 skipped** (identical to the certified reference — vendor rows still BLOCKED). gh_intake 8 (GH-4 idempotent holds), cr_contract 19/2, connect 5, delivery 6, lifecycle 7, substrate 20 — all green. Codex blockers preserved: `coord_accept_v1` CO-10 FAIL (1 failed/10 passed), `fa_core_v1` FAC-1..5 FAIL.

## Release decisions (kept strictly separate)
- **A. #304 local agent-side transport implementation:** interim PASS of the transport design + CF-1 closure, **with one P3 defect (CF-2)** and the Cursor-owned CI blocker (CI-1, transitive zod). **Pre-freeze recommendation: RETURN_TO_BUILDER** for CF-2 (small) + CI-1.
- **B. Authenticated external vendor integration:** **BLOCKED** — vendor/model/remote/private-session transports all fail-closed; no authenticated external receipt. Unchanged by 859f5ec.
- **C. Readiness to eliminate Founder manual forwarding:** **NO** — see the acceptance spec; the runner exists and now persists cold-start, but real vendor connectivity is absent and CF-2 is open.
- **D. Unattended autonomy:** **BLOCKED** — Codex FA-9/CO-10 + FA-PRIV open.

## Findings (ranked)
| ID | Sev | Finding | Owner | Repro |
|---|---|---|---|---|
| CF-2 | **P3** | CF-1 fix reclassifies a real warm-owner assignment as synthetic ledger backing (invariant violation; no data loss demonstrated) | Cursor | `agentbridge_v2` CS-3 |
| CI-1 | CI | `Frontend — npm audit` red on transitive `zod` `GHSA-mrhx-c7xc-m3w5` (not a direct dep) | Cursor | user-reported CI; lockfile |
| FA-9/CO-10 | P1 | authenticated STOP-clear (preserved) | Codex | `coord_accept_v1` CO-10 |
| FA-PRIV | P1 | `mainai_app` over security-critical tables (preserved) | Codex | `fa_core_v1` |
| RAI-01/13/21/27 | BLOCKED/NI | external receipt, real reconnection, audit immutability, semantic correspondence | Cursor(#304+)/Codex | `rai_v1` |

## Interim conclusion
The revised candidate **genuinely closes CF-1** (cold-start bridge persistence verified fixed in behavior) and passes the full adversarial + regression battery with no regression — a real advance. It introduces one **P3** invariant-violation defect (CF-2) as a side effect of the fix, and still carries the Cursor-owned CI blocker. External vendor connectivity remains correctly BLOCKED (scope B), and the Founder still cannot stop forwarding to real agents (scope C). **Pre-freeze recommendation: RETURN_TO_BUILDER (CF-2 + CI-1).** Final PASS/FREEZE or RETURN_TO_BUILDER will issue only once Cursor freezes an exact SHA with both workflows green; then I re-run GH-4/GH-4b, CONN, lifecycle/existing-assignment regression, `agentbridge_v2` (CS-3 must flip), `rai_v1`, and the CR contract on that SHA.

## Reproduce
```
probes/agentbridge_v2/run.sh ../wtcert-859f5ec   # CS-1..6 (CF-1 fixed; CS-3 records CF-2) + ADV-1..8: 13 passed, 1 skipped
probes/agentbridge_v1/run.sh ../wtcert-859f5ec   # AT-1..7 transport negatives still hold
probes/rai_v1/run.sh         ../wtcert-859f5ec   # 24 passed, 3 skipped (BLOCKED)
probes/gh_intake_v1/run.sh   ../wtcert-859f5ec   # 8 passed (GH-4 idempotent)
probes/cr_contract_v1/run.sh ../wtcert-859f5ec   # 19 passed, 2 skipped
probes/coord_accept_v1/run.sh ../wtcert-859f5ec  # CO-10 FAIL = preserved (Codex)
probes/fa_core_v1/run.sh     ../wtcert-859f5ec   # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
```
