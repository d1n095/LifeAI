# Independent examination — PR #310 `beecf0e3` (Founder-absence operational handover)

- **Candidate SHA:** `beecf0e35cd27adffff6f95330e9514db8a6ea5d`. **Base:** #306 `2444bcc` (PARALLEL to
  #309 — does NOT contain #309's provider_ingestion / external_connectivity / mainai_restrictions;
  the diff vs aff5de1a shows those as "deleted" only because #310 never had them).
- **Delta vs 2444bcc (527 insertions, backend):** new `app/mainai_operational_handover/`
  (`absence.py`, `interactive.py`, `self_improve.py`, `gates.py`), `mainai_founder_ops/snapshot.py`
  (+39), `routers/mainai_founder_ops.py` (+19), `durable.py` (+27 — `_operational_handover` persisted
  in `dump_for` / merged on load).
- **Suite:** `probes/absence_v1` — **15 passed, 1 xfailed(strict)**. Pure in-memory policy functions
  (no DB, no network, no paid calls). `first_cycle_go=false`.

## 1. Actual implementation status (genuinely implemented vs simulated/blocked)
| Capability | Status | Evidence |
|---|---|---|
| Founder absence mode (bounded operating policy) | **IMPLEMENTED (policy)** | `evaluate_absence_tick` AB1–AB7 |
| Silence never becomes authorization | **IMPLEMENTED** | AB2 (`needs_approval & !approved → paused`), AB7 catch-up `silence_was_not_treated_as_approval` |
| STOP / spending / privilege boundaries | **IMPLEMENTED (policy, caller-fed)** | AB1 STOP→pause-all, AB3 high-risk pause, AB6 spend-exhausted pause |
| Interactive responsiveness + resource isolation | **IMPLEMENTED (model)** | IN1 interactive always admitted; IN2 background bounded + backpressure |
| Durable status reporting + catch-up summaries | **IMPLEMENTED** | AB7 `founder_catchup_report`; `_operational_handover` serialized (SI4) |
| Bounded self-improvement | **IMPLEMENTED (fail-closed)** | SI1 untrusted actor rejected; SI2 proposals require review, never grant/deploy; SI3 unverified lesson not governing |
| Autonomous delegation within standing authorization | **PARTIAL (policy only)** | AB5 standing-grant gating is enforced in policy; no live delegating executor |
| Dynamic capability-based agent selection | **NOT_IMPLEMENTED (declared, not wired)** | IE1 xfail: `delegate_to_capability` is a proposal field; `delegated_engineering=False`, no executing selector |

**Overall:** #310 ships a deterministic, fail-closed **policy/model layer** with durable state. It is
**not** a wired autonomous execution loop: proposals are prepared, never delegated/executed
(`delegated_engineering=False`, `tested=False`, `deployed=False`), and the absence policy consumes a
caller-supplied `stop_blocked` / `spend_exhausted` rather than reading the kill switch or spend ledger
itself. "MainAI remains available to the Founder while background work continues" is demonstrable at
the admission-control MODEL level (IN1: interactive admitted with priority 0 even while background is
saturated), not as a running integration.

## 2. Test results & reproduce
`probes/absence_v1/run.sh ../../wtcert-beecf0e` → 15 passed, 1 xfailed. Covers: STOP pauses all;
silence≠approval; high-risk pause while low-risk continues; bounded recoverable retries (max 3);
new-work standing-grant gating; spend-exhausted pauses only provider-costing items; honest catch-up;
interactive always admitted under background saturation; background bounded + backpressure; provider
not permanently bound to a category; untrusted actor rejected; proposals never grant/deploy; unverified
lesson not governing; self-improve state serialized; honest gate reporting (unpassed gate never
claimed). (Pure functions — DB-free runner; no `tests.conftest`.)

## 3. New defects / findings (severity · owner)
- **DELEGATE-WIRING (P2 · Cursor/MainAI):** dynamic capability-based agent selection and autonomous
  delegation are declared (`delegate_to_capability`) but not wired — nothing selects a capability and
  executes the proposal (`delegated_engineering=False`). The "autonomous delegation within standing
  authorization" capability is a policy/proposal, not a running selector. `xfail(strict)` IE1.
- **ABSENCE-STOP-SOURCE (P3 · Cursor/MainAI):** `evaluate_absence_tick` trusts a caller-supplied
  `stop_blocked`/`spend_exhausted` instead of reading `assert_not_killed` / the spend ledger. Correct
  as a pure policy, but the wiring that feeds it must itself fail closed (same family as PI-STOP /
  SPEND-STOP). No independent authority read inside the policy.
- **IN-dead-branch (P4 · Cursor, cosmetic):** `interactive.admit` line 52's second condition
  (`_background_in_flight >= max`) is unreachable (line 50 already returned when true); the queue-full
  reject is handled in `_queue_or_reject`. No functional impact.
- No STOP/spend/privilege control is weakened; no autonomy GO; `first_cycle_go=false` throughout.

## 4. Missing requirements for first operational handover
1. A **wired autonomous loop** that consumes `evaluate_absence_tick` output and actually continues/
   pauses real work (today the policy is pure; nothing drives it on a schedule).
2. **Capability-based selector + delegation executor** (closes DELEGATE-WIRING) that routes an
   evidence-backed proposal to a capability and runs it under independent review.
3. **Authority sourcing**: the loop must read STOP (`assert_not_killed`) and spend liveness directly
   (closes ABSENCE-STOP-SOURCE), not rely on a caller flag — ties to PI-STOP / SPEND-STOP wiring.
4. Real **external-agent round trip** (Gate-1 `real_external_agent_round_trip` is False) + an
   **approved unattended-cycle trial** (Gate-1 part False) — both Founder/live-authority gated.

## 5. What can safely proceed while other work is blocked
- The policy/model layer is sound and independently testable now; the absence/interactive/self-improve
  **contracts are frozen-able as a spec**. Cursor can wire the loop against these exact functions.
- Examiner can pre-build the **autonomous-loop acceptance** (drive a stub loop through
  `evaluate_absence_tick` + the interactive controller) so it flips to PASS when wiring lands —
  without any autonomy GO.

## 6. Independent verdict
**#310 `beecf0e3`: the Founder-absence operational-handover POLICY/MODEL layer is independently sound,
fail-closed, and honest (silence≠approval, STOP/high-risk/spend pauses, bounded retries, bounded
self-improvement that never grants or deploys, honest gate reporting).** It is **NOT** a wired
autonomous system: autonomous delegation and dynamic capability-based selection are declared but not
executing (DELEGATE-WIRING, P2), and the policy reads caller-fed authority rather than the kill
switch/spend ledger (ABSENCE-STOP-SOURCE, P3). **NOT CERTIFIED for operational handover**; Gate-1
correctly reports itself unpassed. No merge/deploy/GO. `first_cycle_go=false`. Target worktree unchanged.
