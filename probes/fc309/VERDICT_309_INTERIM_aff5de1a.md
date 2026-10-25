# Interim independent examination — PR #309 `aff5de1a` (composed integration)

- **Candidate SHA:** `aff5de1a6e2a33266bf2fd00e2a3ce554daeafab` — composition of #304–#306 agent-side bridge
  with #307 provider connectivity, plus the new `provider_ingestion.py` and `app.mainai_restrictions`.
- **Examiner:** Claude, read-only; no live/paid provider calls, no network, no credentials, no GitHub writes,
  no merges/deploy, no Recall. `first_cycle_go=false`. Codex FA-9/CO-10 + FA-PRIV preserved and re-verified failing.
- **Status: INTERIM — not a final PASS/FREEZE.** Per the standing constraint, #309 final PASS/FREEZE is withheld
  until **both** required product-SHA workflows are green on the exact frozen SHA **and** this independent
  examination has passed. CI on `aff5de1a` was still in progress at examination time; no freeze SHA is recorded yet.

## What #309 actually implements (verified against production code, not Cursor's description)
- `backend/app/agent_coordination/provider_ingestion.py` — `bind_provider_assignment` (owner/assignment/provider/
  external-agent-id/recipient binding) and `ingest_authenticated_provider_event` (HMAC verify → binding lookup →
  deterministic `event_id` → replay/reorder/expiry gates → terminal transition via the durable ledger → examiner
  handoff). `persist_ingestion` → `persist_owner_ledger`.
- `backend/app/mainai_restrictions/service.py` — `encounter_restriction`, `lookup_restriction_lessons`,
  `redact_secrets`, `restriction_public_view` (limitation reporting, pause/escalation, redaction, lesson reuse).
- Durable serialization: `dump_for` serializes `_provider_ingestion` and `_restrictions`; load/merge unions
  `processed_event_ids` and status-ranks obligations (verified; drives the restart-safety tests below).

## Executable acceptance — `composed_v1` on the real #309 functions: **17 passed**
In-memory + DB-backed tests driving the actual `DurableCoordinationService` + `bind_provider_assignment` +
`ingest_authenticated_provider_event` + `mainai_restrictions`. Reproduce: `probes/composed_v1/run.sh ../wtcert-aff5de1`.

### Priority 2 / 3 — provider evidence ingestion & adversarial integration
| ID | Property | Result |
|---|---|---|
| CMP-1 | binding stores owner/assignment/provider/external-agent-id | PASS |
| CMP-2 | forged HMAC never enters the chain (`forged_rejected`, not persisted) | PASS |
| CMP-3 | unbound / cross-owner / recipient-mismatch agent-id rejected (`rejected`/`forged_rejected`) | PASS |
| CMP-4 | webhook replay deduped on deterministic `event_id` (`replay_ignored`) | PASS |
| CMP-5 | reordered (older-than-last) event rejected (`reordered_rejected`) | PASS |
| CMP-6 | FINISHED → exactly one `completed` + exactly one examiner obligation; `independently_verified=False` | PASS |
| CMP-7 | expired / future-skew event rejected (`expired_rejected`) | PASS |
| CMP-8 | provider **cannot** inject `independently_verified` / `ready` / `founder_authorized` into trusted state | PASS |
| CMP-9 | durable restart: completion survives a fresh session; duplicate after restart → `replay_ignored` (no double-exec) | PASS |
| CMP-10 | UNKNOWN / BLOCKED remote state → `block` (`missing_capability=provider_terminal_evidence`), never a silent complete | PASS |
| CMP-11 | **PI-DEDUP (P3):** when a provider omits `timestamp`, `event_id` degrades to receive-time → a duplicate can re-apply | PASS (documents residual) |

### Priority 4 — Limitation Transparency (`mainai_restrictions`)
| ID | Property | Result |
|---|---|---|
| RST-1 | prohibited / uncertain / no-option restriction → pause + escalation (not silent bypass) | PASS |
| RST-2 | supported workaround → `continue_authorized_troubleshooting` without new authorization | PASS |
| RST-3 | malicious provider payload (`suppress` / `independently_verified` / `founder_authorized`) → pause + `paused_provider_cannot_authorize` | PASS |
| RST-4 | secret redaction by key + long keyword-bearing strings; record `plaintext_secrets=False` | PASS — **residual RST-4 (P3)** below |
| RST-5 | records fingerprinted, searchable, versioned, correctable; Founder-visible view | PASS |
| RST-6 | lessons reusable after restart but `authorization_still_valid=False` / `requires_reconfirmation=True` | PASS |

## Requirements that genuinely FLIP to PASS on #309 (were NOT_IMPLEMENTED / BLOCKED on #307)
From the #307 preflight and the ingestion contract (`fc307/PROVIDER_EVIDENCE_INGESTION_CONTRACT.md`):
- **PEVI-2 / preflight-4 (durable provider-event ingestion):** NOT_IMPLEMENTED → **implemented** (`ingest_authenticated_provider_event`).
- **PEVI-5 / preflight-5 (evidence↔assignment correlation, cross-owner):** → **PASS** (binding + `forged_rejected` on cross-owner, CMP-1/3).
- **PEVI-4 (reorder) / PEVI-8 (crash/restart recovery):** → **PASS** (CMP-5, CMP-9; durable reload, no duplicate execution).
- **Automatic examiner handoff on a provider completion (preflight-7 extended to provider path):** → **PASS** (CMP-6; distinct examiner identity, `independently_verified=False`).
- **Limitation transparency orchestrator** (previously no module): → **implemented & PASS** across RST-1..6.

## Requirements that remain BLOCKED / not flipped (honest)
- **Independent completion verification via a REAL round trip (RAI-01/03/22/27):** still BLOCKED — no live provider key,
  no authenticated external agent. Every `composed_v1` result carries `independently_verified=False`,
  `vendor_delivery_claimed=False`, `live_paid_mutate=False`, and every provider-sourced completion is marked
  `fixture=True`. **No fixture yields a vendor PASS.**
- **Unattended autonomy:** BLOCKED — Codex FA-9/CO-10 (`coord_accept_v1` CO-10 1-failed) + FA-PRIV
  (`fa_core_v1` 5-failed) re-verified on `aff5de1a`. Unchanged by #309.
- **Real external vendor connectivity:** BLOCKED (Founder-authorized live key; out of examiner scope).

## Residual findings on #309 (all P3 / composition — none a #309 regression)
- **PI-DEDUP (P3, confirmed executable — CMP-11):** the ingestion `event_id` is
  `cursor:{agent_id}:{status}:{payload.timestamp OR receive_time}`. When a provider webhook omits `timestamp`,
  dedup degrades to receive-time, so two deliveries of the same logical event can both apply. Ingestion should key
  idempotency on the webhook delivery id (or reject a terminal event lacking a provider timestamp), not receive-time.
- **RST-4 (P3, confirmed — `composed_v1` RST-4):** `redact_secrets` is keyword-driven (redacts dict keys / long
  strings matching `token|secret|password|authorization|api[_-]?key|cookie|credential`). A **bare** secret value in
  free text with no nearby keyword is not redacted. Low severity (records are Founder-visible, not public), but a
  credential pasted without its keyword can persist in a restriction record. Recommend a value-shaped detector
  (high-entropy / known key prefixes) in addition to keywords.
- **PI-STOP (P2, composition gap — verified by exhaustive read, not yet executably driven):** neither
  `provider_ingestion` nor `ingest_report` re-checks a Founder **STOP / revocation / expiration** of the owning
  assignment before applying a terminal provider completion. Root cause: `DurableCoordinationService` has **no STOP
  primitive at all** — STOP-clear is the Codex-owned Security Kernel surface (CO-10 / FA-9), and the two are **not
  wired together**. Consequence today is contained: the entire unattended path is already BLOCKED by CO-10 + FA-PRIV,
  so no real round trip can reach this code. **Before a first real authenticated round trip, the ingestion terminal
  transition MUST be gated on the Codex STOP/revocation state.** Joint owner: Codex (STOP mechanism) + MainAI (wiring).

**Confirmed product defects vs unimplemented enhancements:** PI-DEDUP and RST-4 are confirmed product behaviours
(P3, hardening — not blockers). PI-STOP is an unimplemented composition leg (the STOP mechanism it must consult does
not yet exist in this service), not a #309 regression. No P1/P2 regression was found in #309.

## Regression & preservation on `aff5de1a` (no new regressions)
`unattended_v1` 10/1skip · `gh_intake_v1` 8 · `rai_v1` 24/3skip · `cr_contract_v1` 19/2skip · `agentbridge_v2` 14/1skip ·
`delivery_v1` 6 · `lifecycle_v1` 7 · `substrate_v1` 20 · `connect_v1` 5 — all green. Preserved-BLOCKED re-verified:
`coord_accept_v1` CO-10 **1-failed** (Codex STOP-clear) · `fa_core_v1` FA-PRIV **5-failed** (Codex). Target worktree clean.

## Updated Gate-B readiness matrix (12 prerequisites; delta from #307)
| # | Prerequisite | #307 `f24ccf2` | #309 `aff5de1a` | Owner |
|---|---|---|---|---|
| 1 | authenticated external agent identity | BLOCKED | BLOCKED | Founder (live key) + Cursor |
| 2 | actual assignment receipt | BLOCKED | BLOCKED | Founder (live-mutate) + Cursor |
| 3 | authorized execution | BLOCKED | BLOCKED | Founder + Cursor |
| 4 | durable provider-event ingestion | NOT_IMPLEMENTED | **PASS (local mechanism)** | Cursor/MainAI |
| 5 | evidence-to-assignment correlation | BLOCKED | **PASS (binding)** | Cursor/MainAI |
| 6 | independent completion verification | BLOCKED | BLOCKED (needs real round trip) | Cursor/MainAI + examiner |
| 7 | automatic examiner handoff | PASS | PASS (extended to provider path) | present |
| 8 | findings-to-correction routing | PASS | PASS | present |
| 9 | restart/interruption recovery | PASS | PASS (provider ingestion restart-safe) | present / Cursor |
| 10 | STOP / spending / publication authority | BLOCKED | BLOCKED (+ PI-STOP wiring gap) | Codex + Founder |
| 11 | independent certification of composed release | NOT_TESTED | NOT_TESTED (awaits frozen SHA + green CI) | Founder + examiner |
| 12 | Founder notification / intervention | PASS | PASS | present |

**Net: 3 prerequisites advance to PASS on #309 (#4, #5, and #7/#9 extended to the provider path).** 4 PASS → **6 PASS**;
BLOCKED 6 → 4; NOT_IMPLEMENTED 1 → 0; NOT_TESTED 1 → 1. The local provider-ingestion mechanism is now present and
adversarially sound; the remaining BLOCKED items are all live-authority/real-round-trip legs (Founder + Codex), not
MainAI engineering gaps — except the PI-STOP wiring, which is pre-round-trip required.

## Remaining blockers to the FIRST real authenticated external-agent round trip
1. **Founder live authorization** (out of examiner scope): scoped Cursor Cloud key, `LIFE_CURSOR_CLOUD_AGENTS_LIVE_MUTATE`,
   bounded spend grant, webhook ingress secret.
2. **Codex close FA-9 / CO-10 (STOP-clear) + publication + FA-PRIV** — required for unattended autonomy.
3. **Wire PI-STOP**: gate the ingestion terminal transition on the Codex STOP/revocation state (joint Codex + MainAI).
4. **Harden PI-DEDUP** (webhook-id idempotency) and **RST-4** (value-shaped secret detection) — P3, recommended before trial.
5. **Freeze a composed SHA + both required workflows green**, then **examiner final certification** (Gate A green on the
   frozen SHA + RAI-01/03/22/27 flipped by a real round trip). Only then the **72h Gate-B trial** (Founder-authorized).

## Verdict
**#309 `aff5de1a`: the composed provider-ingestion + limitation-transparency integration is INDEPENDENTLY SOUND at the
local-mechanism level and advances the Gate-B matrix (6/12 PASS), with no new P1/P2 regression.** Provider evidence is
authenticated-not-authorized, cannot inject trusted/readiness fields, is replay/reorder/expiry/cross-owner safe, is
restart-durable without duplicate execution, and routes to a distinct examiner identity with `independently_verified=False`.
Limitation transparency pauses on prohibited/uncertain restrictions, cannot be suppressed or authorized by a provider,
and reuses lessons without reusing authorization. **Not a final PASS/FREEZE:** withheld pending a frozen SHA with both
required workflows green and the real-round-trip legs (Founder live auth + Codex STOP/FA-PRIV + PI-STOP wiring).
Real external vendor connectivity and unattended autonomy remain **BLOCKED**. `first_cycle_go=false`.

## Reproduce
```
probes/composed_v1/run.sh  ../wtcert-aff5de1   # 17 passed (CMP-1..11 + RST-1..6)
probes/coord_accept_v1/run.sh ../wtcert-aff5de1  # CO-10 1-failed = preserved (Codex)
probes/fa_core_v1/run.sh      ../wtcert-aff5de1  # FA-PRIV 5-failed = preserved (Codex)
# full regression batch: fc309/REGRESSION_aff5de1.txt
```
