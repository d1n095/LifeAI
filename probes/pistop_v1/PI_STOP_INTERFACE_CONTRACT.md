# PI-STOP — minimal interface contract (Cursor + Codex)

**Property under contract:** a provider completion webhook MUST NOT cause completion, examiner
promotion, or any further execution when the owning assignment's authority is invalid — Founder
STOP active, authority revoked, grant expired, or authority state UNKNOWN/unavailable.

**Candidate examined:** #309 `aff5de1a`. **Examiner suite:** `probes/pistop_v1` (6 passed / 3 skipped /
5 xfailed-strict). Read-only; network-free; no live/paid calls. `first_cycle_go=false`.

## Root cause (verified in product code, not inferred)
- The real STOP mechanism EXISTS and is sound: `app/workforce/kill_switch.py` —
  `activate_kill_switch`, `assert_not_killed(db, owner_id)`, `assert_grant_allowed` (row-locked,
  race-safe), `query_stop_status`, DB-backed `workforce_authority_epoch`, owner-scoped. The provider
  **worker** (`app/workforce/provider_worker.py`) already calls `assert_not_killed` at execution time.
- The provider **ingestion** path does NOT: `app/agent_coordination/provider_ingestion.py
  ::ingest_authenticated_provider_event` takes **no `db` session** and never consults the kill switch
  or assignment authority. Its terminal branch calls `service.complete(...)` →
  `durable.ingest_report(...)`, and `ingest_report` has no STOP/revocation/authority gate either
  (it rejects only stale/reorder/impersonation). So a FINISHED webhook arriving after a Founder STOP
  still drives `transition=completed` + a distinct examiner obligation.
- Containment today: the whole unattended path is already BLOCKED (Codex CO-10 STOP-clear + FA-PRIV),
  so no real authenticated round trip can reach this code yet. PI-STOP is therefore a **pre-round-trip
  wiring requirement**, not an exploitable production defect on `aff5de1a` — but it MUST be closed
  before the first real authenticated external-agent completion is honored.

## Interface Cursor/MainAI must implement (flips pistop_v1 B1–B5 from xfail→pass)
1. **Give ingestion an authority oracle.** `ingest_authenticated_provider_event` must accept a `db`
   session (or an injected `authority` callable) and, in the SAME logical step as the terminal
   transition, consult authority for the bound owner **before** `service.complete(...)`:
   - call `assert_not_killed(db, owner_id)` (owner + global), AND
   - verify the bound assignment is not `revoked`/expired (see §Codex below for the authority state).
2. **Fail closed on STOP/revoked/expired/UNKNOWN.** On any of these, return a terminal
   `outcome="authority_rejected"` result with `transition` absent (no completion), promote **no**
   examiner obligation, and perform no continuation. UNKNOWN/oracle-unavailable → refuse (never
   assume GRANTED).
3. **Authority is re-checked at apply time, not only at receipt.** The check must sit immediately
   before the durable write so a STOP committed between HMAC-verify and transition is observed
   (pistop_v1 B3).
4. **Durable audit of rejections.** A STOP/authority rejection must be persisted (owner, external
   agent id, `event_id`, `reason=authority_rejected`, timestamp) for Founder audit — using the same
   `persist_owner_ledger` path ingestion already uses for accepted events (pistop_v1 C3).
5. **Replay after STOP stays refused on authority.** Dedup (`replay_ignored`) is not an authority
   decision; once STOP is active a replayed terminal event must be `authority_rejected`, and the
   earlier completion must not stand (pistop_v1 B4/B5).

## Interface Codex must provide (unblocks pistop_v1 C1–C2; required for unattended autonomy)
1. **Authenticated STOP-clear.** Today `clear_owner_stop`/`clear_kill_switch_for_recovery` gate only
   on a `founder_ack:<text>` string format + a small fabricated-ack denylist (`_validate_founder_ack`).
   That is CO-10: a string is not proof the Founder authorized the clear. Provide an authenticated
   clear (bound to the Founder identity/session + the stop epoch), so a clear cannot be forged by any
   component that can supply the string.
2. **Queryable authority state with UNKNOWN.** `assert_not_killed` is binary (stopped / not) and
   raises only on an explicit stop. Provide a canonical `authority_state(db, owner_id) ∈
   {GRANTED, STOPPED, REVOKED, EXPIRED, UNKNOWN}` that the ingestion oracle (§Cursor-1) consults and
   that fail-closes on UNKNOWN / kernel-unavailable. This is the missing leg behind the broader
   FA-9/CO-10 Security-Kernel blocker.

## Blocker matrix
| # | Requirement | Classification | Owner | pistop_v1 |
|---|---|---|---|---|
| 1 | STOP before ingestion blocks completion | **EXECUTABLE NOW — FAILS (gap)** | Cursor/MainAI | B1 xfail(strict) |
| 2 | No examiner promotion under STOP | **EXECUTABLE NOW — FAILS (gap)** | Cursor/MainAI | B2 xfail(strict) |
| 3 | STOP between receipt and durable transition observed | **EXECUTABLE NOW — FAILS (gap)** | Cursor/MainAI | B3 xfail(strict) |
| 4 | Replay after STOP refused on authority (not just dedup) | **EXECUTABLE NOW — FAILS (gap)** | Cursor/MainAI | B4 xfail(strict) |
| 5 | Durable STOP honored by a fresh service (restart) | **EXECUTABLE NOW — FAILS (gap)** | Cursor/MainAI | B5 xfail(strict) |
| 6 | Cross-owner STOP does not block a different owner | **PASS (holds today)** | present | B6 pass |
| 7 | Kill switch is real, DB-backed, owner-scoped, ack-gated | **PASS (holds today)** | present | A1/A2/A3 pass |
| 8 | Authenticated STOP-clear (not a bare string) | **BLOCKED on Codex** | Codex (CO-10) | C1 skip |
| 9 | Authority-state UNKNOWN → fail closed | **BLOCKED on Codex** | Codex | C2 skip |
| 10 | STOP-rejected event preserved for audit | **BLOCKED (ingestion has no db; needs §Cursor-1/4)** | Cursor+Codex | C3 skip |

**Executable-now vs blocked:** requirements 1–7 run against #309 today (real `activate_kill_switch` +
real `ingest_authenticated_provider_event`); 1–5 fail (gap present and strict so a fix turns the suite
red), 6–7 pass (real properties). Requirements 8–10 cannot be exercised until Codex ships the
authenticated STOP-clear / authority-state interface and ingestion takes a session.

## Honesty note
No test in this suite simulates a STOP gate inside ingestion and reports PASS. The gap is proven by
**failing** required-boundary assertions (strict xfail), with positive controls (`assert_not_killed`
raising in the same transaction) showing the STOP is genuinely active — i.e. the failure is the
product's, not a fixture artifact.

## Reproduce
```
probes/pistop_v1/run.sh ../../wtcert-aff5de1     # 6 passed, 3 skipped, 5 xfailed
```
