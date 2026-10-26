# Operational-handover readiness matrix (examiner synthesis, read-only)

Consolidates the independent examinations of the parallel work line into one view of remaining
blockers to MainAI's first genuine unattended operating cycle, with owners. Nothing here certifies a
release; it is an evidence-anchored map of what each examined SHA proves and what is still missing.
`first_cycle_go=false`. No merge/deploy/GO; Codex's Security Kernel untouched.

## Candidate line (all parallel off #306 `2444bcc`)
| PR | Exact SHA | Scope | Examiner suite(s) | Status |
|---|---|---|---|---|
| #306 | `2444bcc3da8ffa5ca10ef3295bac13aa045769c6` | local agent-side bridge | fc306 | **PASS/FREEZE (Scope A)** |
| #309 | `aff5de1a6e2a33266bf2fd00e2a3ce554daeafab` | provider ingestion + limitation transparency | composed_v1, pistop_v1, indepcomp_v1, spend_authority_v1 | INTERIM, NOT CERTIFIED |
| #310 | `beecf0e35cd27adffff6f95330e9514db8a6ea5d` | Founder-absence operational handover | absence_v1 | policy sound, NOT CERTIFIED |
| #311 | `c3373f93cb6d3881ed6560cdd106ac1852c54a1e` | large-file streaming lane | large_file_v1 | 1 P2 defect, NOT CERTIFIED |
| STOP-clear | `20d5a14301f574efe0ba3cc1d2d2f694b6cc1ef4` | kernel STOP-clear (Codex) | — | **NOT YET EXAMINED (not fetchable)** |
| FA-PRIV | `078312d7abce3f5c0f87c9e7b30161973e55d532` | kernel DB-privilege isolation (Codex) | — | **NOT YET EXAMINED (not fetchable)** |

## Authority-enforcement contracts for the first unattended cycle
Each row: is the *mechanism* present, and is it *wired* into the path that needs it, with examiner evidence.
| Contract | Mechanism present? | Wired into the acting path? | Evidence | Owner of the gap |
|---|---|---|---|---|
| Founder **STOP** halts provider completion | yes (`kill_switch.assert_not_killed`, worker gates execution) | **NO at ingestion** (PI-STOP) | pistop_v1 B1–B5 xfail | Cursor (wire) + Codex (authority_state) |
| Founder **STOP-clear** is authenticated | partial (string `founder_ack`, CO-10) | pending Codex `20d5a143` | pistop_v1 C1 skip | Codex (kernel) |
| **Revoked/expired** execution grant blocks spend | yes (`provider_spend` revoke/expiry) | yes (reserve refuses) | spend_authority_v1 C1/C2 pass | — (holds) |
| **Provider spend/reservation under STOP** fails closed | spend ledger sound; kill switch sound | **NO** (SPEND-STOP) — spend layer doesn't read kill switch | spend_authority_v1 G1 xfail | Cursor (wire) |
| **Independent verification** before privileged continuation | yes (`mainai_verification_registry`, runtime cannot forge, append-only) | **NO** — provider cycle doesn't consult `find_independent_pass` (ICV-WIRING) | indepcomp_v1 B1..D1 pass, E1 xfail | Cursor (wire) |
| **Publication/deployment** authority | gated (GitHub App publication not available) | n/a (not attempted) | gates.py Gate-1 part False | Founder (grant) + Codex (publish authority) |
| **Concurrent workers / stale authority epochs** | yes (`assert_grant_allowed` row-lock + epoch; usage `ON CONFLICT`) | yes | spend_authority_v1 D1; kill_switch epoch design | — (holds) |
| **Autonomous delegation** within standing authorization | policy only (`evaluate_absence_tick`, standing-grant gating) | **NO** — no executing capability selector (DELEGATE-WIRING) | absence_v1 AB5 pass, IE1 xfail | Cursor (wire) |
| **Silence never authorization** | yes | yes (policy) | absence_v1 AB2/AB7 pass | — (holds) |
| **Bounded self-improvement** never grants/deploys | yes | yes | absence_v1 SI1–SI4 pass | — (holds) |
| **Large-file resume integrity** (whole-file checksum) | **defective** (CHECKPOINT-HASH) | n/a | large_file_v1 LF9 xfail | Cursor (fix) |
| Ordinary runtime cannot **grant itself authority / forge verification / forge Founder approval** | yes (DB trigger, append-only, authority fingerprint) | yes | indepcomp_v1 B1/B2, spend_authority_v1 E1 pass | Codex (FA-PRIV completes the DB-privilege set) |

## The dominant remaining-work class: "sound subsystem, not wired" — UPDATED
Five gaps shared one shape — a correct, fail-closed store the acting path didn't consult. **Three are
now CLOSED and independently verified** (#312 `22458257`, #313 `5cb9b46`); two remain open:
- **PI-STOP** — ✅ CLOSED (#312): ingestion wraps `assert_not_killed`+`assert_grant_allowed`, fail-closed without a session (fc312_v1)
- **ICV-WIRING** — ✅ CLOSED (#312): provider continuation queries `find_independent_pass` (fc312_v1)
- **PI-DEDUP** — ✅ CLOSED (#312): stable `event_id` from `sha256(raw_body)` (fc312_v1)
- **RST-4** — ✅ CLOSED (#312) for prefixed secrets; P4 residual (non-prefixed high-entropy) documented
- **CHECKPOINT-HASH** — ✅ CLOSED (#313): resume re-hashes the prefix; resumed hash == single-pass (fc313_v1)
- **SPEND-STOP** — ⬜ OPEN: spend reserve ↔ kill switch (spend_authority_v1 G1) — Cursor wiring
- **DELEGATE-WIRING** — ⬜ OPEN: self-improvement proposal ↔ capability selector/executor (absence_v1 IE1) — Cursor wiring

Where an existing kernel interface already suffices (per Codex's "identify, don't duplicate" rule):
`kill_switch.assert_not_killed` / `assert_grant_allowed` (STOP + grant race), `provider_spend`
reservation ledger (spend ceilings), `mainai_verification_registry.find_independent_pass` (independent
verification). The wiring should consume these, not introduce parallel mechanisms.

## Blockers to first operational handover (owner · closes which xfail)
1. **Fix CHECKPOINT-HASH** (Cursor) — resume must reproduce the single-pass whole-file hash; closes
   large_file_v1 LF9; Gate-2 precondition G2-PRE-1. Smallest, no resources needed.
2. **Wire PI-STOP + SPEND-STOP** (Cursor, using `assert_not_killed`) — ingestion & spend reserve fail
   closed under STOP; closes pistop_v1 B1–B5, spend_authority_v1 G1.
3. **Wire ICV-WIRING** (Cursor, using `find_independent_pass`) — provider completion gated on an
   independent, non-builder, exact-SHA PASS; closes indepcomp_v1 E1.
4. **Wire DELEGATE-WIRING + autonomous loop** (Cursor) — capability selector/executor + a loop that
   drives `evaluate_absence_tick`; closes absence_v1 IE1.
5. **Examine Codex `20d5a143` (STOP-clear) + `078312d7` (FA-PRIV)** (Claude) — blocked on fetchable
   bytes/ref; closes pistop_v1 C1, confirms FA-PRIV closure of `fa_core_v1` / `coord_accept_v1`.
6. **Founder live authority** — Cursor Cloud key, live-mutate flag, spend grant, webhook secret,
   publication authority; real round trip (RAI-01/03/22/27) + approved unattended-cycle trial.
7. **Gate-2 under authorized resources** — 5×5 GB measured per LARGE_FILE_GATE2_SPEC after #1.

## What can safely proceed now (no GO, no resources)
- Cursor: blockers 1–4 (all wiring/fix of existing sound subsystems; each has an executable xfail that
  flips to PASS on correct implementation).
- Claude (examiner): examine Codex kernel SHAs the moment they are fetchable; re-run the four
  strict-xfail suites against any corrected candidate (they self-invalidate, so no re-derivation needed).
- Founder: authorize live credentials + Gate-2 resources when ready; nothing above presumes GO.
