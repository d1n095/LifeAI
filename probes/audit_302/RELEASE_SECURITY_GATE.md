# Combined Release Security Acceptance Gate (deduplicated)

One executable gate for the composed MainAI release. Built from the completed full-codebase audit
(`SECURITY_AUDIT_ff28b51.md`) + the examiner suites. It separates three release scopes and does **not**
restart research — each row points at reproducible evidence. Run with `gate.sh <target-worktree>`.

## Scope A — First Founder-supervised SAFE cycle (human-in-the-loop)
**Gate: the MUST-PASS suites below are green AND the safe-mode invariants hold.** No Scope-A blocker was
found by the audit; the supervised cycle runs with writes isolated, STOP-before-merge, no GitHub writes.

| Must-pass | What it locks | Evidence |
|---|---|---|
| `security_audit_v1` SEC-1..4 | prod secret/cookie startup guard fail-closed; `require_founder` id-pin; cookie-only auth + CSRF; dependency floor | SEC-1..4 PASS |
| `gh_intake_v1` (incl. GH-4) | durable, idempotent, Founder-only read-only GitHub intake (DEF-1 closed on #303) | 8 PASS |
| `delivery_v1`, `connect_v1` | recipient-scoped delivery; forged/replay/cross-agent/fabrication rejected; CLI fail-closed; no double-exec | 6 + 5 PASS |
| `lifecycle_v1`, `handover_v1`, `live_handover_v1` | forgery/replay/STOP-vs-coordination; one-examiner; restart-safe | 7 + 12 + 9 PASS |
| `security_diag_v1` | security monitoring regressions | 10 PASS |

## Scope B — UNATTENDED autonomy (MainAI runs cycles without a human in the loop)
**Gate: Scope A green AND both Codex-owned blockers independently CLOSED.** These are the explicit, kept blockers.

| Blocker | Why it gates unattended autonomy | Current state | Owner |
|---|---|---|---|
| **FA-9 / CO-10** | STOP can be cleared without authenticated Founder reauthorization — unattended, nothing else stops a runaway cycle | **OPEN** (`coord_accept_v1` CO-10 FAIL) | Codex |
| **FA-PRIV** | `mainai_app` can write `users`/`audit_log`/`workforce_authority_epoch`/`execution_authorization_envelopes`/`provider_spend_authorizations`/`mainai_goals`. The code-agent executes AI-generated `pytest` in the worker that holds `mainai_app` creds, so unattended cycles let generated code reach these tables | **OPEN** (`fa_core_v1` FAC-1..5 + `security_kernel_v1` FA-PRIV FAIL) | Codex |

Gate.sh asserts these remain **FAIL** (blocker present) and reports Scope B as **BLOCKED** until Codex demonstrates closure — a naive "all green" would wrongly pass.

## Scope C — External vendor (Cursor/Claude/Codex) activation
**Gate: Scope B green AND a real authorized transport to each vendor agent.** Today: **BLOCKED** — vendor
messaging `usable=False` (`agent_messaging_adapter` missing). Requires an agent-side bridge/CLI runner
(Cursor/MainAI) + Founder-issued scoped token per agent + optional Founder CLI enablement. No vendor API exists;
not inventable. GitHub publication authority stays Codex-owned.

## Hardening findings — tracked, NOT release blockers (deduplicated from the audit)
| ID | Finding | Fix | Owner |
|---|---|---|---|
| SEC-DEP-1 | `python-multipart==0.0.9` DoS advisories (CVE-2024-53981 fixed 0.0.18; CVE-2026-40347 fixed 0.0.26). Founder-auth + 25/30 MB capped | bump ≥0.0.26 | Dennis/Cursor |
| SEC-DEP-2 | `pypdf==4.3.1` ~90 malformed-PDF DoS advisories. Founder-auth + size-capped | bump to 6.x; cap pages/decompressed size | Dennis/Cursor |
| SEC-GH-1 | `GitHubReadClient.get()` accepts any absolute `https://` path (latent SSRF primitive; not currently reachable) | constrain to `api.github.com` host allow-list | Cursor/MainAI |
| SEC-CI-1 | Actions on mutable major tags; no build provenance | SHA-pin actions; add SLSA/sigstore attestation | Dennis |
| SEC-CSP-1 | No Content-Security-Policy at Caddy (deferred) | add vetted CSP after script-source audit | Dennis |
| SEC-CFG-1 | Branch protection / required-checks / review independence are GitHub config, unverifiable from code | confirm in GitHub UI | Dennis |

PyJWT 2.9.0 is **not** a finding (symmetric HS256, explicit `algorithms=[HS256]`, jti revocation → JWKS/iss advisories N/A); bump is hygiene only.

## Verified-strong controls (regression-locked; do not re-audit)
Argon2id; HS256 explicit-alg JWT + jti/`sessions_valid_after` revocation; HttpOnly cookie-only tokens, no header fallback; constant-time CSRF; `require_founder` exact-id pin; RLS restricted runtime role; parameterized SQL; no `shell=True`; network isolation + container hardening + digest-pinned images; Caddy TLS/headers/30 MB cap; least-privilege CI (no `pull_request_target`, image build only from `main`); delivery/coordination forgery-rejection.

## How to read the gate result
- **Scope A PASS + Scope B BLOCKED + Scope C BLOCKED** = cleared for the first Founder-supervised safe cycle; NOT for unattended autonomy or vendor activation. This is the expected state at #302/#303.
- Scope B clears only when Codex closes FA-9/CO-10 and FA-PRIV (the gate re-checks by expecting CO-10 / FAC to flip from FAIL to PASS).
