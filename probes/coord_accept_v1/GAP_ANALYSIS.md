# MainAI Agent Coordination, Task-State Integrity & Operational Memory — independent audit

- **Audited SHA:** `2aeaa00efe4ffe4c5264b725d28e2e97b3bfa7f0` (#296, latest accessible).
- **Method:** executable suite `coord_accept_v1` (CO-1..CO-10) against the real task/event/job/assignment/verification/liveness mechanisms. Providers and egress blocked (no paid calls). PASS = enforced by real code; FAIL = real code wrong; MISSING/BLOCKED = no mechanism (never PASS).
- Reuses the Security Kernel (FA-*), app-readiness (AR-*) and FO-6 findings; does not duplicate them.

## Inventory (what actually exists)

| Mechanism | Module | Covers |
|---|---|---|
| Task state + liveness | `mainai_execution/liveness.py`, `mainai_founder_ops/status.py` | assigned/acknowledged/running/blocked/completed/failed; idle/running/stalled/dead |
| Dedup guards | `mainai_founder_ops/assignment_guard.py` | duplicate assignment, duplicate paid dispatch, dependency cycle |
| Idempotency | `idempotency_key` on jobs/delegation | at-least-once event safety |
| Cross-SHA receipts | `mainai_verification_registry/service.py` | builder ≠ examiner independent verification |
| No-fabricated-progress | `mainai_runtime_contract.py`, `mainai_founder_ops/communication.py` | chat cannot claim a background job |
| Current-state snapshot | `mainai_founder_ops/snapshot.py` | founder-ops derives work status from durable rows |

**Not found (first-class):** a cross-agent *current-obligation / ownership-timeline* ledger; an *authorized automatic artifact-transfer* path. Supersession exists per domain (envelopes, diagnoses, decisions) but **not for agent assignments/obligations**.

## Results

| Inv | CO | Result | Evidence |
|---|---|---|---|
| 3 assignment vs execution distinct | CO-3 | **PASS** | queued/waiting/running/failed/completed each distinct; a crashed task (`running` + dead job) → `failed`, not `running`. |
| 1 current vs historical | CO-1 | **PASS (partial)** | a currently-`ready` task with a 2-day-old `dispatched` event is shown `queued`/`acknowledged`, never `completed`; current state is derived from live rows, not replayed history. **Gap:** `acknowledged` is still inferred from a historical `dispatched` event (inherited **N4/AR-15**), so "acknowledged" can outlive the claim. |
| 2 no duplicate assignment | CO-2, CO-2b | **PASS** | a progressed/terminal agent task refuses re-dispatch for every non-redispatchable status; the three dedup guards exist; idempotency keys protect against replayed events. |
| 7 finite execution | CO-7 | **PASS** | `stalled` (lease expired) and `dead` (job missing/terminal) are classified; `retryable_failed` is not treated as `running`. Reuses FO-6 retry-bound work. |
| 6 no fabricated progress | CO-6, CO-6b | **PASS** | a chat answer is structurally `job_id=None`; "independently verified" needs a builder ≠ examiner exact-SHA receipt (so green CI / self-review on a SHA never reads as verified — reuses #294/#295). |
| 4 multi-agent ownership | CO-4 | **PASS (receipts only)** | the verification registry rejects `builder == examiner`, tracking distinct agents per candidate SHA. |
| 4 / 1 obligation ledger | CO-4b | **MISSING** | no module tracks, per independent agent (Cursor/Codex/examiner), a **current obligation** and dependency timeline. Coordination relies on per-owner task rows + per-SHA receipts; separate agent timelines and current-vs-historical obligation are not first-class state. |
| 5 artifact dependency | CO-5 | **MISSING** | no authorized automatic artifact-transfer/import. A missing artifact needs a manual Founder hand-off — observed directly with the Codex bundle. The examiner's `certify_v1`/`fa_core_v1` bundle-import is an *examiner-side* intake, not a production capability. |
| 10 safety boundary | CO-10 | **FAIL (LEGACY = FA-9)** | `clear_owner_stop(founder_ack="founder_ack:<any text>")` clears STOP **without proof of an authenticated Founder** — `_validate_founder_ack` checks only string format + a small denylist. Any runtime/coordination path that can build a `founder_ack:` string can clear STOP. Safety-critical; same root as the Security Kernel FA-9 / audit finding. |

## Classification

- **Existing and sound (keep):** CO-1(partial), CO-2, CO-3, CO-6, CO-7, CO-4 receipts. Task-state integrity and dedup are genuinely good.
- **Inherited defects (already on the release ledger, not new here):** N4/AR-15 (`acknowledged` from history); R1/D4/D5/N1 (Cursor); FA-PRIV legacy incl. **FA-9 STOP-ack** (Codex / Security Kernel).
- **Architectural gaps (new, this audit):** CO-4b (no cross-agent obligation ledger), CO-5 (no authorized auto-transfer).

## Remediation contract (by ownership; not arbitrary first-cycle blockers)

**Safety-critical (do first) — Security Kernel owner (Codex):**
1. **Bind STOP-clear to an authenticated Founder.** Replace the free-form `founder_ack` string with a step-up reauth receipt (reuse `account/reauth.py`) or a signed token the runtime cannot mint. Gate: CO-10, FA-9. Until then, STOP is clearable by any code that formats an ack.

**Architectural gaps (post-first-cycle; flag, do not block) — new coordination owner:**
2. **Cross-agent obligation ledger (CO-4b, inv 1+4):** a durable, owner-scoped table of `{agent, obligation, depends_on, current_status, superseded_by}` so a newer active obligation provably supersedes a completed older one and each agent's timeline is distinct. This is the structural fix for "historical queue-empty ≠ idle now".
3. **Authorized automatic artifact transfer (CO-5, inv 5):** a reviewed intake path (checksum + provenance, like `verify_bundle.sh`) so a missing artifact becomes a tracked BLOCKED with an automatic authorized-transfer attempt, instead of repeated Founder courier requests.

**Already owned elsewhere:** N4 (`acknowledged` from history) → Cursor (AR-15); R1/D4/D5/N1 → Cursor; FA-PRIV legacy → Codex.

## Not blockers for the first supervised cycle

CO-4b and CO-5 are coordination-maturity features, not safety gates; the first cycle is single-goal, Founder-supervised. Only CO-10 (STOP-ack binding) is safety-critical, and it is the existing FA-9, already owned.

## Reproduce

```
probes/coord_accept_v1/run.sh ../wtcert-2aeaa00
```
Versions: candidate #296 `2aeaa00`; base #295 `97e2b04`. The pre-existing `coordination_v1/` suite (coordination-store SQL hardening) is preserved and untouched.
