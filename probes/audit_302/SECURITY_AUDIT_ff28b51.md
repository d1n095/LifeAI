# Comprehensive Independent Security Audit — LifeAI / MainAI

- **Audited SHA:** `ff28b51429a5bc61f78308b657b8d57b2e5bf152` (frozen PR #302), whole accessible codebase + deployment definitions, not just the PR delta.
- **Examiner:** Claude, read-only. No production change, no network attack, no GitHub write, no paid provider call. Every probe runs outside a detached worktree and verifies it is unchanged.
- **Date:** 2026-10-23. Advisories verified against osv.dev and vendor trackers on this date.
- **Headline:** The codebase is in **strong** security shape. No new P0/P1 was found. The only high-severity open items are the **already-known, Codex-owned** Security-Kernel blockers (FA-PRIV, FA-9/CO-10), preserved here, not rediscovered. New findings are all **P3 hardening**, reachable (if at all) only behind single-Founder authentication, tight size caps, and container/network isolation. The first Founder-supervised (human-in-the-loop) cycle has **no newly-introduced security blocker**; broader *unattended* autonomy is gated on the Codex blockers for a concrete reason given below.

---

## 1. Technology & attack-surface inventory (independently confirmed)

| Layer | Component | Evidence |
|---|---|---|
| Backend | Python 3.12 / FastAPI 0.115.0, SQLAlchemy 2.0.35, Uvicorn | `backend/requirements.txt`, `backend/Dockerfile` |
| Frontend | TypeScript / Next.js 16.4.2, React 19.2.7 | `frontend/package.json` |
| DB | PostgreSQL + pgvector; two roles (superuser for migrations, restricted `mainai_app` at runtime) | `app/config.py`, `app/rls.py`, `backend/db-init/` |
| Cache/limits | Redis (Valkey 8.1.9 on VPS), rate-limit counters + job locks only | `docker-compose.vps.yml`, `app/limiter.py` |
| Edge | Caddy 2 (TLS/Let's Encrypt, security headers, 30 MB body cap) | `Caddyfile` |
| Deploy | Docker Compose (VPS) + Render blueprint; digest-pinned images | `docker-compose.vps.yml`, `render.yaml`, `.github/workflows/build-images.yml` |
| **No** PHP/Composer, no wildcard CORS, no `pull_request_target`, no `shell=True` | confirmed absent | repo-tree + grep |

**Public attack surface:** one process (Caddy) → Next.js standalone (proxy) → backend on a private Docker network; backend/worker/redis publish **no** host ports. **Authn surface:** cookie-based founder-only app; public registration disabled. **Untrusted-input parsers:** `pypdf` + `python-multipart` on Founder-only upload/ingest routes. **Outbound (SSRF) surface:** fixed vendor hosts (OpenAI/Anthropic/…); GitHub read client (fixed `api.github.com` + configured repo); local bridge (localhost). **Code-execution surface:** MainAI code-agent runs `pytest`/`git` as list-form subprocesses in an isolated worktree inside the hardened worker container.

## 2. Verified vulnerability / dependency matrix

| Package | Pinned | Verified advisories (osv.dev, 2026-10-23) | Reachability in THIS app | Severity |
|---|---|---|---|---|
| python-multipart | 0.0.9 | CVE-2024-53981 DoS (fixed 0.0.18); CVE-2026-40347 DoS large preamble/epilogue (fixed 0.0.26); unbounded-part-header & negative-Content-Length DoS; RFC2231 param-smuggling. **Arbitrary-file-write GHSA-wp53 = N/A** (FastAPI uses `SpooledTemporaryFile`, not the on-disk-path config) | DoS only, reachable on Founder-only `/api/documents/upload` + `/api/library/import`, bounded by 25 MB app cap + 30 MB Caddy cap | **P3** |
| pypdf | 4.3.1 | ~90 malformed-PDF advisories affecting 4.3.1 (RAM exhaustion via FlateDecode/LZWDecode bombs, infinite loops, quadratic runtime) — osv confirms all apply to 4.3.1; older CVE-2023-36464/36810 already fixed <3.9.0 | DoS/OOM only, via Founder-only upload + RAG ingest, within 25/30 MB caps; **nuance:** rises if untrusted *retrieved* documents are ever auto-ingested (item 6) | **P3** |
| PyJWT | 2.9.0 | Many GHSAs listed for 2.9.0, but every applicable one targets `PyJWKClient`/JWKS/asymmetric-key flows. **CVE-2024-53861 (iss) N/A** (regression introduced 2.10.0) | **Not exploitable** — app uses symmetric HS256, explicit `algorithms=[HS256]`, `jti`+`sessions_valid_after` revocation (`app/security.py`, `app/deps.py`) | **Informational** |
| next | 16.4.2 | current; frontend `npm audit` CI gate active | n/a | OK |

Reproducible: `probes/security_audit_v1/run.sh ../wtcert-ff28b51` → **SEC-4** prints the pinned-vs-fixed matrix; raw osv queries in the audit notes.

## 3. Deduplicated security register (P0–P3)

### Inherited / Codex-owned — PRESERVED, confirmed still open at ff28b51 (NOT rediscovered, NOT duplicated)
| ID | Sev | Component | Evidence @ ff28b51 | Owner |
|---|---|---|---|---|
| **FA-PRIV** | **P1** | Runtime role `mainai_app` can `UPDATE`/`DELETE` `users`, `audit_log`, `workforce_authority_epoch`, `execution_authorization_envelopes`, `provider_spend_authorizations`, `mainai_goals` — authority/identity/audit are not independent of the runtime role | `fa_core_v1` FAC-1..5 FAIL; `security_kernel_v1` FA-PRIV FAIL | Codex (active) |
| **FA-9 / CO-10** | **P1** | STOP-clear gated only by a `founder_ack` string, not an authenticated Founder reauth / signed token | `coord_accept_v1` CO-10 FAIL | Codex (active) |

### New / confirmed by this audit — all P3 hardening (none a launch blocker)
| ID | Sev | Finding | Exploit prerequisites | Fix | Owner |
|---|---|---|---|---|---|
| SEC-DEP-1 | P3 | python-multipart 0.0.9 DoS CVEs | authenticated Founder posts crafted multipart ≤25/30 MB | bump ≥0.0.26 | Dennis/Cursor |
| SEC-DEP-2 | P3 | pypdf 4.3.1 malformed-PDF DoS/OOM | authenticated Founder uploads/ingests crafted PDF ≤25/30 MB | bump to latest 6.x; cap pages/decompressed size | Dennis/Cursor |
| SEC-GH-1 | P3 | `GitHubReadClient.get()` accepts any absolute `https://` path (latent SSRF primitive) | would need an attacker-controlled path reaching it; all callers use fixed `API_BASE`+configured repo, Founder-only | constrain to `api.github.com` host allow-list | Cursor/MainAI |
| SEC-CI-1 | P3 | GitHub Actions pinned to mutable major tags (`@v4/@v5/@v6`); no build provenance/attestation | compromise of a first-party action tag | SHA-pin actions; add SLSA/sigstore provenance | Dennis |
| SEC-CSP-1 | P3 | No Content-Security-Policy header at Caddy (deliberately deferred, documented) | XSS foothold would be less contained | add a vetted CSP after a script-source audit | Dennis |
| SEC-CFG-1 | P3 (config) | Branch protection / required-checks / review independence are GitHub repo settings not readable from code | n/a | confirm protections on `main` + integration branches | Dennis |

### DEF-1 / GH-4 (availability/correctness, Cursor active) — NOT a security blocker, listed for completeness
`github-intake/refresh` with zero backing `AgentWorkAssignment` rows is non-idempotent across refresh/restart (`gh_intake_v1` GH-4 FAIL). Owner: Cursor (do **not** duplicate the active engineering).

## 4. Autonomy-gating synthesis (new framing, not a new vulnerability)
The code-agent executes **AI-generated code** via `pytest` in an isolated worktree (`app/mainai_execution/execution_job.py::_run_pytest`, validated target, list-form argv, bounded timeout) — but that process runs inside the worker container, which holds the `mainai_app` DB credentials. Because **FA-PRIV** lets `mainai_app` write authority/audit/spend tables, broadening MainAI to **unattended** autonomy (more unsupervised code-execution cycles) increases the blast radius of FA-PRIV specifically. This is the concrete security reason the Codex-owned **FA-PRIV + FA-9/CO-10** must close before broader autonomy. Containment that holds today: `read_only` rootfs, `cap_drop: ALL`, `no-new-privileges`, non-root UID 10001, pids/mem/cpu limits, `internal`-only data network, and `mainai_allow_live_checkout_writes=False` (writes stay in an isolated workspace).

## 5. Controls independently verified strong (executable evidence)
Argon2id passwords; HS256 JWT with explicit `algorithms=[HS256]`, `jti` blocklist + `sessions_valid_after` bulk revocation (fail-closed same-second `<=`); HttpOnly **cookie-only** tokens with **no** Authorization-header fallback (**SEC-3**); constant-time CSRF double-submit on every mutation (**SEC-3**); `require_founder` pins the exact `FOUNDER_USER_ID`, not just role (**SEC-2**); production startup guards fail closed on default `SECRET_KEY`/`FOUNDER_PASSWORD`/`FOUNDER_EMAIL` and `COOKIE_SECURE=false` (**SEC-1**); Postgres RLS with a restricted runtime role (superuser used only for migrations); parameterized SQL / constant DDL identifiers (no injection surface); no `shell=True`; network isolation + container hardening + digest-pinned images; Caddy TLS, security headers and 30 MB body cap; least-privilege CI (`contents: read`, no `pull_request_target`, image build only from `main`); explicit CORS allow-list; and the delivery/coordination gateway's forgery/replay/cross-agent/fabrication rejection (`delivery_v1`, `lifecycle_v1`, #298–302 verdicts).

## 6. First-supervised-cycle blockers vs hardening
- **First-cycle (human-in-the-loop) security blockers introduced by this audit:** NONE. Safe mode isolates writes, STOPs before merge, performs no GitHub writes.
- **Standing blockers (unattended autonomy):** FA-PRIV, FA-9/CO-10 — Codex-owned, confirmed open (§3), reason in §4.
- **Hardening (not blockers):** SEC-DEP-1/2, SEC-GH-1, SEC-CI-1, SEC-CSP-1, SEC-CFG-1.

## 7. Adversarial regression package (executed this audit)
```
probes/security_audit_v1/run.sh ../wtcert-ff28b51   # SEC-1..4 (4 passed): prod secret guard, founder id-pin,
                                                     #   cookie-only+CSRF, verified dependency floor
probes/delivery_v1/run.sh       ../wtcert-ff28b51   # DLV-1..6 (6 passed): delivery gateway hardening (#302)
probes/gh_intake_v1/run.sh      ../wtcert-ff28b51   # GH-4 FAIL = inherited DEF-1 (Cursor); GH-4b PASS
probes/coord_accept_v1/run.sh   ../wtcert-ff28b51   # CO-10 FAIL = preserved STOP-clear blocker (Codex)
probes/fa_core_v1/run.sh        ../wtcert-ff28b51   # FAC-1..5 FAIL = preserved FA-PRIV (Codex)
probes/security_kernel_v1/run.sh ../wtcert-ff28b51  # FA-PRIV + FA-* FAIL = preserved (Codex)
probes/security_diag_v1/run.sh  ../wtcert-ff28b51   # 10/10 (no regression)
```

## 8. Limitations & queue status
- **No access** to Codex's private Security-Kernel bundle or the running production VPS host. I audited the repo's deployment *definitions* (compose/Caddy/Dockerfiles/CI), not the live server. **Missing access is not treated as evidence of security** — the FA-PRIV/FA-9/CO-10 blockers remain open until Codex independently demonstrates closure.
- **GitHub branch protection** (required checks, review independence, force-push/deletion rules) is repo configuration not readable from the tree (SEC-CFG-1) — confirm in the GitHub UI.
- **CI 12 required checks** not independently re-read (no GitHub credentials in this environment); user-reported push `39910420698` / PR `39910425753` successful. Green CI is not certification.
- **Queue:** this audit is complete at ff28b51. Highest-value remaining security work is Codex's (FA-PRIV, FA-9/CO-10) and small dependency bumps (Dennis/Cursor) — none of which the examiner may perform. No examiner task remains blocked on anything actionable here.
