# Interim examination — PR #307 (Real External Agent Connectivity; Cursor Cloud Agents)

- **Candidate SHA:** `8e54c42eb7898971658e4b17a736b5a8901b9e83`. Base: frozen #303 `74ebda9` (verified ancestor). **Parallel to #304–#306** — it does NOT contain the agent-side transport / delivery gateway from #304–#306.
- **Status:** INTERIM. #307 is a **fail-closed prototype** selecting the Cursor Cloud Agents API as the first intended real integration; **there is no authenticated live vendor round trip**, by design.
- **Examiner:** Claude, read-only; executable tests on the real fail-closed client / official HMAC / identity classifier / ledger proposal; **no live paid calls, no network (egress-guarded), no credentials**; `first_cycle_go=false`. Worktree verified unchanged.
- **Delta over #303:** new `agent_coordination/external_connectivity/` (`cursor_cloud.py`, `identity.py`, `ledger.py`, `catalog.py`, `inspect.py`, `__main__.py`), `connect.py` +6, a feasibility doc and a candidate test (7 passed).

## Priority 1 — integration surface vs OFFICIAL documentation
I inspected the actual code and independently checked the official Cursor Cloud Agents API (not the code's word, not speculative endpoints):
- **`POST /v1/agents` (Create Agent, enqueues first run)** — the code's `CREATE_PATH=/v1/agents` matches the official endpoint (public beta; "APIs may change before GA").
- **v0 webhook HMAC** — the code's `verify_v0_webhook` (header `sha256=<hex>`, HMAC-SHA256 over the raw body keyed with the webhook secret, User-Agent `Cursor-Agent-Webhook/1.0`, event `statusChange`, statuses `FINISHED`/`ERROR`) matches the documented signing scheme (Hookdeck / Learn Cursor; confirmed by multiple third-party sources). Honest and correct.
- **Identity vs local credentials** — `identity.classify_credential` + `authorize_provider_key` rigorously separate a vendor API key (`CURSOR_API_KEY`/service account) from a local bridge token (`LIFE_AGENT_BRIDGE_TOKEN` → `local_bridge_token`, blocked `provider_identity_required`) and from this Cloud Agent's own runtime (`CURSOR_AGENT_SOCKET` → `this_cloud_agent_runtime`, blocked). Secret values are never copied into returned structures.
- **Correlation** — `ledger.propose_ledger_event` documents how create (`agent.id`+`run.id`) → follow-up → terminal status would be correlated and independently checkable (`GET /v1/agents/{id}`, run status), but #307 does not perform it.

### PROV-1 (documentation compatibility, P3 / informational) — v1-create vs v0-webhook version assumption
#307 plans to create via **v1** (`POST /v1/agents`) but obtain completion evidence via the **v0 webhook** (`statusChange` HMAC). Vendor-adjacent docs (Port) state v1-create has **no** webhook support and that webhook tracking requires the **v0** create path; the official v1 page lists a Webhooks section but the v1 webhook request format is unconfirmed. The code correctly names it "v0 webhook," and everything is fail-closed, so this is **not exploitable** — but it is an unresolved round-trip assumption to reconcile against the official Webhooks page before any live round trip (either create via v0 to receive webhooks, or confirm v1 emits them). Owner: Cursor (round-trip design).

### PROV-2 (naming/semantics, P3 / informational) — `provider_authenticated` reflects credential CLASS, not a live-authenticated session
`authorize_provider_key` returns `provider_authenticated=True` for an **unvalidated** candidate key (it denotes "a provider-key *class*," disclaimed in the result note), with no live validation. It never flows into a delivery/completion claim — `propose_ledger_event` derives its own `provider_authenticated` from the **webhook HMAC** path and forces it `False` for fixtures. Still, the field name overstates. Recommend renaming to `provider_key_class` / reserving `provider_authenticated` for a verified live probe, so no future consumer mistakes class-membership for a live authenticated vendor. Owner: Cursor. Not a security defect (fail-closed; no live action results).

## Priority 2 — adversarial security (`extconn_v1`, 12 passed, 2 BLOCKED)
| EXT | Scenario | Result |
|---|---|---|
| 1 | forged provider identity | **PASS** — classification implies no live call/delivery (`vendor_delivery_claimed=False`); see PROV-2 for the class-label nuance |
| 2 | local bridge token presented as a vendor credential | **PASS** — `blocked / provider_identity_required` (`identity=local_bridge_token`) |
| 3 | invalid webhook HMAC | **PASS** — `rejected`, `provider_authenticated=False` |
| 4 | webhook replay / duplicate completion | **PASS (verify) / BLOCKED (dedup)** — both HMACs verify (stateless); duplicates map to the SAME deterministic `event_id` so the existing ledger would dedup at ingestion, which #307 does not wire (`persisted=False`) |
| 5 | reordered completion/status events | **BLOCKED** — ordering/monotonicity is the existing ledger's job at ingestion; not wired; not simulated |
| 6 | wrong agent/assignment id | **PASS** — proposal not persisted, `independently_verified=False`; real binding to an `AgentWorkAssignment` is deferred |
| 7 | cross-owner / cross-tenant contamination | **BLOCKED** — verify/propose is provider-side with no owner context; isolation is enforced at ingestion (RAI-07/CS-6), not wired |
| 8 | payload injection + malformed JSON | **PASS** — malformed body with valid HMAC → `accepted` payload `{}` (no crash); injected `independently_verified`/`vendor_delivery_claimed` do NOT propagate (stay `False`) |
| 9 | API auth failure / timeout / network interruption | **PASS (offline-safe) / BLOCKED (live)** — client gates never attempt a live call (`live_http_attempted=False`); live network behavior of the unauthenticated probe not exercised (egress-guarded) |
| 10 | unauthorized paid agent creation / follow-up | **PASS** — `create`/`follow_up` hard-blocked **even with both enable flags set** (`hard_block=True`, `live_http_attempted=False`) |
| 11 | STOP blocking pending provider mutations | **PASS (moot) / documented** — no provider mutation can occur (hard-blocked), so STOP-gating of mutations is moot on #307; authenticated STOP-clear stays Codex CO-10 |
| 12 | false completion without authoritative evidence | **PASS** — unverified → not proposed/persisted; a fixture → `provider_authenticated=False`, `persisted=False` |
| 13 | partial / ambiguous remote state | **PASS** — non-terminal `RUNNING` → `proposed_delivery_status=None`, event status `blocked` (never a completion) |
| 14 | secret exposure via output/diagnostics | **PASS** — neither the API key nor the webhook secret appears in any returned structure |

No simulation of success for anything requiring real provider-side behavior; those are explicitly BLOCKED (EXT-5/7, and the live halves of 4/9).

## Priority 3 — end-to-end evidence contract (`propose_ledger_event` is strictly a proposal — confirmed)
`propose_ledger_event` **never persists** (`persisted=False` always), only proposes for `accepted` evidence, never self-asserts `independently_verified`, and forces `provider_authenticated=False` for fixtures. Required evidence to prove a genuine round trip (none satisfied on #307):
| Claim | Required evidence | #307 |
|---|---|---|
| A real external agent was authenticated | a live authenticated call under a Founder/service-account vendor key (not a class label) | **BLOCKED** (PROV-2; no live call) |
| The actual agent received a specific assignment | `POST /v1/agents` response `agent.id`+`run.id`, bound to an existing `AgentWorkAssignment` | **BLOCKED** (create fail-closed) |
| The provider accepted/executed authorized work | `GET /v1/agents/{id}/runs/{runId}` in CREATING/RUNNING under the provider key | **BLOCKED** |
| A completion belongs to the correct assignment | v0 HMAC `statusChange` FINISHED/ERROR for that `agent.id` + independent `GET` | **PARTIAL** — HMAC verified; binding + ingestion not wired |
| MainAI persisted a trustworthy completion record | ingestion into the durable ledger via `persist_owner_ledger` | **BLOCKED** (`propose` only, never persists) |
| Duplicate/stale events can't change a settled result | ledger `processed_event_ids` + status-rank at ingestion | **BLOCKED** (deterministic event_id ready, ingestion not wired) |
| Founder STOP + spend remain effective | STOP gate + spend grant on provider mutations | **PARTIAL** — mutations hard-blocked (spend can't be reached); STOP-on-mutation gate not wired; authenticated STOP-clear = Codex CO-10 |

## Priority 4 — release separation
| Scope | Status | Basis |
|---|---|---|
| **A. Provider API feasibility** | **PASS (feasible, documented)** — official `POST /v1/agents` + v0 webhook HMAC confirmed against vendor docs; PROV-1 v1-vs-v0 webhook assumption to reconcile | web-verified + code |
| **B. Local prototype correctness** | **PASS** — fail-closed gates, identity separation, HMAC, proposal-only ledger, no secret leakage; `extconn_v1` 12/2, candidate 7/7, no regression | this exam |
| **C. Authenticated real-vendor round trip** | **BLOCKED / NOT DEMONSTRATED** — no live authenticated call; fixtures are not deliveries | by design |
| **D. Elimination of Founder manual forwarding** | **NO** | depends on C |
| **E. Unattended autonomy** | **BLOCKED** | Codex FA-9/CO-10 + FA-PRIV (preserved: CO-10 fail, FAC-1..5 fail) |

## Currency (no regression on the #303 baseline)
gh_intake **8** (GH-4/GH-4b), RAI **24/3**, cr_contract **19/2**, substrate **20**; Codex blockers preserved. (agentbridge_v2 / connect_v1 / delivery_v1 target #304–306 code absent on this parallel branch — N/A here, not a gap.)

## Interim verdict
**#307 is a correct, honest fail-closed connectivity prototype (Scope A feasible, Scope B correct).** It never claims a vendor PASS, never spends quota, never treats a local token as provider identity, keeps `propose_ledger_event` a strict proposal, and leaks no secrets. **No live external round trip is demonstrated (Scope C BLOCKED), so it does not advance C/D/E.** Two P3/informational findings (PROV-1 v1-vs-v0 webhook assumption; PROV-2 `provider_authenticated` naming) — neither a blocker. **No RETURN_TO_BUILDER blocker found for the prototype's stated scope.** A final PASS/FREEZE issues only once Cursor freezes an exact SHA with both workflows green; and Scope C certification requires the real authenticated round-trip evidence above (which needs Founder authorization + a live provider key — out of examiner scope).

## Precise requirements for a genuinely verified external round trip
1. Create under a Founder/service-account **vendor** key (not a class label) → capture `agent.id`+`run.id`; bind to a real `AgentWorkAssignment`.
2. Receive the **v0 webhook** `statusChange` (reconcile PROV-1: create on the path that actually emits webhooks) AND independently confirm via `GET /v1/agents/{id}` under the same key — two-source evidence.
3. Ingest through the existing durable ledger (`persist_owner_ledger`) so `processed_event_ids` + status-rank give replay/reorder safety and owner isolation (closes EXT-4/5/6/7).
4. Keep mutations behind the Founder STOP + spend grant; keep independent examiner verification mandatory (builder completion ≠ certification).
5. `provider_authenticated` must mean a verified live probe, not a credential class (PROV-2).

## Reproduce
```
probes/extconn_v1/run.sh ../wtcert-8e54c42   # EXT-1..14: 12 passed, 2 BLOCKED (EXT-5/7 deferred to ingestion)
probes/gh_intake_v1/run.sh ../wtcert-8e54c42 # 8 ; rai_v1 24/3 ; cr_contract_v1 19/2 ; substrate_v1 20
probes/coord_accept_v1/run.sh ../wtcert-8e54c42  # CO-10 FAIL = preserved (Codex)
probes/fa_core_v1/run.sh ../wtcert-8e54c42       # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
# candidate: pytest tests/backend/test_external_agent_connectivity.py -> 7 passed @ 8e54c42
# official API: POST /v1/agents (public beta); v0 webhook sha256= HMAC, UA Cursor-Agent-Webhook/1.0 (cursor.com docs + Hookdeck/Learn Cursor)
```
