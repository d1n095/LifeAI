# Provider-neutral composition contract — agent-side bridge (#306) × provider adapter (#307)

Independent analysis of exactly what must compose for a real unattended external cycle. #307 (`f24ccf2`) is parallel on
frozen #303; #306 (`2444bcc`) carries the certified agent-side transport. The branches are NOT merged here, no production
is modified, and the integration does not yet exist — this is the contract it must satisfy.

## Component ownership (symbol cross-check, verified)
| Layer | Symbol | #306 | #307 | Shared #303 base |
|---|---|---|---|---|
| Agent-side runner | `agent_coordination.agent_side.run_agent_side` | ✅ | — | — |
| Agent-side transport | `agent_coordination.transports.gateway.AuthorizedGatewayTransport` | ✅ | — | — |
| Provider adapter | `external_connectivity.cursor_cloud` (+ identity, catalog, inspect) | — | ✅ | — |
| Provider verdict/ledger | `external_connectivity.verdict.capability_verdict`, `ledger.propose_ledger_event` | — | ✅ | — |
| Delivery gateway | `delivery.submit_receipt` / `prepare_message` / `claim_message` | ✅ | ✅ | ✅ |
| Durable coordinator | `durable.DurableCoordinationService`, `persist.persist_owner_ledger` | ✅ | ✅ | ✅ |

## Interface compatibility
No **incompatible** interface was found: both branches build on the same #303 `delivery`/`persist`/`durable`/`lifecycle`
contracts. The gap is **absence**, not conflict.

## Missing surfaces the composition must add (none exists on either branch)
1. **`ingest_provider_event(db, *, owner_id, verified_event)` → durable ledger.**
   `#307.ledger.propose_ledger_event` returns a proposal (`persisted=False`). `#306.delivery.submit_receipt` is
   **recipient/bridge-inbox-scoped** (keyed to a local gateway `message_id`), not a provider `agent.id`. Required: a
   function that takes a *verified* provider event (HMAC `accepted`, `provider_authenticated=True`, non-fixture) and
   records a durable delivery completion via `persist_owner_ledger`, idempotent on the provider event id.
2. **Identity binding `provider agent.id ↔ AgentWorkAssignment`.**
   `POST /v1/agents` returns an `agent.id`+`run.id`; nothing maps that to the owning `AgentWorkAssignment`. Required: a
   stored, owner-scoped binding (new column or provenance key) set at dispatch and checked at ingestion, so a webhook for
   `agent.id` resolves to exactly one assignment under the correct owner (closes `extconn` EXT-6/EXT-7).
3. **Missing state transition:** `verified_provider_event → delivery.delivered/acknowledged/completed` on the bound
   assignment, re-using the existing delivery state machine and `processed_event_ids` for replay/reorder safety
   (closes `extconn` EXT-4/EXT-5 at ingestion).
4. **Dispatch → provider create wiring:** the dispatch gate (`dispatch.evaluate_dispatch_readiness`, #303/#306) must gate
   a `cursor_cloud.create_agent` call (today hard-blocked) behind Founder approval + spend grant + STOP, and record the
   returned `agent.id` via surface (2).

## Persistence requirements
- The provider binding and the ingested completion must live in the **durable** store (`AgentWorkAssignment.provenance`
  / the `durable_coordination` dump), restart-safe (reuse the #303/#306 reload + status-rank merge).
- Replay/idempotency must key on the **provider event id** (deterministic `cursor-cloud-{agent.id}-{status}` already
  produced by `propose_ledger_event`) via the existing `processed_event_ids`.
- HD-1 corroboration (synthetic-origin) should land first so provenance-based binding cannot be subverted (P3).

## Provider-neutrality invariants the composition must preserve
- The ingestion/binding must be keyed by a provider-neutral identity (`cursor`/`claude`/`codex`), not hardcode a vendor;
  the adapter stays replaceable (`verdict.provider_cannot_redefine_authority=True`, `replaceable_adapter=True`).
- An adapter must never set `independently_verified`, `vendor_delivery_claimed`, STOP, spend or readiness (GBV-5 / RAI /
  CR enforce this at the layers that exist; the new ingestion surface must inherit the same `FORBIDDEN_RECEIPT_KEYS`
  discipline).
- Independent examiner verification stays mandatory; builder completion (even a real provider FINISHED) ≠ certification.

## Acceptance for the composed surface (what the examiner will test when it exists)
On a composed frozen SHA: `agentbridge_v2` + `rai_v1` + `cr_contract_v1` + `extconn_v1` must stay green; `extconn` EXT-4/
EXT-5/EXT-6/EXT-7 flip from BLOCKED to PASS (replay/reorder/wrong-id/cross-owner enforced at ingestion); a real round
trip flips RAI-01/03/22/27; `gateb_v1` GBP prerequisites 1–6 flip from BLOCKED/NOT_IMPLEMENTED to PASS; and Gate A
(`unattended_v1`) runs green on the composed stack. Codex FA-9/CO-10 + FA-PRIV must be closed for unattended autonomy.
