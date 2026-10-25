# Independent verdict — PR #307 `f24ccf2` (capability verdict + Gate-B readiness)

- **Candidate SHA:** `f24ccf2b6c632a96f7d35bf540593d0548040f4b` (linear advance over the previously-examined `8e54c42`). Base: frozen #303 `74ebda9`; **parallel to #304–#306** (no agent-side transport on this branch).
- **Delta:** `external_connectivity/verdict.py` (new `capability_verdict()`, `vertical_slice()`, `unattended_trial_plan()`), `connect.py`/`inspect.py` wiring (handover view now carries `unattended_operating_cycle_ready=False`), `UNATTENDED_OPERATING_CYCLE.md`, candidate test (+50). Cursor reports the real external cycle **NOT READY** — I independently confirm that.
- **Examiner:** Claude, read-only; no live/paid calls, no network, no credentials. `first_cycle_go=false`. Codex FA-9/CO-10 + FA-PRIV preserved.

## Priority 1 — the new capability verdict is honest and non-flippable (`gateb_v1` GBV-1..6, 6/6)
`capability_verdict()` is a **fail-closed, nullary** declaration (`gateb_v1` evidence):
- **GBV-1** `ready=False`, `unattended_operating_cycle_ready=False`, `unattended_autonomy_ready=False`, `external_agent_connected=False`; **every** requirement `satisfied=False`.
- **GBV-2** `local_gateway_sufficient=False`, `green_ci_sufficient=False`, `disconnected_prototype_sufficient=False` — a local gateway / green CI / disconnected prototype can never turn readiness on.
- **GBV-3** blocking gates include `fa9_co10_privilege_and_publication`, `founder_cursor_cloud_live_mutate_authorization`, `independent_certification`, `first_cycle_go_false`, `no_provider_authenticated_external_agent` — missing STOP-clear / publication / spend / identity authority keeps readiness BLOCKED.
- **GBV-4** one valid provider webhook event is a **proposal only** (`persisted=False`, `independently_verified=False`); it does not certify the multi-step cycle.
- **GBV-5** injected `independently_verified`/`ready`/`vendor_delivery_claimed`/`satisfied` in a webhook payload never propagate — no adapter alters trusted/readiness classification via an untrusted field.
- **GBV-6** `capability_verdict()` takes **no arguments** and is deterministic — no caller-supplied (forged) input can set `ready`.

**Observation (not a defect):** the verdict is a *static* honest declaration, not an *evidence evaluator* — it asserts `False` rather than computing each requirement from live state. That is correct and safe for #307's NOT-READY purpose (it cannot be wrong in the False direction). A real readiness gate must eventually COMPUTE each requirement from evidence while staying fail-closed — which is exactly what my independent preflight does (below), and what the composition work must wire. No executable classification boundary was left uncovered: GBV-1..6 lock every boundary the Founder named.

## Priority 2 — independent evidence-driven Gate-B preflight (`gateb_v1` GBP, read-only, provider-neutral)
My examiner-owned preflight classifies the 12 prerequisites from real evidence on this branch — never manufacturing live evidence, never requiring a credential. **Corrected tally (Priority-3 reconciliation): exactly 12 prerequisites summing to 12 — PASS 4 · BLOCKED 6 · NOT_IMPLEMENTED 1 · NOT_TESTED 1.** Two AUXILIARY findings are reported separately and are NOT prerequisites: a composition FAIL (agent-side runner absent on this #303-based branch) and the product `verdict.ready` summary (BLOCKED). (The earlier draft folded those 2 aux rows into the tally, yielding a misleading 13/14; corrected here and enforced by `gateb_v1` which asserts `PREREQ_COUNT==12` and the tally sums to 12.)

| # | Prerequisite | Status | Owner |
|---|---|---|---|
| 1 | authenticated external agent identity | **BLOCKED** | Founder (live key) + Cursor |
| 2 | actual assignment receipt | **BLOCKED** | Founder (live-mutate) + Cursor |
| 3 | authorized execution | **BLOCKED** | Founder + Cursor |
| 4 | durable provider-event ingestion | **NOT_IMPLEMENTED** | Cursor/MainAI |
| 5 | evidence-to-assignment correlation | **BLOCKED** | Cursor/MainAI |
| 6 | independent completion verification | **BLOCKED** | Cursor/MainAI + examiner |
| 7 | automatic examiner handoff | **PASS** (local mechanism) | present |
| 8 | findings-to-correction routing | **PASS** (status machine) | present |
| 9 | restart/interruption recovery | **PASS** (durable reload) | present (local) / Cursor (provider-side) |
| 10 | STOP / spending / publication authority | **BLOCKED** | Codex (STOP/publication) + Founder (spend) |
| 11 | independent certification of composed release | **NOT_TESTED** | Founder (freeze) + examiner |
| 12 | Founder notification / intervention | **PASS** (snapshot) | present (local) |
| — | agent-side runner present on this branch | **FAIL** (composition) | Founder/MainAI |

No live-provider / Codex-authority prerequisite is PASS without live evidence (the suite asserts this). The real external operating cycle is **NOT READY**.

## Priority 3 — provider-neutral composition contract (see also COMPOSITION_CONTRACT.md)
Symbol cross-check proves the composition boundary:
| Symbol | #306 (agent-side) | #307 (provider adapter) |
|---|---|---|
| `agent_side.run_agent_side`, `transports.gateway.AuthorizedGatewayTransport` | **present** | absent |
| `external_connectivity.verdict/ledger.propose_ledger_event` | absent | **present** |
| `persist.persist_owner_ledger`, `delivery.submit_receipt` | present | present (shared #303 base) |

A real unattended cycle requires BOTH: #306's agent-side transport **and** #307's provider adapter, over the shared #303 delivery/persist. **Missing integration surface (the critical gap):** no function ingests a *verified provider event* into the durable ledger bound to an `AgentWorkAssignment`. `#307.ledger.propose_ledger_event` is proposal-only (`persisted=False`); `#306.delivery.submit_receipt` is **bridge-inbox/recipient-scoped**, keyed to a local gateway message, not a provider `agent.id`. So there is a missing state transition `verified_provider_event → delivery.delivered/completed on the owning AgentWorkAssignment`, and a missing **identity binding** `provider agent.id ↔ AgentWorkAssignment`. Neither branch has `ingest_provider_event`. These are the concrete composition requirements; no interface is *incompatible*, but the binding+ingestion surface is *absent*.

## Priority 4 — Founder-babysitting acceptance (mapping; real-agent legs need Gate B)
| Founder-experience requirement | Local evidence (Gate A) | Real-agent (Gate B) |
|---|---|---|
| Zero routine manual message forwarding | UA-1 (MainAI auto-prepares/transmits; no Founder step) | BLOCKED (needs live receipt) |
| Automatic reassignment after recoverable failure | UA-8 (stale lease reclaimed) | BLOCKED (provider-side retry) |
| Bounded retries, no duplicate execution | UA-7 + `unattended_trial_plan.temporary_failure` | BLOCKED (bounded retry on 5xx/timeout, no replan storm) |
| Automatic independent review + correction cycles | UA-4 + UA-5 | BLOCKED (external completion to review) |
| Escalation only for actual authorization / unrecoverable | UA-10 (`privilege_rejected`); dispatch APPROVAL_REQUIRED | holds; extends to provider mutate |
| Clear summaries of completed work + outstanding approvals | UA-12 / Founder-ops snapshot | BLOCKED (external-cycle exceptions) |
| No repetitive approval for valid narrowly-scoped grants | dispatch approval gate honors an existing grant; spend grant bounds cost | BLOCKED (live spend grant) |

**Acceptable authorization request vs unnecessary supervision:** a request is *acceptable* when it crosses a real authority boundary — STOP-clear (authenticated), a new/!expanded spend grant, publication, or a sensitive op with no covering grant. It is *unnecessary supervision* when it asks the Founder to forward routine progress, re-approve an action already covered by a valid narrowly-scoped grant, or relay a message between agents. Gate A shows the routine path needs no Founder step; only the authority-boundary crossings escalate.

## Verdict
**#307 `f24ccf2`: the capability verdict is CORRECT and honestly NOT READY; no new blocker, two prior P3 informational findings stand (PROV-1 v1-vs-v0 webhook, PROV-2 `provider_authenticated` naming).** It proves official-interface feasibility (Scope A) and a correct fail-closed prototype (Scope B); it does **not** connect an external agent, complete a verified cycle, or authorize a 72-hour trial (Scope C/D/E BLOCKED). Gate B remains **NOT PERFORMED**. The inherited zod release blocker is correctly flagged on this #303-based branch (do not suppress; do not mix the #305 patch here). Final PASS/FREEZE awaits a frozen SHA + green CI; Scope-C certification awaits the real round-trip evidence (Founder-authorized live key — out of examiner scope).

## Shortest critical path to a real unattended trial (blocker → owner)
1. **Compose #304–#306 (agent-side) + #307 (provider adapter)** on one release line, reusing #303 delivery/persist — **Founder/MainAI** (branch composition decision) + **Cursor** (engineering).
2. **Build `ingest_provider_event`**: bind a verified provider `agent.id` to an `AgentWorkAssignment` and record a durable delivery completion via `persist_owner_ledger` (closes preflight #4/#5/#6, `extconn` EXT-4/5/6/7) — **Cursor/MainAI** (engineering).
3. **Founder live authorization**: scoped Cursor Cloud key, `LIFE_CURSOR_CLOUD_AGENTS_LIVE_MUTATE`, bounded spend grant, webhook ingress secret — **Founder** (setup; out of examiner scope).
4. **Codex close FA-9/CO-10 + publication + FA-PRIV** — **Codex** (P1, required for unattended autonomy).
5. **Examiner certifies the composed frozen SHA** (Gate A green on it + RAI-01/03/22/27 flipped by a real round trip) — **examiner**, on a frozen candidate.
6. Then run the **72h trial** against `unattended_trial_plan.required_coverage` — **Founder-authorized**, not before.

### Engineering vs explicit Founder setup
- **Engineering (Cursor/MainAI):** steps 1–2 (composition + ingestion/binding), the evidence-computing readiness gate.
- **Codex:** step 4 (STOP/publication/privilege).
- **Explicit Founder setup:** step 3 (live key + live-mutate + spend grant + webhook secret) and the branch-composition + freeze decisions.

## Reproduce
```
probes/gateb_v1/run.sh ../wtcert-f24ccf2   # GBV-1..6 + GBP preflight: 7 passed (verdict fail-closed; preflight matrix)
# composition cross-check: agent_side/transports on #306 only; external_connectivity on #307 only; persist/delivery on both
probes/coord_accept_v1/run.sh ../wtcert-f24ccf2  # CO-10 FAIL = preserved (Codex)
probes/fa_core_v1/run.sh     ../wtcert-f24ccf2   # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
# candidate: pytest tests/backend/test_external_agent_connectivity.py @ f24ccf2
```
