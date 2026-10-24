# Interim examination — PR #304 (Agent-Side Communication Bridge)

- **Candidate SHA (MOVING, not frozen):** `4102c167f85e643f3fffb5a9fbf0e9d5b01f6b04`. Baseline: certified #303 `74ebda9` (verified ancestor; 3 commits on top).
- **Status:** INTERIM. No final verdict is issued on a moving candidate. CI at last independent check: `Backend — unit/integration` still running; `Frontend — npm audit` FAILED on a transitive-`zod` advisory (`GHSA-mrhx-c7xc-m3w5`). Cursor owns CI resolution.
- **Examiner:** Claude, read-only; real app/worker/DB/gateway/transport; no paid calls, no GitHub writes, no production change, no external activation; `first_cycle_go=false`. Worktree verified unchanged after every suite.
- **Delta over #303:** `transport.py` (AgentTransport abstraction), `transports/{__init__,gateway,local_cli,vendor,executor}.py`, `agent_side.py` (runner entrypoint), `connect.py` +28, founder_ops +15, candidate test (196).

## Release decisions (kept strictly separate — a PASS in one scope never authorizes another)

### A. #304 local agent-side transport implementation — **interim PASS with one defect (CF-1) + a Cursor-owned CI blocker**
The code is honest and well-isolated. The `AgentTransport` abstraction keys transports by id (never vendor name), `TransportCapabilities`/`TransportAvailability` default `vendor_delivery_claimed=False`, and every surface is reported as itself. Verified by `agentbridge_v1` AT-1..AT-7 (all pass):
- **AT-1:** every vendor/model/remote/private-session transport is `BlockedVendorTransport` and `fail_closed` on deliver/execute/ingest — **never simulates**; `vendor_delivery_claimed=False`.
- **AT-2:** `capability_matrix()` never marks a vendor surface usable; the runner is reported as `agent_side_runner`, explicitly "not a vendor session."
- **AT-3:** `agent_side` runner fail-closed without a token (`founder_agent_side_authorization`).
- **AT-4:** executor `record` mode persists structured evidence only — no subprocess, `interpolated_instruction=False`, `write_scope=none` (a `rm -rf /` instruction is never shell-interpolated).
- **AT-5:** executor `allowlisted` mode fail-closed without Founder-enabled provider+args; unknown mode blocked.
- **AT-6:** a real authorized runner round trip delivers, and a duplicate `execute` of the same message → `replay_ignored`.
- **AT-7:** the runner inherits gateway recipient+owner scoping (cross-agent claim `forged_rejected`; owner-2 token leaks 0 of owner-1's messages). *(Note: the runner is authenticated by Founder-issued token, which is possession-based — not cryptographic proof that the token holder IS the vendor agent; see scope B.)*

**No regression:** RAI 24/3, gh_intake 8 (GH-4 idempotent holds), cr_contract 19/2, connect 5, delivery 6, lifecycle 7, substrate 20 — all green on #304.

**Defect CF-1 (P2, cold-start delivery persistence)** and **the CI blocker** are why A is not yet a FREEZE — see Findings.

### B. Authenticated external vendor integration — **BLOCKED (unchanged, correctly)**
Cursor/Claude/Codex vendor sessions, provider model APIs and remote background agents are all `BlockedVendorTransport`; `inspect_surfaces` marks each `usable=False` with a named `missing_capability` and the explicit note that a Founder-enabled local CLI "is still not this surface." RAI-01 (authenticated external receipt) and RAI-13 (real reconnection) remain **BLOCKED** — #304 ships the runner an external agent *could* operate, but provides **no** authenticated proof that a real vendor agent received or executed anything. #304 does not claim otherwise. This is a genuine advance in scaffolding, not external connectivity.

### C. Readiness to eliminate Founder manual forwarding — **NO**
For any agent that operates the authorized runner with a *persisted* token, MainAI now automates deliver→ack→execute→complete. But (1) real vendor connectivity is BLOCKED (B), and (2) CF-1 means a freshly-issued credential for an owner with no prior assignment does not survive a restart. So the Founder still cannot stop forwarding to the real vendor agents. The manual-forwarding-reduction criteria in `rai_v1/RAI_ACCEPTANCE_CONTRACT.md` are not yet met.

### D. Unattended autonomy — **BLOCKED (Codex-owned, unchanged)**
FA-9/CO-10 (authenticated STOP-clear) and FA-PRIV (`mainai_app` over security-critical tables) remain open: `coord_accept_v1` CO-10 FAIL (1 failed/10 passed), `fa_core_v1` FAC-1..5 FAIL. #304 correctly does not touch these.

## Findings
| ID | Sev | Finding | Reproduction | Affected interface | Owner |
|----|----|----|----|----|----|
| **CF-1** | **P2** | Cold-start delivery persistence gap: `issue_bridge_credential` → `persist_delivery`, which early-returns `if not assignments: return`. A Founder-issued bridge for an owner with **zero** `AgentWorkAssignment` rows reports `issued` but is **never persisted** — it does not survive a fresh session, so the agent-side runner's token fails to authenticate. The #303 cold-start fix (`persist.ensure_owner_ledger_backing`) is used by the lifecycle/github-intake path but **not** by the delivery path. | `agentbridge_v1` CS-1 (`resolve_cold_after_restart=False`, warm control `True`) and CS-2 (HTTP inbox → 403). No seeding. | `delivery.issue_bridge_credential` / `persist_delivery`; `routers/agent_gateway` | Cursor (do not duplicate; smallest fix: have `issue_bridge_credential` call `ensure_owner_ledger_backing` first, or make `persist_delivery` create a backing row like `persist_owner_ledger`) |
| CI-1 | (CI) | `Frontend — npm audit` fails on a **transitive** `zod` advisory `GHSA-mrhx-c7xc-m3w5` (zod is not a direct dep — direct deps are only next/react/react-dom). Per the repo's own working principle, an unrelated advisory is normally fixed on its own branch, not folded into the PR. | user-reported CI; lockfile shows transitive `zod ^3.25.0 || ^4.0.0` | frontend lockfile | Cursor (CI) |

Regarding the RAI-06 fixture note: the earlier seed was **not** masking a test artifact — it masked **CF-1**, a genuine production cold-start limitation, now reproduced independently without seeding. Seeding an assignment is **not** a documented or enforced prerequisite for issuing a bridge credential.

## Real-integration vs local-execution classification (task 3)
| Layer | State on #304 | Evidence |
|----|----|----|
| Local authenticated runner | **PASS** (exists, Founder-token, fail-closed) | AT-3/AT-6; `agent_side.py` |
| Actual external agent receipt | **BLOCKED** | RAI-01; no authenticated external receipt distinct from a local token pull |
| Real coding-agent execution | **BLOCKED / record-only** | executor `record` default = evidence only; `allowlisted` needs Founder enablement; no vendor execution |
| Authenticated completion evidence | **PARTIAL** | evidence dict required (AT-4/RAI-03) but self-asserted by token holder — not independently verifiable |
| Connection to existing private vendor sessions | **BLOCKED** | `BlockedVendorTransport`; `vendor_session_* usable=False` |
| Durable return of results to MainAI | **PASS for the gateway runner path** (restart-safe receipts), **but CF-1 breaks the cold-start credential** | AT-6, RAI-10/11; CF-1 |

A successful local runner is **not** counted as a PASS for any vendor integration.

## RAI-01..RAI-27 coverage on #304
Identical profile to the #303 reference: **24 passed, 3 skipped (BLOCKED: RAI-01, RAI-13, RAI-21)**. #304 adds the runner/transport scaffolding the future MUST-flip rows (RAI-01/13/22/27) depend on, but does not itself flip any BLOCKED row to PASS — vendor connectivity is still absent. Full matrix: `probes/rai_v1/RAI_ACCEPTANCE_CONTRACT.md`.

## Remaining BLOCKED / NOT_IMPLEMENTED
Authenticated external receipt (RAI-01); real reconnection (RAI-13); audit immutability = Codex FA-PRIV (RAI-21); semantic request/response correspondence (RAI-27 NOT_IMPLEMENTED); real vendor sessions/model APIs/remote agents (all `BlockedVendorTransport`). Codex FA-9/CO-10 + FA-PRIV gate unattended autonomy.

## Interim conclusion
#304 **genuinely advances** MainAI toward external-agent communication: it adds an honest, least-privilege transport abstraction and an operable agent-side runner, keeps every vendor surface truthfully BLOCKED (no simulation), and regresses nothing. It does **not** establish real vendor connectivity (scope B BLOCKED), does not yet let the Founder stop forwarding to real agents (scope C NO), and carries one P2 cold-start defect (CF-1) plus a Cursor-owned CI blocker (CI-1). **Pre-freeze recommendation: RETURN_TO_BUILDER for CF-1 + CI-1.** When Cursor freezes a candidate with both workflows green, re-run GH-4/GH-4b, CONN, the lifecycle/existing-assignment regression, `agentbridge_v1` (CS-1/CS-2 must flip or be documented), `rai_v1`, and the CR contract on the exact SHA, then issue the final PASS/FREEZE or RETURN_TO_BUILDER.

## Reproduce
```
probes/agentbridge_v1/run.sh ../wtcert-4102c16   # CS-1/CS-2 (cold-start CF-1) + AT-1..7 (transport negatives): 9 passed
probes/rai_v1/run.sh         ../wtcert-4102c16   # RAI-01..27: 24 passed, 3 skipped (BLOCKED)
probes/gh_intake_v1/run.sh   ../wtcert-4102c16   # 8 passed (GH-4 idempotent holds)
probes/cr_contract_v1/run.sh ../wtcert-4102c16   # 19 passed, 2 skipped
probes/connect_v1/run.sh     ../wtcert-4102c16   # 5 passed
probes/coord_accept_v1/run.sh ../wtcert-4102c16  # CO-10 FAIL = preserved (Codex)
probes/fa_core_v1/run.sh     ../wtcert-4102c16   # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
```
