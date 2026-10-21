# Independent examination — #293 (Founder workflow readiness)

- **Candidate:** PR #293 (draft), branch `cursor/founder-workflow-readiness-91c1`.
- **Examined SHA:** `ee132bb8e87fcc99f2f3607cfc2b46a5364d23c7`. It is "Frozen for independent examination" per the PR body, has 3 commits on #292 `5e3cd01`, and the ancestry is verified.
- **Head and CI:** confirmed through the unauthenticated public API. The head was unchanged at verdict time. All 12 required checks plus the aggregate are `success` in both the push and pull_request suites.
- **Examiner:** Claude, read-only.
  - Hermetic git; synthetic credentials only.
  - Provider recorders plus an egress guard.
  - No merge, deploy, Recall or paid calls.

## Verdict: RETURN_TO_BUILDER

#293 was built on #292 before the #292 verdict. It carries both #292 P1s, and it adds new truthfulness defects in the chat communication policy and in the "acknowledged" status.

| ID | Sev | Origin | Finding | Evidence |
|---|---|---|---|---|
| D1 | **P1** | #292 | Builder self-verification, and an unopened PR with zero checks, are shown as "Självständigt verifierad". | AR-11, AR-11b fail |
| D2 | **P1** | #292 | Concurrent dispatches, including over HTTP, make 2 paid calls. | AR-9, AR-9h fail |
| D3 | P2 | #292 | Concurrent delegation creates duplicate open requests (20/20). | AR-10 fails |
| D4 | P2 | #292 | When the backend is lost, `/admin/founder-ops` keeps showing "Backend: nåbar" and "STOP: Inte blockerad" under the error banner. | Live stack + built-in browser on ee132bb (backend killed, read after 9 s). Automated UI-1/UI-2 not run: the Playwright Chromium download stalled (setup, not evidence). |
| D5 | P3 | #292 | "Beredskap: BLOCKED" is shown next to "BLOCKERINGAR (0)". The new "NÄSTA ÅTGÄRDER (0)" panel likewise omits readiness. | AR-17 fails (readiness pinned BLOCKED → blockers `[]`); live page on ee132bb |
| N1 | P2 | #293 | **Invented disagreement.** The Founder question "Vad blockerar first-cycle GO?" gets a reply that starts "Nej. … Evidensen motsäger påståendet …". | AR-14a fails |
| N2 | P3 | #293 | A Recall claim is answered with the GitHub-write disagreement text. | AR-14b fails |
| N3 | P2 | #293 | The chat route feeds **hard-coded** evidence (`independently_verified: False`, `recall_http_mounted: False`) into the disagreement policy. | AR-14d fails |
| N4 | P2 | #293 | Any historical `dispatched` event makes a `ready` task with no live job show "acknowledged". This includes the FO-6 stranded task. | AR-15 fails |
| P-1/P-2 | P2 | pre-existing | Proxy traversal to `/openapi.json` and `/docs`; client `X-Forwarded-For` trusted, so the login per-IP limit is bypassed (rotating XFF). The proxy code is unchanged since #292. | PX-1, PX-2, PX-9 fail (known P2 in the cursor profile) |
| P-3 | P2 | pre-existing | Safe mode writes the paid code agent's output into the server's own checkout, and `safe_launch` does not show it. | AR-13b fails (known P2 in the cursor profile) |
| K-1 | P1 (carry) | pre-existing | **FO-6** (governed-goal paid retry loop) is still open. | `stop_classification`: same 4 blockers as #288–#292 |

**What holds:**
- AR-1: 131-route authorization matrix.
- AR-2..AR-8: STOP authorization, CSRF, rate limit, fail-closed reads, dependency failure, account isolation, Recall unmounted, GO never derived from flags, offline status reads.
- AR-12: safe-mode chat makes no egress, and the failed turn stays visible.
- AR-13: with the safe default, nothing is pushed and no PR is opened.
- AR-14c: a known cost is not rewritten as unknown.
- AR-16: every member of a dependency cycle refuses assignment, and the cycle is surfaced.
- Migration integrity: a single head `0089_activation_branch_protect` and a linear chain.
- Proxy PX-3..PX-6.
- 788 regression tests pass. Review/STOP, spend, task authority, credential canary and FC-4 are unchanged, with **no regression against #292**.

**Required:**
- D1/D2/D3 as listed in `../fc292/VERDICT_292_5e3cd01.md`.
- D4: mark stale cards and treat unreadable STOP as blocked.
- D5: include readiness reasons in blockers and next actions.
- N1: trigger disagreement only on a Founder *assertion*, never on a question.
- N2: use the correct Recall text.
- N3: pass real durable evidence, or no evidence at all.
- N4: `acknowledged` requires a currently held lease or claim, not a historical event.
- Gates: AR-9, AR-9h, AR-10, AR-11, AR-11b, AR-14a, AR-14b, AR-14d, AR-15, AR-17, and UI-1..UI-3 once a browser is available.

## Reproduce

```
git worktree add --detach ../wt293 ee132bb8e87fcc99f2f3607cfc2b46a5364d23c7
FE=$(probes/proxy_v1/build_frontend.sh ../wt293 <scratch>/fe293)
FE_BUILT=$FE REDIS_URL=redis://127.0.0.1:6390/0 probes/first_cycle_acceptance_v1/run_acceptance.sh cursor ../wt293
```

The UI suite needs Playwright Chromium in `<scratch>/fe293/pw-browsers`. Without it, the suite reports `SETUP_BROWSER_MISSING`.

## Addendum (2026-10-21)

**D3 is re-rated from P2 to P3 (latent).** The only production callers of `submit_delegation_request` pass no `task_id`:
- `workforce/vertical_slice.py`, reached from `mainai_executive/loop.py`;
- `workforce/first_delegation_scenario.py`, reached from `safe_internal_run.py`.

With no task ID, the duplicate guard returns early. The race (20/20 duplicates) is real in the guard, but no production path reaches it at ee132bb. AR-10 stays as a regression for future task-bound callers.

**D4/D5 now have executable browser evidence.** I ran Playwright (Chromium 141, `ui_v1`) against the live local stack at ee132bb:
- **UI-1.** After snapshot polls fail, `getByText('Backend: nåbar')` still resolves to 1 element.
- **UI-2.** After the backend is lost, the dashboard health line still reads "Backend nåbar · beredskap BLOCKED · STOP inaktiv · …".
- **UI-3.** With readiness BLOCKED, the page shows "BLOCKERINGAR (0)".

All three fail on the defect itself, not on setup. The verdict is unchanged: RETURN_TO_BUILDER.
