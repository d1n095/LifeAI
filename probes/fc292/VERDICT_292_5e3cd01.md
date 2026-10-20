# Independent examination — #292 (application readiness: truthful Founder interface, safe first launch)

- **Candidate:** PR #292 (draft), branch `cursor/application-readiness-91c1`.
- **Examined SHA:** `5e3cd01ac3a2cded69ef19bfa074d15196d3afaa`. Four commits on frozen #291 `0b1871b`; ancestry verified.
- **Production delta:** 32 files (+1917/−65):
  - `mainai_founder_ops/` (snapshot, status, assignment guard) and its router;
  - executive dashboard;
  - duplicate-work guards in `agent_orchestration` and the workforce broker;
  - chat failed-turn schema;
  - frontend dashboard, `/admin/founder-ops`, AuthGuard, chat, Library;
  - E2E specs.
- **Head and CI:** confirmed through the unauthenticated public API. The commit was fetched anonymously (isolated git config, empty credential helper). Head is unchanged at verdict time.
- **Examiner:** Claude, read-only.
  - Hermetic git; synthetic credentials only.
  - Provider recorders plus an egress guard: **zero paid calls, zero non-loopback connections**.
  - No merge, deploy or Recall. Local evidence only.

## Verdict: RETURN_TO_BUILDER

Two of the PR's own headline claims are false under executable test (D1, D2). Everything else the PR delivers holds.

### Defect ledger

| ID | Sev | Origin | Defect | Evidence |
|---|---|---|---|---|
| **D1** | **P1** | #292 | **"Självständigt verifierad" (independently_verified) is shown for builder self-verification.** `status._task_independently_verified` accepts the builder job's own `verification_passed` event or its own checkpoint `test_status=passed`. `_finalize_task_outcome` emits that event for *every* completed Path A task, so every completed task is labelled independently verified. In safe-launch mode, an **"Open a draft PR." task that opened no PR and ran zero verification steps (`steps: []`)** is also shown as independently verified. This contradicts the module's own invariant ("from a party that is not the builder"). The PR's unit test locks in the wrong behaviour. | `app_readiness_v1` AR-11 (real worker cycle: 1 verification event from the same single job → `independently_verified`); AR-11b (safe mode: `prs=0`, `steps=[]` → `independently_verified`) |
| **D2** | **P1** | #292 | **The claim that a second paid agent dispatch is refused is not race-safe.** `assert_agent_dispatch_allowed` reads `task.status` and then awaits the provider, with no lock and no compare-and-set. Two concurrent dispatches both pass and **both make a paid provider call**. This is reachable over HTTP: two Founder requests to `/api/admin/agents/tasks/{id}/dispatch` → `200, 200`, 2 paid calls. | AR-9 (`['ok','ok']`, paid_calls=2); AR-9h (HTTP `[200,200]`, paid_calls=2) |
| D3 | P2 | #292 | The duplicate live delegation guard is check-then-insert with no lock or unique constraint. Two concurrent `submit_delegation_request` calls for one task both succeed: **20/20 races produced 2 open requests.** | AR-10 (`duplicated=20/20`) |
| D4 | P2 | #292 | **Stale positive state on `/admin/founder-ops`.** After the backend is lost, the 5 s poll shows the error banner but keeps rendering the last snapshot: "Backend: nåbar", "STOP: Inte blockerad", work counts. There is no staleness marker and no `generated_at`. The dashboard does not poll at all, so its health line stays "Backend nåbar" indefinitely after load. This contradicts the PR claim "never 'Backend nåbar'" on failure, and the fail-closed STOP principle (unknown is treated as blocked). | Live stack + built-in browser: page loaded healthy, backend killed, read after 8 s (`proxy_v1/live_stack.sh`; procedure below) |
| D5 | P3 | #292 | `/admin/founder-ops` shows "Beredskap: BLOCKED" next to "BLOCKERINGAR (0) Inga registrerade blockeringar". Readiness blocking reasons are not included in `blockers`. | Live stack, Founder-status page text |
| P-1 | P2 | pre-existing (proxy `route.ts`, unchanged) | **Proxy path traversal leaves `/api/`.** Encoded dot segments (`..%2f`, `%2e%2e%2f`, `..%5c`, double-encoded `%252e%252e`) are decoded before `new URL()` normalises them. Anonymous users get the backend's `/openapi.json` (180 KB full schema of the Founder-only API), `/docs` and `/redoc` through the public frontend. | `proxy_v1` PX-1 (fake backend records `/openapi.json`, `/docs`); live stack: `GET /api/..%2fopenapi.json` → 200, 179 673 bytes |
| P-2 | P2 | pre-existing (`route.ts` + `backend/Dockerfile`) | **Per-IP rate limits are bypassable.** The proxy forwards the client's `X-Forwarded-For` verbatim. uvicorn `--forwarded-allow-ips=*` (uvicorn 0.30.6) takes the **leftmost** entry. End to end through the real proxy, 30/30 password-spraying login attempts with a rotating spoofed XFF are processed (limit 10/min). The control (same IP, or a fixed spoofed XFF) is limited at the 11th attempt. Audit/login IPs are attacker-chosen. Per-account lockout still protects a single account. | PX-2; PX-9 `xff_ratelimit.sh` (Dockerfile flags, `next start`) |
| P-3 | P2 | pre-existing (proposal mode), surfaced by #292's safe-launch claim | With the safe-launch default `github_write_enabled=false`, Path A still runs the paid code agent, **writes its files into the backend's own checkout** (`_repo_root()`), and runs pytest there. `safe_launch.privileged_remote_effects_enabled=false` is accurate for *remote* effects, but local source writes are not represented. In the examiner's first unredirected run, the written test picked up the real `tests/conftest.py` and dropped the examiner's test database. | AR-13 (no remote branch, no PR; `local_server_checkout_writes=[backend/app/greeting.py, backend/tests/test_greeting_doc.py]`) |
| K-1 | P1 (carry) | pre-existing | FO-6: a bare RuntimeError or crash at the push boundary on a governed goal → paid re-run ×2, task stranded `ready`. This blocks first-cycle GO, not this PR's scope. | acceptance `stop_classification`: 4 blockers, identical to #291 |
| K-2 | known | PR-listed | Chat and workbench call paid providers when keys are set: chat makes an embed and a chat attempt. No Next.js middleware: shell HTML is client-guarded and static pages contain no Founder data. | AR-12; static build output grep |

### What holds (executable)

**Authorization matrix (AR-1):**
- all 131 mounted `/api` routes outside self-service auth/account were probed;
- an ordinary member **and** a `role=founder` impostor with a non-Founder id get **403 on every route**;
- anonymous callers get 401/403 everywhere except `/api/health`;
- **0** provider calls and **0** egress.

**STOP (AR-2..AR-4):**
- non-Founder, impostor, anonymous, missing/forged CSRF and invalid reasons are all refused, with **no authority-epoch change**;
- a valid STOP blocks everywhere;
- POST is rate limited (429 after 120/min);
- an unreadable STOP gives GET 503 and snapshot `blocked=true, observability_failed=true`;
- a readiness failure gives `UNKNOWN` with blocking reasons;
- `first_cycle_go` stays false.

**Dependency failure (AR-5):** Redis or database down → `unavailable`, `backend_reachable=false`. A missing heartbeat is never reported as `alive`.

**Account isolation (AR-6):** another account's goal, task, job, conversation and usage rows never appear in the Founder snapshot. The other account cannot read the Founder's conversation.

**Safe launch (AR-7, AR-13):**
- no Recall route is mounted, and every plausible prefix returns 404;
- the detector flips when the real Recall prep router is mounted (non-vacuous);
- GO is never derived from flags, even with all flags on;
- with `github_write_enabled=false`, a full real-worker cycle pushes no branch and opens no PR.

**Offline status reads (AR-8):** snapshot, STOP, dashboard and health make no provider call and open no egress.

**Chat (AR-12):** a failed assistant turn is durable and visible after reload. No egress in safe mode.

**Outage:** the full-outage UI is honest (AuthGuard: "Kunde inte nå servern…", no logout).

**Proxy:**
- hop-by-hop and `Proxy-Authorization` headers are stripped;
- the attacker `Host` is not forwarded;
- multiple `Set-Cookie` headers are preserved;
- backend down → structured 502 with no internal address.

**Frontend:** `tsc` clean, ESLint 0 errors (1 warning), `next build` OK. Static HTML carries no Founder name, email or id.

**Full `cursor` acceptance profile:**
- 788 regression tests pass;
- review/STOP, spend, task authority, credential canary, FC-4 and transport (TC-0/TC-2) unchanged;
- the same 4 FO-6 blockers as #291 and **no regression**.

**Exact-SHA CI:** all 12 required checks plus the aggregate are `success` in both the push and pull_request suites.

### Required to return

- **D1:** `independently_verified` must require durable evidence from a non-builder party, such as:
  - a CI receipt bound to the exact SHA from a check-run app;
  - a review receipt from a reviewer other than the builder;
  - a workforce verifier whose `verifier_profile_id` differs from the builder's.

  A task with zero verification steps is never "verified". Fix the unit test that encodes the opposite.
- **D2:** make dispatch atomic, for example a conditional `UPDATE … SET status='dispatched' WHERE id=:id AND status IN (created, reviewed_needs_correction)` with a row count of 1 *before* the provider call.
- **D3:** add a partial unique index or `SELECT … FOR UPDATE` on the task row.
- **D4:** on a failed poll, mark the cards stale (or clear them) and render STOP as "okänd — behandlas som blockerad". Show `generated_at`.
- **Gates:** AR-9, AR-9h, AR-10, AR-11, AR-11b; D4/D5 by E2E.

**Pre-existing P-1/P-2/P-3 are not grounds for this return**, but they should be fixed before any public exposure:
- **P-1:** reject any decoded path segment that is `.` or `..`, or contains `/` or `\`. Disable `/docs`, `/redoc` and `/openapi.json` in production.
- **P-2:** have the proxy overwrite `X-Forwarded-For` with the peer address it actually received, and trust only that single hop.
- **P-3:** surface proposal-mode local writes in `safe_launch`, or disable Path A `repo_edit` entirely in safe mode.

## Reproduce

```
git worktree add --detach ../wt292 5e3cd01ac3a2cded69ef19bfa074d15196d3afaa
REDIS_URL=redis://127.0.0.1:6390/0 probes/first_cycle_acceptance_v1/run_acceptance.sh cursor ../wt292
probes/app_readiness_v1/run.sh ../wt292                     # AR-1..AR-13
# frontend: git archive the SHA's frontend/ into a scratch dir, `npm ci --ignore-scripts`, `next build`, then:
probes/proxy_v1/run.sh <built-frontend>                     # PX-1..PX-8 (fake backend)
probes/proxy_v1/xff_ratelimit.sh ../wt292 <built-frontend>  # PX-9 (real backend, Dockerfile flags)
probes/proxy_v1/live_stack.sh up ../wt292 <built-frontend>  # D4/D5: log in at :13073, open /admin/founder-ops,
#   kill the backend process (run_live_backend.py), wait one poll, read the cards; then `live_stack.sh down`
```
