# LIVE-RUNTIME handover acceptance gate — operational verdict

- **Baseline SHA:** `1105e331a444c47d80d10265c4947eb1b48aeb72` (#299, FROZEN).
- **Gate:** `live_handover_v1` (LRH-1..LRH-10). Production path only: the real ASGI app + worker, the real persistence boundary (`AgentWorkAssignment.provenance` via `persist_to_assignment`), and the real Founder read path (`founder_coordination_snapshot`). Isolated DB, credential-free, no paid calls. **No in-process `DurableCoordinationService` is substituted for a runtime** — the discovery check `_ingestion_surface()` inspects the mounted routes and the worker for a real ingestion path and governs every ingestion-dependent scenario.

## Operational verdict: **BLOCKED**

**MainAI cannot yet take over routine development coordination.** The read side is live and correct; the write (ingestion) side has no production entry point.

| LRH | Scenario | Result |
|---|---|---|
| 1 | Real app + worker stack boots (isolated, credential-free) | **PASS** |
| 2 | **A production ingestion boundary exists** (HTTP route or worker/event hook into the durable ledger) | **BLOCKED** — none found |
| 3 | Submit a real assignment through a production entry point | **BLOCKED** (depends on 2) |
| 4 | Persisted obligation survives restart + shows in the Founder dashboard (READ path) | **PASS** — a real `AgentWorkAssignment` persisted via `persist_to_assignment`, then a fresh session reads it back through `founder_coordination_snapshot` with status `acknowledged` intact |
| 5 | Three identified agents, concurrent / delayed / out-of-order events | **BLOCKED** (depends on 2) |
| 6 | Historical queue-empty cannot erase a newer active assignment | **BLOCKED** (depends on 2) |
| 7 | Only a genuinely-completed agent becomes eligible for more work | **BLOCKED** (depends on 2) |
| 8 | A completed candidate generates exactly one independent-review obligation | **BLOCKED** (depends on 2) |
| 9 | Artifact receipts survive restart; unavailable vendor transport stays BLOCKED | **BLOCKED** (depends on 2) |
| 10 | Forged/unauthorized events cannot create work, falsify completion, certify code, alter permissions or bypass STOP | **BLOCKED** (depends on 2) |

The LRH-5..10 **logic** is already proven correct in `handover_v1` (HO-1..HO-10, 12/12) and the candidate's own 16+1+3 tests. What is unproven — and what this gate isolates — is that **real agent events reach the ledger through production wiring.** Each of LRH-2/3/5–10 is an executable regression that **auto-flips to PASS** the moment `_ingestion_surface()` discovers a route or worker that writes lifecycle events into `DurableCoordinationService` + `persist_to_assignment`.

## Exact missing production dependency

**No production ingestion boundary for the durable coordination ledger.** Verified exhaustively on #299:
- No HTTP route references `DurableCoordinationService` / `persist_to_assignment` / `ingest_report` (`app/routers`, `app/main.py` — zero hits).
- `persist_to_assignment` has **zero runtime callers** (only its definition, export, and tests).
- `app/worker.py` uses only the **legacy** `agent_coordination.service.expire_stale_active_leases`, not the durable ledger.
- Even the candidate's own `tests/backend/mainai/test_durable_agent_coordination_integration.py` (3 passed) drives persistence by calling `persist_to_assignment` **directly in the test** — there is no application entry point.

So the ledger is populated only by tests and the in-process three-agent demonstration.

## Shortest verified route to replacing manual Founder coordination

1. **Ingestion surface (Cursor / MainAI — this is the whole remaining gap for coordination):** add a Founder/MainAI-authenticated production path — an HTTP endpoint (e.g. `POST /api/mainai/coordination/{assign,ack,execute,complete,verify,handoff}`) or a worker/event hook — that:
   - loads the owner ledger (`load_owner_ledger`), applies the real lifecycle event (`assign`/`acknowledge`/`record_execution_progress`/`complete`/`independently_verify`/artifact calls), and `persist_to_assignment` on each. The logic to call already exists and is proven; only this wiring is missing. When it lands, LRH-2/3/5–10 turn PASS with no change to this gate.
2. **Codex — STOP-resume integrity (FA-9 / CO-10):** bind STOP-clear to a reauth/signed token. *(Preserved, Codex-owned; not duplicated here.)*
3. **Codex — Security-Kernel DB enforcement (FA-PRIV):** authority/approval/audit independent of the runtime role. *(Preserved, Codex-owned.)*

Items 2–3 are the separately-confirmed safety blockers. Item 1 is the single coordination-integration blocker and needs no external API access and no manual Founder file transfers.

## Reproduce
```
probes/live_handover_v1/run.sh ../wtcert-1105e33     # LRH-1/4 PASS; LRH-2/3/5-10 BLOCKED (auto-PASS when wired)
# wiring proof: grep -rn "persist_to_assignment\|DurableCoordinationService" backend/app/routers backend/app/worker.py  (none)
```
