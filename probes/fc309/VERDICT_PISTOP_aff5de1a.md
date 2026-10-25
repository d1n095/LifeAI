# PI-STOP security-boundary examination — #309 `aff5de1a`

- **Scope:** prove that a provider completion cannot bypass a Founder STOP / revoked authority /
  expired grant on the composed candidate. Executable acceptance + race tests, read-only.
- **Suite:** `probes/pistop_v1` — **6 passed, 3 skipped, 5 xfailed(strict)**. Network-free, no
  live/paid calls, no GitHub writes, no merge/deploy. `first_cycle_go=false`.
- **Result: PI-STOP CONFIRMED as a real, executable composition gap on `aff5de1a`.** #309 stays
  **NOT CERTIFIED**; Gate B **NOT PERFORMED**.

## Finding (confirmed executably, not by code-reading alone)
The provider-event **ingestion** path applies a terminal completion (and promotes a distinct
examiner obligation) with **no authority check**. A real, DB-backed Founder STOP
(`activate_kill_switch`) that `assert_not_killed` enforces in the very same transaction does **not**
stop `ingest_authenticated_provider_event` from completing a FINISHED webhook — because ingestion
takes no `db` session and neither it nor `durable.ingest_report` consults the kill switch or
assignment authority. The provider **worker** (`provider_worker.py`) is STOP-gated; the **ingestion**
leg is not. Contained today only because the whole unattended path is already BLOCKED (Codex CO-10 +
FA-PRIV), so no real round trip reaches this code — but it MUST be closed before the first real
authenticated completion is honored.

## Evidence
**PASS (properties that genuinely hold):**
- A1 — kill switch is real, DB-backed (`workforce_authority_epoch`); `query_stop_status.blocked=True`,
  `assert_not_killed` raises after `activate_kill_switch`.
- A2 — STOP is owner-scoped: a STOP on owner A does not raise for owner B.
- A3 — STOP-clear rejects empty and boot-fabricated acks (the minimal check CO-10 flags as insufficient).
- B6 — cross-owner: owner A's STOP does not block owner B's legitimate ingestion (no cross-owner DoS;
  binding resolves the correct owner).

**xfail(strict) — required boundary UNMET today (each fails; XPASS if fixed → suite turns red):**
- B1 PI-STOP-1 — STOP before ingestion does not block completion.
- B2 PI-STOP-1 — examiner obligation is still promoted under STOP.
- B3 PI-STOP-2 — a STOP committed between HMAC-verify and the durable transition is not observed.
- B4 PI-STOP-5 — after STOP, the standing completion is not revoked and a replay is only deduped,
  not authority-rejected.
- B5 PI-STOP-6 — a fresh service (restart analogue) ignores the durable STOP and completes.

**BLOCKED on Codex (skipped with exact reason):**
- C1 — authenticated STOP-clear: today gated only by a `founder_ack:<text>` string (CO-10).
- C2 — authority-state UNKNOWN → fail closed: no queryable `authority_state()` with an UNKNOWN value.
- C3 — STOP-rejected event preserved for audit: ingestion has no `db`, so no durable rejection record.

## Regression (focused, not a re-run of the full #309 exam)
- D1 PI-DEDUP (P3) — confirmed: a timestamp-less duplicate terminal event is NOT deduped
  (event_id degrades to receive-time).
- D2 RST-4 (P3) — confirmed: a bare high-entropy secret value with no nearby keyword is NOT redacted.

## Interface contract delivered
`pistop_v1/PI_STOP_INTERFACE_CONTRACT.md` — minimal, split by owner: Cursor/MainAI (ingestion takes a
`db`/authority oracle; fail-closed `authority_rejected` with no examiner promotion; apply-time re-check;
durable rejection audit; replay-after-STOP refused on authority) and Codex (authenticated STOP-clear
bound to Founder identity + stop epoch; `authority_state ∈ {GRANTED,STOPPED,REVOKED,EXPIRED,UNKNOWN}`
that fail-closes on UNKNOWN). Blocker matrix included: requirements 1–5 EXECUTABLE-NOW/FAIL,
6–7 PASS, 8–10 BLOCKED.

## Verdict
**PI-STOP is a confirmed pre-round-trip blocker.** It is an unimplemented composition leg (ingestion
not wired to the existing, sound kill switch) plus a Codex authority-interface dependency — **not** a
new #309 regression and not exploitable on `aff5de1a` today (unattended path already BLOCKED). #309
remains **NOT CERTIFIED**, Gate B **NOT PERFORMED**. No STOP mechanism was simulated and reported as a
product PASS. Next: independent-completion acceptance for the eventual real provider cycle.
