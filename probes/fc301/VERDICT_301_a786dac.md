# Final production-runtime certification — PR #301 (autonomous coordination)

- **Candidate:** PR #301 (draft, frozen for independent examination), on #300.
- **Examined SHA:** `a786dac9f32bc3120fa2176e18041a75f1cf6c56`. Base #300 `2c9dd7e06dd3072f8f1f31531437f285ae978e5f`.
- **Exact-SHA CI:** all 12 required checks green on BOTH suites (push `39853268222`, pull request `39853274620`), independently re-read.
- **Delta over #300:** `app/agent_coordination/lifecycle.py` (630 lines, new); `POST /api/mainai/founder-ops/coordination/events` (Founder-only, +31 in `mainai_founder_ops.py`); `Worker._reconcile_durable_coordination` hook called from `run_once()` (+18 in `worker.py`); `_persist_ledger` rewritten to persist **all** obligations (DEF-1 fix). Next.js pinned 16.4.2.
- **Examiner:** Claude, read-only. The only faked boundary is GitHub network JSON (gh_intake_v1). The ASGI app, auth, worker hook, lifecycle service, persistence and the Founder read are all production code. No paid calls, no GitHub writes; every suite runs outside a detached worktree and verifies it is unchanged.

---

## A. Exact-SHA code — **RETURN_TO_BUILDER** (one bounded residual: DEF-1 GH-4)

The new coordination surface is correct and hardened. One durability guarantee the PR's own item 1 states ("persist … even when no prior assignment exists") is **not met on a reachable endpoint**.

### DEF-1 — partially fixed; residual on the zero-backing-row path (P2)
`_persist_ledger` (lifecycle.py) now iterates **all** `service._obligations` and persists each obligation whose `backing_assignment_id`/obligation_id matches an `AgentWorkAssignment`. That closes the #300 "only 1 of 2 survives" defect:
- **GH-3 / GH-4b (now PASS):** a PR's 2 obligations (builder + examiner) **both** survive a fresh-session restart (`created=2 survived_restart=2 missing=set()`; `restart_obligations=2`).

But the function still opens with `if not assignments: return` — when the owner has **zero** `AgentWorkAssignment` rows, nothing persists, and neither endpoint constructs a backing row:
- **GH-4 (still FAIL):** two `github-intake/refresh` HTTP calls for the same PR with no pre-existing workforce assignment return **different** obligation ids each time (`idempotent=False`) — the ledger reloads empty and re-mints. Not idempotent / not durable across HTTP calls or restart for a GitHub-discovered-only obligation.

**Scope of the residual:** every obligation that originates from a real workforce `AgentWorkAssignment` (the entire autonomous-coordination runtime path — the coordination/events endpoint and the worker reconcile) always has a backing row, so it persists and is idempotent (proved by LC3, LC5 below). The gap is isolated to the #300 github-intake path discovering a PR that has no corresponding workforce assignment yet. **Fix (small):** ensure/create a durable backing row (or a ledger-owned row independent of `AgentWorkAssignment`) when none exists, so reload is complete and idempotent. Owner: Cursor. Gate: GH-4.

### Everything else on the new surface is correct (production-path verified — `lifecycle_v1` LC-1..7, all pass)
| Item | LC | Result |
|---|---|---|
| 3 endpoint is Founder-only | LC1 | PASS — anon (401/403), ordinary member (403), and a **role-only "founder" impostor** (403) all refused |
| 3 forged lifecycle kind / unknown agent identity | LC2 | PASS — both `forged_rejected` via the real endpoint |
| 1,3 replay protection survives a fresh DB session | LC3 | PASS — an applied event's `event_id` is `replay_ignored` after the ledger is reloaded in a brand-new session (restart-safe dedup) |
| 6,9 prepared instruction ≠ delivered; vendor messaging blocked | LC4 | PASS — `delivery_claimed=False`; all three vendor-messaging channels `usable=False` |
| 2,7 worker hook auto-projects real lifecycle into ledger | LC5 | PASS — a real planned→ready→running→completed assignment, after `Worker()._reconcile_durable_coordination`, appears in `founder_coordination_snapshot` from a fresh session (`obligations=1 status=completed`) |
| 3 payload identity never trusted for attribution | LC6 | PASS — (a) Founder posting `agent_identity=codex` for a cursor assignment is neutralized to the real identity, never attributed to codex; (b) a non-privileged `codex` completing cursor's assignment is `forged_rejected` |
| 5,6 historical queue-empty cannot erase active work | LC7 | PASS — `queue_empty_claim` while an obligation is active is `stale_rejected`; the active obligation still present after restart |

Reproduce: `probes/lifecycle_v1/run.sh ../wtcert-a786dac` (7 passed); `probes/gh_intake_v1/run.sh ../wtcert-a786dac` (7 pass, GH-4 FAIL = residual).

---

## B. Internal autonomous coordination — **PASS** for the assignment-backed runtime path (one bounded durability gap, = A's residual)

This is the question #299/#300 left open ("is there a production ingestion boundary, and does it run through the real worker?"). On #301 the answer is **yes**, verified through production wiring, not an in-process service substituted in a test:

- **Ingestion boundary exists and is live:** `live_handover_v1` **LRH-2 now PASS** — `_ingestion_surface()` discovers the real route `POST /api/mainai/founder-ops/github-intake/refresh`; LRH-1/4 pass (9/9). The coordination/events endpoint adds a second authenticated boundary.
- **Worker auto-reconciliation (item 2):** `Worker._reconcile_durable_coordination` is called every `run_once()` and projects real `AgentWorkAssignmentEvent` rows into the durable ledger. Verified end-to-end by LC5 (my independent probe) **and** the candidate's own `test_worker_three_agent_restart_and_follow_up` (real `await Worker().run_once()` + restart; 5/5 pass against the frozen SHA).
- **Three-agent across-restart (item 5):** the candidate test drives three identified agents (cursor builder, claude reviewer, codex builder) all executing; cursor completes; after a worker **restart** cursor=completed while the other two remain `executing`, and **exactly one** examiner obligation exists and is not `independently_verified`. My LC7 adds the historical-queue-empty-cannot-erase property.
- **Dashboard reflects persisted runtime state (item 7):** LC5/LRH-4 — a fresh-session `founder_coordination_snapshot` shows the real persisted obligation and status, not a demonstration object.
- **Green CI is never certification; a prepared obligation is never a delivery (item 6):** LC4 + candidate `test_github_green_is_not_independent_certification` + GH-6 — `first_cycle_go=False`, `delivery_claimed=False`, `github_green_is_independent_certification=False`, cursor is builder-eligible but never in `independently_certified`.
- **Forgery/replay/spoofing hardened (item 3):** LC1/2/3/6 + candidate spoof/forge/duplicate tests.

**Bounded gap:** for an obligation that exists only from GitHub discovery with no workforce assignment, durability/idempotency is not yet guaranteed (A's DEF-1 residual). For the actual dispatched-work runtime, coordination is durable and restart-safe.

---

## C. External agent delivery — **BLOCKED** (honestly reported; no simulated receipts)

`delivery_adapter_inventory()` (read from the frozen tree) enumerates every channel truthfully:

| Channel | Kind | Usable | Missing capability |
|---|---|---|---|
| `in_process_registry` | ledger | **True** | — (internal obligation tracking only) |
| `local_authorized_workspace` | transfer | **True** | — (local file transfer in the authorized workspace) |
| `claude-code` / `cursor-agent` / `codex` | local_cli_execution | **False** | `founder_adapter_enablement` (Founder must enable + provide executable + credentials) |
| `cursor` / `claude` / `codex` _vendor_messaging | messaging | **False** | `agent_messaging_adapter` (no such vendor API exists) |
| `github_publication` | write | **False** | `codex_github_publication_authority` (Codex-owned) |

There is **no authorized mechanism to deliver an instruction to Cursor, Claude or Codex today.** The only usable channels record/transfer locally; they do not reach an external agent. No private vendor messaging API is assumed to exist — the inventory marks all three `usable=False`. A delivery receipt cannot be produced without the Founder enabling a local-CLI adapter and supplying credentials (outside this examination's authority). The endpoint's `delivery_claim` path returns `delivery_claimed=False` + `blocked` (LC4) — a prepared instruction is never reported as a delivered one.

---

## D. Founder handover readiness — **NOT YET** (the Founder cannot stop forwarding today)

What the Founder **no longer** has to do manually: track and reconcile workforce task status/progress/completion between the runtime and the dashboard. The worker now auto-projects real lifecycle events into the durable, restart-safe ledger that the Founder dashboard reads (B). Forged/replayed/queue-empty events cannot corrupt that state.

What **still requires the Founder**, with the smallest remaining executable tasks and owners:
1. **Agent delivery (Cursor/MainAI) — the critical path.** No adapter can actually hand an instruction to Cursor/Claude/Codex (C). Smallest task: enable + implement one authorized delivery path — the `local_cli_execution` adapter (Founder-enabled, credentialed, executable-present) is the only non-vendor option present; vendor messaging does not exist. Until then MainAI records obligations but the Founder forwards the actual instructions.
2. **DEF-1 residual (Cursor).** Persist/ensure a durable backing row so github-discovered-only obligations are durable and idempotent (A / GH-4).
3. **Codex safety blockers — preserved, independently confirmed still open, must not be GO'd:**
   - **CO-10 / FA-9** — STOP-clear is gated only by a `founder_ack` string, not an authenticated Founder (`coord_accept_v1` CO-10 FAILS = preserved positive control).
   - **FA-PRIV** — the runtime role `mainai_app` can still `UPDATE`/`DELETE` `users`, `audit_log`, `workforce_authority_epoch`, `execution_authorization_envelopes`, `provider_spend_authorizations`, `mainai_goals` (`security_kernel_v1` FA-PRIV + `fa_core_v1` FAC-1..5 FAIL = preserved). #301 is a coordination PR and correctly does not touch these.

Items 1–2 are coordination/durability; item 3 is safety. None needs GitHub writes or external API access beyond read-only.

---

## Item-by-item (the 10 requirements)

1. **DEF-1/GH-4/GH-4b** — GH-4b **FIXED** (both obligations survive restart); GH-4 **RESIDUAL** (no-backing-row idempotency). Partial. → A.
2. **`Worker._reconcile_durable_coordination` through the real lifecycle** — **VERIFIED** (LC5 + candidate `run_once` restart test + LRH-2 route live).
3. **Authenticated coordination-events endpoint** — **VERIFIED** (LC1 Founder-only incl. role-only impostor; LC2 forged kind/identity; LC3 replay across restart; LC6 fabricated/cross-agent; LC7 stale queue-empty).
4. **Existing suites** — live_handover 9/9, security_diag 10/10, coord 10 pass (CO-10 preserved-fail), gh_intake 7 pass (GH-4 residual), candidate lifecycle 5/5. No stale fixture reclassified except my own new LC fixtures, which I corrected (wrong threat model / random id) **before** judging — the implementation was right in each case.
5. **Three-agent across restarts** — **VERIFIED** (candidate real-worker test: one builder completes, two remain active, exactly one examiner; + LC7 queue-empty-cannot-erase).
6. **Green CI ≠ certification; prepared ≠ delivered** — **VERIFIED** (LC4 + candidate github-green test + GH-6).
7. **Dashboard = persisted runtime state** — **VERIFIED** (LC5/LRH-4 fresh-session snapshot).
8. **Next.js 16.4.2** — **VERIFIED** — pinned in `frontend/package.json` and `package-lock.json`; `npm audit` CI job preserved (`audit:policy:test` + `check-npm-audit.js`: production findings always fail, high/critical dev fail except one dated braces advisory expiring 2027-01-15). Policy not weakened.
9. **External delivery mechanisms** — **ESTABLISHED** (C): in_process_registry + local_authorized_workspace usable; local CLI execution usable only on Founder enablement + credentials; vendor messaging non-existent; github_publication Codex-blocked. No private vendor API assumed.
10. **FA-9/CO-10 + FA-PRIV preserved** — **CONFIRMED still open** (positive controls fail as required). No false GO.

## Reproduce
```
probes/lifecycle_v1/run.sh    ../wtcert-a786dac   # LC-1..7 (7 passed) — new coordination surface
probes/gh_intake_v1/run.sh    ../wtcert-a786dac   # GH-4b PASS (DEF-1 multi-obligation fixed), GH-4 FAIL (residual)
probes/live_handover_v1/run.sh ../wtcert-a786dac  # LRH-2 now PASS (ingestion surface live); 9/9
probes/coord_accept_v1/run.sh ../wtcert-a786dac   # CO-10 FAIL = preserved STOP-clear blocker
probes/fa_core_v1/run.sh      ../wtcert-a786dac   # FAC-1..5 FAIL = preserved FA-PRIV
probes/security_kernel_v1/run.sh ../wtcert-a786dac # FA-PRIV + FA-* FAIL = preserved
# candidate's own production-path suite (in target):
#   pytest tests/backend/test_lifecycle_autonomous_coordination.py  -> 5 passed @ a786dac
```
