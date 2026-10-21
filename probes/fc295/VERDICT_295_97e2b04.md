# Independent examination — #295 (Founder-workflow P2 corrections)

- **Candidate:** PR #295 (draft), branch `cursor/founder-workflow-p2-91c1`.
- **Examined SHA:** `97e2b04c9babf3c30e3067fb9e8282a82b071be4`. Three commits on #294 `27e945d`; ancestry verified. Exact-SHA CI: all 12 required checks + aggregate green in push and pull_request.
- **Examiner:** Claude, read-only. Hermetic git; synthetic credentials; provider recorders + egress guard; no merge/deploy/Recall/paid calls.
- **Method:** the consolidated correction gate `cursor_corrections_v1` (each defect → gate + positive control), run against the real app, worker, `next start` proxy, a backend started with the Dockerfile uvicorn flags, and real Chromium.

## Verdict: RETURN_TO_BUILDER — one P1 unresolved (R1)

#295 is a large, genuine improvement over #294: R2, R3, R4, R5 and R6 are now closed, with positive controls passing. One release blocker remains, plus four carried P2/P3 UI/communication items.

### Open

| ID | Sev | Finding | Evidence |
|---|---|---|---|
| **R1** | **P1** | **Rate-limit identities still collapse — global lockout persists.** #295 replaces the client `X-Forwarded-For` with Next's `request.ip` (`frontend/lib/proxyRequestIdentity.ts`). Under self-hosted `next start` (the Docker/Render deployment), `request.ip` is **undefined**, so the proxy forwards the constant `x-forwarded-for: unknown` for every client. uvicorn (`--forwarded-allow-ips=*`) then keys every anonymous caller as `ip:unknown`, so 10 failed logins a minute from anyone lock out the Founder from every address. This is the same outcome as #294 by a new mechanism. | `proxy_v1/identity_collapse.sh` on ee2… : control PX-10a limits client A; **PX-10** client B (`::1`) → 429 after A sprays; **PX-11** client B → 429 after A forges B's address. Proxy log shows `x-forwarded-for: 'unknown'`. On #293 PX-10 returns 200. |
| D4 | P2 | Not fixed: with the backend lost, `/admin/founder-ops` keeps "Backend: nåbar" and the dashboard health line keeps "Backend nåbar". | Playwright UI-1, UI-2 (genuine assertions; `Received: 1`) |
| D5 | P3 | Not fixed: readiness BLOCKED shown with `blockers=[]`. | AR-17, UI-3 |
| N1 | P2 | Not fixed: a Founder *question* mentioning GO still gets a "Nej … evidensen motsäger …" prefix. | AR-14a |
| N4 | P2 | Not fixed: a re-queued `ready` task with a historical `dispatched` event still shows "acknowledged". | AR-15 |

### Closed in #295 (verified, with positive controls)

| Item | Result |
|---|---|
| **R2** independent-receipt binding | **CLOSED.** Receipts now bind to the task's **actual produced commit** (`_task_commit_sha` from checkpoint/event `work_result`), and a later FAIL (invalidating or superseding) revokes. AR-18a (invalidated), AR-18b (wrong SHA) and AR-18c (superseded) → `completed`; **AR-18d positive control** (a matching independent PASS) → `independently_verified`. |
| **R3** failed-dispatch recovery | **CLOSED.** A failed paid dispatch no longer strands the task as running/`dispatched` with 409 retries. AR-9f passes. |
| **R4** Founder cancel vs crash | **CLOSED.** A Founder cancel survives a worker crash as `cancelled` (not "external publication failure"), no takeover, no paid call. FC passes. |
| **R5** ambiguous-push observation | **Substantially closed.** The P1 (no paid replay, terminal) holds — 1 job, 1 code-agent call — and the crash finalizer now emits a durable `reconcilable: true, origin: unknown` observation event distinguishing an ambiguous push from a confirmed one (`latest_task_checkpoint_for_step("finalized")`). FO-6b/c pass. **Residual (P3):** the flag marks the push for reconciliation but nothing yet performs the actual remote-ref observation or consumes the flag — mark-only, never-replay. |
| **R6** safe-mode verification | **CLOSED.** A valid safe-mode proposal is hydrated into the isolated workspace and verified there: `verification_passed` is now reachable with one paid call, without touching the live checkout. AR-13c passes; AR-13/AR-13b still pass. |
| P-1, D1, D2, N2, N3 | Remain closed (PX-1; AR-11/11b; AR-9/9h; AR-14b/14d). |

### Examiner-fixture corrections made during this exam (no verdict impact beyond crediting #295 correctly)

- **AR-18d** was failing on #295 because my fixture bound the receipt via a worktree row; #295 (correctly) binds via the task's commit event. Fixed the fixture to emit a commit-bearing `verification_passed` event. #295 then passes all AR-18; #294 still fails AR-18a/b/c with the positive control passing — confirming #294's bug was real.
- **AR-13c** assumed the proposal is written to the server checkout; #295 hydrates a separate isolated workspace. Re-expressed the invariant as "a valid safe-mode proposal must be verifiable with ≤1 paid call." Still fails #294 (verification never reachable), passes #295.
- **FO-6c** required specifically a `mainai_recovery_records` row; relaxed to accept a `reconcilable` observation event too, per R5's wording. Still fails #294, passes #295.

### Correction requirement for R1

Forward a genuine per-client identity. In `next start`, `request.ip` is unset — do not key on it. Use the real socket peer (e.g. read it in a small custom server, or trust a single named upstream's rightmost appended value) and set `X-Forwarded-For` to that. **Gate: PX-9, PX-10 and PX-11 must all pass**, with the control PX-10a still limiting the spraying client.

## Reproduce

```
probes/cursor_corrections_v1/run.sh ../wtcert-97e2b04 <built-frontend>   # needs <built-frontend>/../pw-browsers
probes/proxy_v1/identity_collapse.sh ../wtcert-97e2b04 <built-frontend>   # R1 alone
```

## Authoritative correction-gate result (97e2b04, all fixtures corrected)

```
FAIL R1 (P1)  PX-10, PX-11            # proxy forwards x-forwarded-for: unknown for every client
PASS P-1 R2 D1 D2 R3 R4 R5 R6 N2/N3
FAIL D4  UI-1, UI-2                    # stale "Backend: nåbar" after backend loss
FAIL D5  UI-3, AR-17                   # readiness BLOCKED with zero blockers
FAIL N1  AR-14a                        # invented "Nej" disagreement on a Founder question
FAIL N4  AR-15                         # "acknowledged" from a historical dispatch event
CORRECTION GATE: FAIL (5 defect gate(s) open)
```

Progress vs #294 (`27e945d`): 10 open → 5 open. R2, R3, R4, R5, R6 closed. The one release blocker is R1.
