# MainAI Unattended Operations — Acceptance Matrix (Gate A local deterministic / Gate B real trial)

- **Objective:** let the Founder stop manually supervising and forwarding messages between coding agents, while MainAI runs authorized development cycles independently — provider-neutral, secure, auditable, Founder-permissioned.
- **Composed stack examined:** #306 `2444bcc3da8ffa5ca10ef3295bac13aa045769c6` (full #303→#306 agent-side transport + CF-2 fix + repair). #307 is a parallel connectivity prototype, examined separately.
- **Examiner-owned.** No production code, no live/paid provider calls, no external activation. `first_cycle_go=false`. Codex FA-9/CO-10 + FA-PRIV preserved.
- **Suite:** `unattended_v1` (Gate A). Reuses the existing delivery/lifecycle/routing/service primitives; cross-references RAI/CR/GH/agentbridge for adversarial coverage rather than duplicating it.

## Gate A — local deterministic cycle (executable now): **PASS**
Controlled/simulated agent (`LocalAuthorizedBridge` over the in-process gateway), persistent DB state, injected failures, a real worker restart. No Founder step after the initial authorized assignment.

| UA | Priority-1 capability | Result | Evidence |
|---|---|---|---|
| UA-1 | discover + assign authorized work; track real ownership/status; receive+validate completion | **PASS** | `delivered=1 completed=True ownership=cursor vendor_claimed=False` |
| UA-2 | completion/execution evidence required (self-asserted success not accepted) | **PASS** | empty-evidence execution `rejected` |
| UA-4 | completed builder → exactly ONE independent-examiner obligation; builder ≠ examiner | **PASS** | `examiner_obligations=1`, not pre-`independently_verified` |
| UA-5 | examiner findings route to a correction assignment (not certified) | **PASS** | `reviewing→changes_requested→ready` (`final_status=ready`) |
| UA-6 | continue to the next authorized task without Founder intervention | **PASS** | routing `ASSIGNMENT_FOUND`, picks the agent's own ready work |
| UA-7 | no duplicated assignments or repeated execution | **PASS** | duplicate assignment `DuplicateWorkError`; duplicate event `replay_ignored` |
| UA-8 | detect stalled agents and recover safely | **PASS** | stale lease detected, `expire_stale_active_leases=1`, stale `renew_lease` rejected (stale worker loses write authority) |
| UA-9 | preserve coordination state across restarts | **PASS** | completed cycle intact in a fresh session |
| UA-10 | escalate only genuinely authorization-dependent decisions | **PASS** | `escalate_privilege → privilege_rejected` (never auto-granted); routine progress not escalated |
| UA-3 | receive+validate completion evidence (bridge round trip) | **PASS** | covered within UA-1 (ack→execute→complete with evidence) |
| UA-PI | provider-neutral substrate; an adapter cannot redefine trusted state | **PASS** | identity map deterministic for cursor/claude/codex; a payload `independently_verified:True` from any provider never flips the flag (`independently_verified` stays None) |

### Priority 2 (failure/manipulation) — covered, mostly by reuse (not duplicated)
| Threat | Where proven | Result |
|---|---|---|
| crash / interruption between phases | `agentbridge_v2` ADV-2 (resumable `acknowledged`, not fabricated complete) | PASS |
| lost acknowledgement | ADV-2 / UA-2 | PASS |
| replayed webhook / duplicate completion | `agentbridge_v2` ADV-4, UA-7, `extconn_v1` EXT-4 | PASS |
| forged provider result / self-asserted success | UA-2, UA-PI, RAI-03/04, CR-14/16, `extconn_v1` EXT-12 | PASS (never independently_verified) |
| contradictory reports | lifecycle status-rank / cross-agent `forged_rejected` (CR-06/25, RAI-25) | PASS |
| stale authorization | UA-8 (lease), RAI/dispatch STALE_BASE (CR-13) | PASS |
| unavailable providers | `delivery` blocked-not-fabricated (RAI-26, DLV-5) | PASS |
| STOP activation | coordination cannot clear STOP (CR-19, RAI-20); authenticated STOP-clear = Codex CO-10 | PASS (coordination) / BLOCKED (reauth, Codex) |
| unauthorized spend / privilege | UA-10 (`privilege_rejected`); spend fail-closed (#268-271); publishing BLOCKED | PASS / Codex-owned where applicable |

### Priority 3 (provider independence) — PASS
UA-PI + the RAI/CR adapter-neutral suites: orchestration/memory/authorization/decision policy are keyed on provider-neutral identities and isolated adapters; substituting the provider identity does not change coordination outcomes; capability loss degrades to BLOCKED (not fabricated); manipulated instructions / forbidden keys are rejected; **no adapter can redefine MainAI's trusted state or permissions.** External providers are replaceable tools, not authorities.

## Gate B — real unattended operation (72-hour trial): **NOT PERFORMED / BLOCKED**
`unattended_v1::test_UA_GATE_B...` is skipped with a BLOCKED reason and is **not** reported as passed. Gate B requires what the examiner cannot and must not do: a Founder-authorized **live** provider credential, a genuine external agent environment, and real paid cycles over 72 hours.

### Exact evidence required before a real 72-hour trial (Gate B entry criteria)
1. **Authenticated real vendor round trip** (RAI-01/03/22/27 flipped to PASS): a live create under a Founder/service-account key, a verified webhook/run-status completion bound to the exact assignment, independently checkable via a second GET — see `fc307/EXTERNAL_INTEGRATION_ACCEPTANCE_SPEC.md` and `VERDICT_307_INTERIM`.
2. **Durable ingestion of provider evidence** through the existing ledger (`persist_owner_ledger`) so replay/reorder/owner-isolation hold at ingestion (close `extconn_v1` EXT-4/5/6/7 and the `ledger.propose_ledger_event` → real-persist step).
3. **Bounded authority + spend controls live**: per-cycle spend grant enforced (not merely hard-blocked), STOP gate effective on provider mutations, independent examiner verification mandatory.
4. **Codex Security-Kernel blockers CLOSED**: FA-9/CO-10 (authenticated STOP-clear) and FA-PRIV (audit/authority least-privilege) — required before unattended autonomy; currently open (`coord_accept_v1` CO-10 FAIL, `fa_core_v1` FAC-1..5 FAIL).
5. **CF-2 corroboration (HD-1)** landed, so a tampered provenance marker cannot sideline real work (P3 hardening; recommended before unattended).
6. **Gate A green on the frozen composed release SHA** (this suite) + the combined release gate Scope A PASS.

## Release classification
| Scope | Status |
|---|---|
| Gate A — local deterministic unattended cycle | **PASS** (this suite, on #306 `2444bcc`) |
| Gate B — real 72h unattended trial | **NOT PERFORMED / BLOCKED** (entry criteria above) |
| Elimination of Founder manual forwarding (real agents) | **NOT YET** — depends on Gate B entry criteria 1–4 |
| Unattended autonomy | **BLOCKED** — Codex FA-9/CO-10 + FA-PRIV |

## Uncovered gaps + owners
| Gap | Owner | Severity |
|---|---|---|
| Authenticated external round trip + durable provider-evidence ingestion | Cursor/MainAI (#307 line + ingestion wiring) | required for Gate B |
| Live spend-grant + STOP gate on provider mutations | Cursor/MainAI + Founder authorization | required for Gate B |
| FA-9/CO-10 authenticated STOP-clear; FA-PRIV least-privilege | Codex | P1, required for unattended autonomy |
| HD-1 synthetic-origin corroboration | Cursor/MainAI | P3 hardening |

## Reproduce
```
probes/unattended_v1/run.sh ../wtcert-2444bcc   # Gate A UA-1..10 + UA-PI: PASS; Gate B skipped (NOT PERFORMED)
# adversarial reuse: agentbridge_v2, extconn_v1, rai_v1, cr_contract_v1, gh_intake_v1
probes/coord_accept_v1/run.sh ../wtcert-2444bcc # CO-10 FAIL = preserved (Codex)
probes/fa_core_v1/run.sh     ../wtcert-2444bcc  # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
```
