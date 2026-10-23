# Certification — PR #302 (Agent Messaging & Delivery Gateway)

- **Candidate:** PR #302 (draft, frozen for independent examination), on #301.
- **Examined SHA:** `ff28b51429a5bc61f78308b657b8d57b2e5bf152`. Base #301 `a786dac9f32bc3120fa2176e18041a75f1cf6c56` (verified ancestor; 2 commits on top).
- **CI:** user reports both exact-SHA workflows successful (push `39910420698`, pull request `39910425753`). I could **not** independently re-read the 12 checks in this environment (no valid GitHub credentials here); per item 8 green CI is not certification anyway, so this verdict rests on independent code + test evidence against the frozen SHA.
- **Delta over #301 (1,377 insertions):** `agent_coordination/delivery.py` (616, new — the gateway), `local_bridge.py` (125, new — `LocalAuthorizedBridge`), `routers/agent_gateway.py` (123, new — recipient inbox/claim/receipt), `durable.py` +45 (delivery state persisted in `dump_for`/merge), `routers/mainai_founder_ops.py` +31 (Founder-only `POST /agent-gateway/bridges`), `worker.py` +15 (`reconcile_pending_deliveries` every tick), snapshot +7, candidate test (336), frontend founder-ops +44.
- **Examiner:** Claude, read-only. The ASGI app, worker, DB, gateway routes, bridge, persistence and Founder read are all production code; the bridge is driven over the real routes via the injected `request` callable. No paid calls, no GitHub writes, no personal credentials; the frozen worktree is verified unchanged after every suite.

---

## A. Exact-SHA code — **RETURN_TO_BUILDER** (composed stack; #302's own delta is clean)

#302's own 1,377-line delta introduced **no new defect** I could find: the gateway is correct and well-hardened across my independent `delivery_v1` DLV-1..6 and the candidate's own 5/5 suite. The composed stack is RETURN_TO_BUILDER for **one inherited, in-scope residual** (item 9), which #302 does not touch and does not claim to fix.

### Inherited DEF-1 / GH-4 — still open on #302 (item 9; **not** silently classified as fixed)
`lifecycle.py::_persist_ledger` (and `delivery.py::persist_delivery`) still open with `if not assignments: return`. Re-tested on the frozen SHA:
- **GH-4b (PASS):** a PR's 2 obligations both survive restart (`created=2 survived_restart=2`).
- **GH-4 (FAIL):** two `github-intake/refresh` HTTP calls for the same PR with **no pre-existing `AgentWorkAssignment`** return different obligation ids each time (`idempotent=False`) — not durable/idempotent across refresh+restart when discovery starts from zero rows.

This is the same gap flagged on #300/#301; **owner Cursor; do not open a duplicate ticket.** It is isolated to github-intake discovering a PR with no workforce assignment — every delivery/coordination obligation in the runtime has a backing row and persists correctly (DLV-1/4). Fix is small: ensure/create a durable backing row (or a ledger-owned row) when none exists.

### #302's own gateway code — verified correct (`delivery_v1` DLV-1..6, all pass)
| Item | DLV | Result |
|---|---|---|
| 1 one durable, idempotent, scoped inbox per obligation | DLV1 | PASS — 3 repeated worker reconciles ⇒ exactly **1** message; correct recipient/obligation_id/`{id}:inbox` key/expiry |
| 3 recipient isolation + cross-agent claim over real HTTP | DLV2 | PASS — cursor token's inbox lists only cursor; claiming claude's `message_id` over HTTP ⇒ **403** |
| 2,3 forged token / replayed claim / fabricated receipt over HTTP | DLV3 | PASS — anon/forged/tampered token ⇒ 401; replayed claim ⇒ `replay_ignored`; receipt with `override_stop`+`github_write`+`independently_verified` ⇒ **403** |
| 4,5 real bridge round trip, true fresh-session restart, no double-exec | DLV4 | PASS — issue→transmit→pull→ack→execute→complete; a brand-new sessionmaker session sees `completed`, and a duplicate completion `event_id` ⇒ `replay_ignored` |
| 6 disconnected / expired / malformed / oversized | DLV5 | PASS — no bridge ⇒ `blocked`+`local_authorized_bridge`; expired ⇒ `failed`; 9000-char instructions ⇒ `rejected`; unknown kind ⇒ `rejected` |
| 8,11,12 transport honesty, no false certification | DLV6 | PASS — bridge `usable` only after a live credential; all three vendor-messaging channels `usable=False`; `delivery_claimed=False` |

Structural hardening confirmed by reading `delivery.py`: HMAC-signed, recipient-scoped, TTL'd, revocable bridge tokens (hash-compared on resolve); `FORBIDDEN_RECEIPT_KEYS` rejects `independently_verified`/`first_cycle_go`/`github_write`/`override_stop`/`stop`/`personal_credentials`; identity spoof rejected; `processed_ids` replay set is persisted and unioned on reload; state machine forbids skipping (`prepared`→`available`→`claimed`→`delivered`→`acknowledged`→`executing`→`completed`); `vendor_delivery_claimed:False` on every surface.

Reproduce: `probes/delivery_v1/run.sh ../wtcert-ff28b51` (6 passed); `probes/gh_intake_v1/run.sh ../wtcert-ff28b51` (GH-4 FAIL residual, GH-4b PASS).

---

## B. Internal coordination & durable delivery — **PASS** (with the inherited GH-4 caveat from A)

An obligation deterministically yields exactly one durable, recipient-isolated inbox message with identity/scope/provenance/expiration/idempotency (DLV1). The inbox row is persisted into `AgentWorkAssignment.provenance` via `dump_for`→`persist_to_assignment` and reloaded via the merge path (messages last-writer-wins, `processed_ids` unioned, bridges merged) — so inbox state, receipts and replay-protection survive worker and process restarts (DLV4, candidate restart test). A prepared/available message is never reported as delivered, acknowledged or executed without the real transitions (DLV3/DLV1; `inbox_created_is_not_delivered:True`). The worker auto-runs `reconcile_pending_deliveries` every `run_once`, so the Founder does not trigger each delivery. The only durability gap is the inherited github-intake GH-4 (A), which does not affect the delivery path.

---

## C. Real local bridge round trip — **PASS**

DLV4 + the candidate's `test_real_bridge_round_trip_three_agent_restart` both drive `LocalAuthorizedBridge` over the **production** agent-gateway routes (real app, worker, DB): Founder issues a scoped token → worker prepares+transmits → `bridge.pull()` claims and receives the instructions → `acknowledge` → `report_execution` (with evidence) → `complete`; the obligation advances to completed, the state survives a restart, and a replayed completion is ignored. `independently_verified` and `vendor_delivery_claimed` stay `False` throughout. A genuine message-delivery-and-acknowledgment round trip is demonstrated end to end.

---

## D. Real Cursor/Claude/Codex delivery — **BLOCKED** (honestly reported; no invented access)

The `local_authorized_bridge` is the one executable transport, but it **pulls this owner's own inbox over the gateway**; it is not a connection to the Cursor, Claude or Codex vendor services. `transport_inventory` marks it `usable` only when a live Founder-issued credential exists, and marks all three `*_vendor_messaging` channels `usable=False` with `missing_capability=agent_messaging_adapter` (DLV6). No vendor API is assumed to exist.

**Precise missing piece (item 12):** to actually deliver to and receive evidence from a real agent, that agent must itself run the `LocalAuthorizedBridge` client (an **agent-side runner**, not yet shipped) against the gateway using a **Founder-issued scoped token that the Founder must generate and hand to it** (Founder-only authorization). There is no path that reaches the three vendor agents without one of: (a) a vendor messaging adapter (does not exist), or (b) each agent operating the local bridge with a Founder-issued token. Until an agent runs the bridge, its inbox message correctly sits `blocked`/`local_authorized_bridge`.

---

## E. Founder handover readiness — **BLOCKED / NO** (based on real capability + safety, not a demo)

The Founder **cannot yet** stop manually transferring work to the three real agents, because D is BLOCKED (no agent receives an instruction unless it runs the authorized bridge), the inherited GH-4 is open (A), and the Codex safety blockers remain open (item 10). What **is** real today: for any recipient that runs the authorized bridge, MainAI durably prepares, transmits, delivers, and ingests acknowledgment/execution/completion receipts automatically and restart-safely, and refuses forged/replayed/cross-agent/privilege-escalating events — so the Founder no longer transcribes those messages by hand.

### Shortest concrete path to operational handover
Already implemented & independently verified (do not rebuild): durable isolated inboxes; per-obligation idempotency; recipient-scoped bridge auth; forgery/replay/cross-agent/fabrication rejection; genuine bridge round trip; restart-safe persistence + duplicate-execution prevention; worker auto-reconcile of delivery; transport honesty; no false certification.

Remaining dependencies, with non-duplicated owners:
1. **Agent-side bridge runner — Cursor/MainAI (technical, the one remaining coordination piece).** Package `LocalAuthorizedBridge` as a loop each real agent runs (pull→ack→execute→report→complete). The gateway side is done.
2. **Founder token issuance + secure hand-off — Founder (permission).** Issue one scoped token per agent (`POST /founder-ops/agent-gateway/bridges`, exists) and deliver it to each agent's runner. No vendor API required.
3. **Inherited DEF-1 / GH-4 — Cursor (technical).** Close the zero-backing-row idempotency gap. Same task as #300/#301; not a new ticket.
4. **Codex safety — Codex (permission/safety), preserved & confirmed still open:** CO-10/FA-9 (STOP-clear gated only by a `founder_ack` string; `coord_accept_v1` CO-10 FAILS); FA-PRIV (runtime role `mainai_app` can still `UPDATE`/`DELETE` users/audit_log/authority/envelope/spend; `fa_core_v1` FAC-1..5 + `security_kernel_v1` FA-PRIV FAIL). #302 correctly does not touch these.

Items 1–3 are coordination/durability; item 4 is safety. None needs GitHub writes, vendor APIs, or Founder file transcription.

---

## Item-by-item (the 12 requirements)
1. **One durable scoped idempotent inbox per obligation** — VERIFIED (DLV1).
2. **Never falsely delivered/acknowledged/executed** — VERIFIED (DLV1/DLV3; state machine + `inbox_created_is_not_delivered`).
3. **Authenticated inbox/claim/receipt; reject forged/cross-agent/replay/fabrication** — VERIFIED (DLV2/DLV3 over real HTTP + candidate forged-identity test).
4. **`LocalAuthorizedBridge` round trip through real app/worker/DB** — VERIFIED (DLV4 + candidate).
5. **Receipt persistence + duplicate-execution prevention across restart** — VERIFIED (DLV4 true fresh session: `completed` persists, duplicate completion `replay_ignored`).
6. **Disconnected/expired/missed-ack/malformed/malicious/rate-limit/interrupted** — VERIFIED (DLV5 + candidate; endpoints rate-limited via `limiter`; missed-ack stays `delivered`).
7. **Three-agent production path, one builder done, two active, next bridge accepts, receipt ingested, one review obligation** — VERIFIED (candidate `test_real_bridge_round_trip_three_agent_restart`, 5/5: cursor `available`→round trip→completed; claude+codex `blocked`; exactly one examiner obligation; examiner message `blocked`).
8. **Builder completion / green CI / self-receipt never ⇒ independent certification** — VERIFIED (DLV3/DLV6; `independently_verified:False`; forbidden keys rejected; green-CI-not-certification preserved from #301).
9. **Re-test DEF-1/GH-4 (zero rows)** — DONE, **still FAIL** (GH-4 `idempotent=False`); not classified as fixed. → A.
10. **DB privilege boundaries, STOP, authorization; preserve FA-9/CO-10 + FA-PRIV** — VERIFIED preserved (CO-10 + FA-PRIV/FAC fail as required); delivery boundary refuses `stop`/`override_stop` (DLV3).
11. **Verify every transport-availability claim** — VERIFIED (DLV6; local bridge usable only with a live credential; vendor messaging `usable=False`).
12. **Can any available integration actually reach the 3 agents?** — NO (D). Precise missing: agent-side bridge runner + Founder-issued scoped token per agent; no vendor API.

## Reproduce
```
probes/delivery_v1/run.sh       ../wtcert-ff28b51   # DLV-1..6 (6 passed) — gateway over the real app/worker/bridge
probes/gh_intake_v1/run.sh      ../wtcert-ff28b51   # GH-4b PASS, GH-4 FAIL (inherited DEF-1 residual, item 9)
probes/coord_accept_v1/run.sh   ../wtcert-ff28b51   # CO-10 FAIL = preserved STOP-clear blocker
probes/fa_core_v1/run.sh        ../wtcert-ff28b51   # FAC-1..5 FAIL = preserved FA-PRIV
probes/security_diag_v1/run.sh  ../wtcert-ff28b51   # 10/10 (no regression)
# candidate's own production-path suite (in target): pytest tests/backend/test_agent_messaging_delivery_gateway.py -> 5 passed @ ff28b51
```
