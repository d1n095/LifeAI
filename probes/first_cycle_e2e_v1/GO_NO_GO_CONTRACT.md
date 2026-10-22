# First-cycle GO / NO-GO acceptance contract (runnable on the future composed candidate)

A single, reproducible contract the examiner runs against the exact composed-candidate SHA. Every row is executable or an
explicit class-F attestation. **GO requires every executable row PASS and every class-F row attested.** BLOCKED / MISSING / a
superseded-SHA green CI never counts as PASS. This is the acceptance gate for the first Founder-supervised run.

## How to run
```
# 1. Intake the exact SHA (public PR or an authorized git bundle):
probes/certify_v1/intake.sh <SHA> [bundle]              # or probes/fa_core_v1/verify_bundle.sh <bundle>
# 2. Build the frontend outside the frozen tree:
FE=$(probes/proxy_v1/build_frontend.sh <worktree> <scratch>/fe)   # + Playwright Chromium in <scratch>/fe/../pw-browsers
# 3. Full combined profile + first-cycle E2E + security kernel + coordination:
FE_BUILT=$FE probes/first_cycle_acceptance_v1/run_acceptance.sh combined <worktree>
probes/first_cycle_e2e_v1/run.sh  <worktree>
probes/security_kernel_v1/run.sh  <worktree>
probes/fa_core_v1/run.sh          <worktree>
probes/coord_accept_v1/run.sh     <worktree>
probes/proxy_v1/identity_collapse_standalone.sh <worktree> $FE
```

## Executable rows

| # | Gate | Suite | GO criterion |
|---|---|---|---|
| G1 | Supervised dry-run completes safe | `first_cycle_e2e_v1` FCE | goal→approval→dispatch→verify→STOP; merged=false; go=false; 0 paid |
| G2 | Founder-only authz across all `/api` | `app_readiness_v1` AR-1 | member + role-only impostor → 403 everywhere |
| G3 | Approval / spend / scope enforced **in the DB**, not just the app | `security_kernel_v1` FA-4..FA-8; `fa_core_v1` FAC-2/FAC-5 | runtime role cannot self-grant, widen scope, raise ceilings |
| G4 | STOP integrity + no silent resume | `security_kernel_v1` FA-9; `coord_accept_v1` CO-10; FCE10 | STOP-clear needs an authenticated-Founder reauth/signed token |
| G5 | One-use, replay-safe, typed-denial grants | `grant_contract_v1` GC-1..8 (adapter) | all pass on the composed grant API |
| G6 | Credential-free Git transport | `transport_contract_v1` TC-0..3; harness CRED | no host helper / askpass / token-in-URL |
| G7 | Independent verification only from a non-builder receipt | AR-11/AR-11b, AR-18a..d | builder/CI self-cert never = verified; invalidate/supersede revoke |
| G8 | Recovery / finite exec / no paid replay | `stop_classification_v1` FO-6/FO-7/RL/CS; R3/R4 | ambiguous boundary terminal+observed; 0 extra paid |
| G9 | Publication content hardening | `candidate_harness` TA-12/TA-13/PUB-2 | no symlink/gitlink/history ride-along |
| G10 | Migration integrity | `migration_integrity_v1` | single head, no dual-0089 |
| G11 | Same-origin proxy identity / rate limits | `proxy_v1` PX-1/9/10/11 (next start **and** standalone) | traversal blocked; distinct clients distinct limits; spoof-proof |
| G12 | Security diagnostics never auto-applies | `security_diag_v1` SD-1..6 | remediation apply always refused; redaction holds |
| G13 | Audit append-only | `fa_core_v1` FAC-4; FA-12 | `audit_log` not runtime UPDATE/DELETE-able |
| G14 | Truthful Founder UI under outage | `ui_v1` UI-1/2/3 | no stale positive health/STOP; BLOCKED names a cause |
| G15 | Exact-SHA CI | `ci_exact_sha` | 12 required checks × push + pull_request green at the exact SHA |

## Class-F rows (attested separately; never implied by a green gate)

| # | Fact | Source |
|---|---|---|
| F1 | Integration branch at the approved base SHA; ruleset (PR required, 12 checks bound to app 15368, no bypass, Founder-only merge) | GitHub settings |
| F2 | Runtime GitHub App identity + permission manifest; no classic PAT | GitHub settings |
| F3 | Signed attestation material + pinned verification key for preflight | Founder |
| F4 | First-cycle spend grant (cap, provider/model allow-list) + envelope profile | Founder |
| F5 | Reviewer identity + receipt-recording runbook (privileged path) | Founder |
| F6 | Incident token revoked; evidence-publication method | Founder |

## Current standing (on #297 `50a1437`, the latest accessible)

- **GO rows PASS now:** G1, G2, G7, G8, G11, G12, G14 (D4 home-dashboard P3 pending), G15.
- **NO-GO now:** G3, G4, G13 (**Codex** FA-PRIV / STOP-ack); G5, G6, G9, G10-compose (**Codex** composed candidate not published); all class-F (**Founder**).
- **Overall: NO-GO**, blocked on the Codex Security-Kernel + composed grant/transport candidate and Founder attestation — **not** on the Cursor cycle mechanics, which pass.
