# Durable Provider-Event Ingestion — acceptance contract (Priority 1)

Required chain: **verified provider event → correct owner/assignment → durable lifecycle evidence → independent review
→ authorized continuation.** On #307 `f24ccf2` the ingestion/binding surface does NOT exist (`ledger.propose_ledger_event`
is proposal-only; no `ingest_provider_event`). `pevi_v1` is test-first: it proves what is enforceable today and marks the
missing legs honestly (never a vendor PASS on a fixture). Result: **6 passed, 3 skipped (NOT_IMPLEMENTED/BLOCKED)**.

## Evidence matrix (`pevi_v1`)
| PEVI | Chain property | Status | Evidence |
|---|---|---|---|
| 1 | authentication — invalid HMAC never enters the chain | **PASS** | `bad_hmac=rejected`, `proposed=False`, `persisted=False` |
| 2 | identity correlation (provider `agent.id` ↔ owner/assignment) | **NOT_IMPLEMENTED** | `ingest_provider_event` absent |
| 3 | webhook replay — deterministic event_id enables dedup; no double-persist today | **PASS** | same `event_id`, `persisted=False` |
| 4 | event reordering — monotonic/no-regression at ingestion | **BLOCKED** | needs ledger ingestion (status-rank); proven for the substrate by CR-13/SUB-02 |
| 5 | concurrent ingestion | **NOT_IMPLEMENTED** | no ingestion surface to race |
| 6 | stale authorization — a valid HMAC is NOT authorization | **PASS** | HMAC `accepted` but `provider_authenticated=False`, `persisted=False` |
| 7 | cross-owner contamination | **BLOCKED** | no owner binding at ingestion; enforced for the local gateway by RAI-07/CS-6 |
| 8 | worker crash / persistence / recovery | **NOT_IMPLEMENTED** | nothing provider-side persisted to recover |
| 9 | provider-reported completion ≠ independently verified | **PASS** | FINISHED → `independently_verified=False`, `external_agent_connected=False` |

**Provider-reported vs independently-verified** is firmly distinguished today (PEVI-6/9): a signed webhook authenticates
origin, not that the work was done or that the provider key is still authorized; a provider FINISHED is never
independent verification. No local fixture yields a persisted verified completion.

## Minimal implementation contract (Cursor/MainAI) — flips PEVI-2/4/5/7/8 to PASS
1. **`ingest_provider_event(db, *, owner_id, verified_event)`** — accept only a `verify_v0_webhook`/run-status result with
   `outcome=accepted`, `provider_authenticated=True`, `fixture=False`; reject everything else. Idempotent on the
   deterministic provider `event_id` via the existing `processed_event_ids`.
2. **Owner/assignment binding** — resolve the provider `agent.id` to exactly one `AgentWorkAssignment` under the correct
   owner (binding stored at dispatch); a webhook for an unknown/foreign `agent.id` or owner is rejected (closes PEVI-2/7).
3. **Durable lifecycle evidence** — record the completion through `persist_owner_ledger` + the delivery state machine
   (`available→…→completed`), restart-safe; reorder/replay safety from status-rank + processed_ids (closes PEVI-4/8).
4. **Independent review + authorized continuation** — the ingested completion is a builder result, routed to a non-builder
   examiner (reuse the lifecycle follow-up / UA-4); it never sets `independently_verified`; continuation only after an
   examiner verdict and within the Founder spend/STOP grant (PEVI-9 stays green).

## Acceptance when implemented
On the composed frozen SHA: `pevi_v1` PEVI-2/4/5/7/8 flip to PASS (bind, reorder, concurrent, cross-owner, crash-recover);
PEVI-1/3/6/9 stay green; `extconn_v1` EXT-4/5/6/7 flip to PASS at ingestion; a real round trip flips RAI-01/03/22/27; and
Gate A (`unattended_v1`) runs green on the composed stack. No fixture ever yields a vendor PASS.
