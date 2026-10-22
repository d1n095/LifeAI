# Independent operational-handover examination — #298 (Durable Agent Coordination & Artifact Handoff)

- **Candidate:** PR #298 (draft, FROZEN per PR body), branch `cursor/durable-agent-coordination-91c1`.
- **Examined SHA:** `76f2ad6818f3faab0603ceff03a06bd4c3842d43`. Two commits on #297 `50a1437`; ancestry verified.
- **Delta:** `backend/app/agent_coordination/durable.py` (1359 lines: obligation ledger + artifact handoff), founder-ops snapshot hook, `/admin/founder-ops` view, 572 lines of candidate tests.
- **Examiner:** Claude, read-only. Credential-free, no paid calls, no merge/deploy/Recall.

## Handover question: can MainAI coordinate three agents without the Founder forwarding routine messages?

**YES for coordination mechanics — BLOCKED overall on Codex STOP/Security-Kernel hardening.** The durable coordination layer that was MISSING through #297 (my CO-4b/CO-5) is now **implemented and passes every operational scenario I could test**. It does not, and by design cannot, grant the authority that is still unhardened; the remaining handover blocker is Codex's, not coordination's.

## Operational scenarios (handover_v1, 12/12 pass against the real `DurableCoordinationService`)

| # | Scenario | Result | Evidence |
|---|---|---|---|
| 1 | Three agents, distinct ownership/identity/status/chronology | **PASS** | HO-1: cursor/codex/claude obligations distinct in `founder_view` |
| 2 | Stale completion / queue-empty after a newer assignment | **PASS** | HO-2 (queue-empty refused while active); HO-2b (completion for a superseded generation → `stale_rejected`) |
| 3 | Only agents whose latest work ended are reassignable | **PASS** | HO-3 (same canonical work while active → idempotent, same id) |
| 4 | Duplicate reports / replayed instructions | **PASS** | HO-4 (replayed `event_id` → `replay_ignored`) |
| 5 | Repeated planning detected as stalled, no new plans | **PASS** | HO-5 (5× planning → `stalled`, still 1 obligation) |
| 6 | Completion needs execution evidence | **PASS** | HO-6 (planning evidence as execution → `invalid_evidence`) |
| 7 | Completed candidate → independent review, not builder | **PASS** | HO-7 (builder self-verify rejected; independent examiner accepted) |
| 8 | Artifact handoff: checksum+provenance; private channel BLOCKED | **PASS** | HO-8 (in-process delivered; tampered payload rejected on sha256; `vendor_private_workspace` / `github_publication` / `personal_credentials` → BLOCKED with named missing capability) |
| 9 | Consolidated Founder view | **PASS** | HO-9 |
| 10 | Cannot forge approval / override STOP / alter kernel / impersonate | **PASS** | HO-10 (`escalate_privilege` always rejected, `owner=codex_founder_authority`); HO-10b (cross-agent completion → `forged_rejected`) |

Plus: the candidate's **own 16 coordination tests pass** in one uninterrupted run. My earlier CO-4b/CO-5 gaps (MISSING on #297) now **PASS** on #298 and still MISSING on #297 — the ledger + authorized transfer are genuinely implemented.

## npm-audit failure and exact-SHA freeze (independently checked)

- **Required checks:** 11/12 green on **both** suites. `Frontend — npm audit` is `failure` on the **push** suite only, `success` on pull_request; the aggregate `All required checks passed` is therefore `failure` on push.
- **Root cause confirmed transient-infra, not a vulnerability:** `frontend/package-lock.json` is **byte-identical to #297** (no dependency change in #298), and #297's npm-audit passed. The PR body attributes the push failure to a `503 Service Unavailable` from `registry.npmjs.org`. This is an infrastructure flake, not a code defect.
- **Freeze condition:** the PR declares FROZEN and relies on the pull_request suite's greens. **Strictly, the exact-SHA CI is not uniformly green** (push npm-audit failed). This is a **CI re-run / FREEZE-hygiene item**, not a RETURN_TO_BUILDER code defect: a re-run of the push npm-audit job (no code change needed) should clear it.

## No regression

- `security_diag_v1`: **10/10 pass**. `coord_accept_v1`: CO-1..CO-9 pass; CO-4b/CO-5 now pass (implemented).
- **CO-10 / FA-9 still FAILS** (STOP-clear gated only by a `founder_ack` string) — correctly **not** closed by this PR; Codex-owned.
- Inherited Cursor D4 home-dashboard (P3) and the legacy FA-PRIV (Codex) are untouched.

## Verdict: PASS/FREEZE (coordination layer) — with a CI-hygiene caveat; overall first-cycle still NO-GO on Codex

- **The #298 coordination implementation is accept-worthy on its own merits** (all 12 operational scenarios + 16 candidate tests pass; no new defect found). It correctly refuses every authority action and delegates STOP/kernel to Codex.
- **FREEZE caveat:** re-run the push-suite `Frontend — npm audit` so the exact-SHA aggregate is uniformly green; no code change is required (lockfile identical to #297). Until then the freeze is not strictly satisfied on the push suite.
- **The operational handover is still BLOCKED overall**, but **not by coordination** — by the Codex-owned STOP-resume integrity (FA-9/CO-10) and Security-Kernel DB enforcement (FA-PRIV). Those are the shortest path.

## Shortest path to an operational handover

1. **CI hygiene (Cursor/infra):** re-run push npm-audit → exact-SHA green. *Trivial; no code.*
2. **Codex — STOP-resume integrity (FA-9/CO-10):** bind STOP-clear to a reauth/signed token. *This is the single remaining safety gate between "coordination works" and "the Founder can stop manually coordinating".*
3. **Codex — Security-Kernel DB enforcement (FA-PRIV):** so authority/approval/audit hold independently of the runtime role.

Genuine launch blockers: #2, #3 (Codex). Later enhancements (not handover blockers): the D4 home-dashboard P3 (Cursor); richer dependency-graph views.

## Reproduce
```
probes/handover_v1/run.sh ../wtcert-76f2ad6                 # HO-1..HO-10 (+controls)
probes/coord_accept_v1/run.sh ../wtcert-76f2ad6            # CO-4b/CO-5 now pass; CO-10 still BLOCKED
# candidate's own: cd backend && pytest tests/backend/test_durable_agent_coordination.py  -> 16 passed
```
