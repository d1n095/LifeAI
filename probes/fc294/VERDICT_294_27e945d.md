# Independent examination — #294 (Founder-workflow P1 corrections)

- **Candidate:** PR #294 (draft), branch `cursor/founder-workflow-p1-91c1`.
- **Examined SHA:** `27e945db8ee414c813248bbeb9e7462b93903cee`. Five commits on frozen #293 `ee132bb`; ancestry verified.
- **Freeze / CI:** see "Exact-SHA status" at the end.
- **Examiner:** Claude, read-only.
  - Hermetic git; synthetic credentials only.
  - Provider recorders plus an egress guard.
  - No merge, deploy, Recall or paid calls.
- **Evidence:** a full `combined` run through `certify_v1/intake.sh`: frontend build, real backend with the Dockerfile flags, `next start`, Playwright Chromium. See `prefreeze_combined_27e945d/`.

## Verdict: RETURN_TO_BUILDER — one new P1 (release blocker)

### What #294 fixed (verified executable)

| Item | Result |
|---|---|
| **FO-6 paid retry loop (P1)** | **CLOSED.** FO-6x, RL-1..3 and `untyped_boundary_exception` pass. No paid re-run after a bare error or crash at the push boundary; the task ends terminal. |
| D1 builder self-verification shown as independent | Fixed for the builder path. AR-11 and AR-11b give `completed`. The registry is SELECT-only for the runtime role, so the runtime cannot forge receipts. A residual is listed below (R2). |
| D2 duplicate paid dispatch | Fixed. AR-9 and AR-9h: one paid call; the losing request gets 409. |
| N2 Recall text, N3 hard-coded chat evidence | Fixed. AR-14b passes; AR-14d (behavioural) passes. |
| P-1 proxy traversal | Fixed. PX-1 passes: encoded `..` gets 400 and never reaches the backend. |
| P-3 safe-mode writes to the server checkout | Fixed. AR-13b passes: the proposal is written to an isolated workspace. |
| Unchanged | 788 regression tests pass. Spend, review/STOP (22 pass), migration integrity (single head), FC-4 and the authorization matrix (AR-1) all hold. |

### Defect ledger

| ID | Sev | Origin | Defect | Reproduction |
|---|---|---|---|---|
| **R1** | **P1** | #294 | **Rate-limit identity collapse, which gives a global Founder login lockout.** `route.ts` strips *every* `X-Forwarded-For`, `Forwarded` and `X-Real-IP`, including the one Next.js sets from the real socket. uvicorn (`--proxy-headers --forwarded-allow-ips=*`) then sees the proxy as the only client, so every anonymous caller shares one limit key. Ten failed logins a minute from anyone return 429 to the Founder from any other address. | `proxy_v1/identity_collapse.sh`: dual-stack `next start`. Client A (127.0.0.1) sprays 12 logins against other accounts and is limited at 429 (control). Client B (::1) then logs in to the Founder account with the correct synthetic password: **429**. On #293 the same check returns **200**. |
| R2 | P2 | #294 (D1 residual) | The independent receipt is not bound to the task, and invalidation is ignored. (a) A PASS that a later FAIL explicitly invalidates (`invalidates_verification_id`) still shows "Självständigt verifierad". (b) A PASS for an unrelated SHA verifies a task whose produced commit is a different SHA. | AR-18a, AR-18b |
| R3 | P2 | #294 (D2 regression) | **A failed paid dispatch strands the task.** `dispatched` is committed before the provider call. On a provider failure the HTTP response is 503, the task stays `dispatched`, the Founder sees "Kör" (running), and every retry gets 409. | AR-9f: `first=503 status_after_failure=dispatched founder_label=running retry=409` |
| R4 | P2 | #294 (regression vs #293) | **The Founder's cancel is overwritten.** The new crash-finalizer in `worker.py` marks a task whose Founder cancel is pending as `failed` with reason "external publication failure", not `cancelled`. Safety holds: one job, no paid call after the cancel. | FC-1: `task=failed … cancel_requested → verification_failed{unexpected_worker_crash} → failed{"external publication failure"}` |
| R5 | P2 | FO-6 residual | A bare error or crash at the push boundary is finalized as `failed` with **no recovery or observation record**. An ambiguous push (one that may have reached the remote) is never reconciled against the exact remote ref, contrary to the spec rule "observe the exact ref, never replay". | FO-6c: `no Founder-visible recovery record` |
| D4 | P2 | #292, **not fixed** | With the backend lost, `/admin/founder-ops` keeps "Backend: nåbar" and the dashboard keeps "Backend nåbar · beredskap okänd (fail-closed) · STOP inaktiv". | Playwright UI-1 and UI-2: genuine assertions (`Received: 1`; the received string is quoted above) |
| D5 | P3 | #292, not fixed | Readiness BLOCKED is shown with `blockers=[]`. | AR-17 |
| N1 | P2 | #293, not fixed | The Founder *question* "Vad blockerar first-cycle GO?" gets the reply prefix "Nej. … Evidensen motsäger påståendet …". | AR-14a |
| N4 | P2 | #293, not fixed | A historical `dispatched` event makes a re-queued `ready` task show as "acknowledged". | AR-15 |
| R6 | P3 | #294 | Safe-mode proposals are written to an empty isolated workspace, but verification runs `pytest` in the live checkout. Every safe-mode `repo_edit` therefore fails verification. This is safe (no false PASS, one paid call) but non-functional. | AR-13c: `verification=['verification_failed'] proposal_files_in_verified_tree=[] code_agent_calls=1` |
| D3 | P3 latent | #292 | Fixed for task-bound callers (advisory lock). No production caller passes a `task_id`. | AR-10 passes |
| K | — | Codex-owned, unchanged | CRED-1/2/3 (TC-1, TC-3), E2E-3, GRANT-PROD, TA-12, TA-13, PUB-2. GC-1..GC-8 are BLOCKED (no grant API in this lineage). | `combined` matrix |

### Correction requirements for Cursor

- **R1:** forward exactly one trustworthy client address. In the route handler, set `X-Forwarded-For` to the **peer address of the incoming socket**, or to the rightmost entry appended by a known edge proxy, and drop only the *client-supplied* values. Gate: PX-9 (rotating XFF cannot bypass) **and** PX-10 (distinct clients keep distinct limits) must both pass.
- **R2:** bind the receipt to the task's published or committed SHA, for example the worktree `current_commit` or the PR head. Exclude records that a later record invalidates or supersedes. Gates: AR-18a, AR-18b.
- **R3:** if the paid call fails, roll back to a re-dispatchable status (or a distinct `dispatch_failed`). Never leave a task `dispatched` without a live attempt. Gate: AR-9f.
- **R4:** the crash-finalizer must honour a pending Founder cancel, giving `cancelled`, and must not label a worker crash an "external publication failure". Gate: FC-1, FC-2.
- **R5:** after an ambiguous publication-boundary crash, record a recovery or observation entry, observe the exact remote ref, and reconcile. Never replay. Gate: FO-6b/c.
- **D4, D5, N1, N4, R6:** see the #292 and #293 verdicts, and AR-13c for R6. Gates: UI-1, UI-2, AR-17, AR-14a, AR-15, AR-13c.

### Reproduce

```
probes/certify_v1/intake.sh 27e945db8ee414c813248bbeb9e7462b93903cee   # needs PW_BROWSERS for the UI suite
probes/proxy_v1/identity_collapse.sh ../wtcert-27e945d <built-frontend>   # R1 alone
```

## Exact-SHA status (final)

- **Head:** `27e945db8ee414c813248bbeb9e7462b93903cee`, unchanged at verdict time (public API).
- **CI:** all 12 required checks plus the aggregate are `success` in both the push and pull_request suites at this SHA. The PR's own freeze condition ("not frozen until all 12 required GitHub CI checks are green on this head") is therefore met at this SHA.
- **The examination run is valid for this SHA.** It ran on exactly this commit, and its only CI blockers were the then-in-progress backend job and the aggregate. Both are now green.
- **The CI row of the GO/NO-GO matrix is now GO.** Every other NO-GO row is unchanged.

**Verdict: RETURN_TO_BUILDER at `27e945db8ee414c813248bbeb9e7462b93903cee`.** The release blocker is R1 (P1).
