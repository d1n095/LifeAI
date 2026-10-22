# First-cycle Founder-supervised operational readiness — factual matrix

- **Tested SHA:** `50a1437391d5b43582d52ea19f9b5cf7d1e8e33f` (#297, latest accessible). Base #296 `2aeaa00`.
- **Conditions:** isolated, credential-free, non-billable — synthetic chat provider, `github_write_enabled=false`, egress guard (0 paid calls, 0 non-loopback), hermetic git. No merge/deploy/Recall.
- **Headline:** the supervised first cycle **completes end-to-end in safe mode today** (FCE dry-run: goal→approval→dispatch→local verify→STOP-before-merge, `stopped=true merged=false go=false`, 40-char verification SHA, no paid calls). What prevents a *real* first run is a small set of **authority/safety-hardening** blockers, all already owned, not the cycle mechanics.

## Legend
IMPLEMENTED = real code, examiner-verified PASS · VERIFIED = independently verified by an executable adversarial suite · BLOCKED = capability present but gated by a named dependency · MISSING = no mechanism.

## The Founder-facing sequence (assignment steps 1–10)

| Step | Capability | State | Evidence (suite) | Owner of any gap |
|---|---|---|---|---|
| 1 | Founder signs in; sees real readiness, restrictions, blockers | **VERIFIED** | FCE1; AR-1..AR-8; founder-ops Founder-only, GO=false, restrictions surfaced | — |
| 2 | Founder describes a development objective → Goal | **IMPLEMENTED** | FCE dry-run stage `goal` | — |
| 3 | Concrete implementation proposal, no invented completed work | **VERIFIED** | FCE (`proposed_task`, real local verify SHA); CO-6 (chat `job_id=None`); N1 | — |
| 4 | Authorization, scope, spend, STOP enforced | **VERIFIED (app layer)** / **BLOCKED (kernel)** | FCE (`approval_required`→`founder_approval`→`dispatch`); spend suite; FA-4..FA-8 show the DB layer is bypassable by `mainai_app` | **Codex** (FA-PRIV) |
| 5 | assignment→execution→verification→report, evidence-grounded states | **VERIFIED** | FCE stage sequence; CO-3 (distinct states; crash→failed); CO-7 (stalled/dead) | — |
| 6 | Independent verification not claimable from builder tests/CI | **VERIFIED** | FCE; AR-11/AR-11b; AR-18a..d (receipt bound to task commit; invalidate/supersede revoke) | — |
| 7 | Interruption / failed provider / cancel / ambiguous publication → recoverable, no unauthorized paid retry | **VERIFIED** | FO-6 (paid loop closed), FO-6c reconcilable obs; FO-7; CS (concurrent STOP vs draft PR); R3/R4 (failed dispatch, Founder cancel) | — |
| 8 | Founder inspects results, approvals, restrictions, audit | **IMPLEMENTED** / **BLOCKED (integrity)** | FCE1 (security dashboard, evidence/confidence); but `audit_log` is runtime-writable | **Codex** (FA-PRIV) |
| 9 | Denied/unavailable capability → accurate typed explanation, actionable next step | **VERIFIED (partial)** | FCE9 (typed denials distinct); **MISSING**: distinct `SecurityIncident` class (FA-16) | new (minor) |
| 10 | Founder can halt; no silent resume without valid authorization | **BLOCKED** | STOP activates and blocks (FCE dry-run, AR-2); but STOP-clear is gated only by a `founder_ack` **string format**, not an authenticated Founder — FCE10 / CO-10 / FA-9 FAIL | **Codex** (Security Kernel) |

## Capability inventory

| Area | State | Note |
|---|---|---|
| Path A supervised dry-run (safe mode) | IMPLEMENTED (PASS) | the E2E spine works; no push/merge/paid |
| Approval gating (assignment vs execution) | VERIFIED | app-layer enforced; distinct states |
| Spend reservation / provider egress control | VERIFIED | SPEND-*, egress guard |
| Independent-verification receipts | VERIFIED | builder≠examiner, SHA-bound, revocable |
| Recovery / finite execution / no paid replay | VERIFIED | FO-6/FO-7, liveness, CS, R3/R4 |
| Same-origin proxy identity / rate limits | VERIFIED | R1 fixed on #297 (PX-9/10/11, next start + standalone) |
| Security diagnostics (read-only, never auto-applies) | VERIFIED | SD-1..6 |
| **Security Kernel DB enforcement (authority/identity/audit)** | **BLOCKED (P1)** | `mainai_app` can write `users`/`audit_log`/STOP/envelope/spend — FA-PRIV (Codex) |
| **STOP-resume integrity** | **BLOCKED (P1)** | format-only `founder_ack` — FA-9/CO-10 (Codex) |
| **Grants (one-use push/PR, replay-safe, typed denial)** | **MISSING / BLOCKED** | no grant API in the Cursor lineage; GC-1..8 need the Codex composition |
| **Credential-free Git transport** | **BLOCKED** | TC-1/TC-3 fail (host helper / askpass) — Codex |
| **Publication content hardening (TA-12/13, PUB-2)** | **BLOCKED** | Codex |
| Cross-agent obligation ledger / authorized auto-transfer | MISSING | CO-4b/CO-5 — not first-cycle blockers |
| Home-dashboard stale status | OPEN (P3) | D4 residual — Cursor |
| Class-F Founder/GitHub facts (ruleset, app perms, attestations, spend grant) | UNATTESTED | Founder |

## Minimal critical path to the first SAFE supervised run

The first run is **Founder-supervised, single-goal, STOP-before-merge, no live GitHub write, no live paid provider** — the exact mode the dry-run already exercises. On that scope, the gating set is small:

1. **Codex — STOP-resume integrity (FA-9/CO-10):** bind STOP-clear to a reauth receipt / signed token. *Without this, "the Founder can halt and it cannot silently resume" (step 10) is not guaranteed.*
2. **Codex — Security Kernel DB enforcement (FA-PRIV):** RLS + REVOKE so `mainai_app` cannot rewrite authority/identity/audit; make `audit_log` append-only. *Without this, steps 4 and 8 hold only at the app layer, not independently of the model.*
3. **Founder — class-F attestation** of the supervised scope (spend grant cap, STOP runbook, reviewer identity). *Required before any run that could touch a real provider or repo, even in dry-run-adjacent modes.*

Everything else (grants, credential-free transport, TA-12/13, cross-agent ledger) is required for **activation / live GitHub write**, i.e. the cycle *after* the first safe supervised dry run — not for the first safe run itself. The home-dashboard D4 (P3) is cosmetic for this purpose.

## Ownership of remaining blockers (no duplicate work)

| Blocker | Severity | Owner |
|---|---|---|
| STOP-resume integrity (FA-9/CO-10) | P1 safety | **Codex** |
| Security Kernel DB enforcement (FA-PRIV) | P1 safety | **Codex** |
| Grants GC-1..8, credential-free transport TC-1/3, TA-12/13/PUB-2 | P1 activation | **Codex** (composed candidate) |
| Home-dashboard stale status (D4) | P3 | **Cursor** |
| Cross-agent obligation ledger (CO-4b), authorized auto-transfer (CO-5) | P2 arch | new coordination owner (post-first-cycle) |
| Class-F Founder/GitHub attestation | gating | **Founder** |

## Reproduce
```
probes/first_cycle_e2e_v1/run.sh ../wtcert-50a1437
```
See `GO_NO_GO_CONTRACT.md` for the acceptance contract to run on the future composed candidate.
