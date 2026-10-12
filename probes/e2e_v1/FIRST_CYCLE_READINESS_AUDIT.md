# MainAI first supervised self-development cycle — end-to-end readiness audit

Date 2026-10-12. Examiner: Claude (read-only; no production code changed, no paid API, no GitHub write, no merge/deploy/Recall).
Audited composition (local only, never pushed): `2fbe20a` + #264/#266 (`dba9390`) + #265/#267 (`da4d96b`) + #263 (`e33f494`) = local `74cc485`.
Path A + Track B merge cleanly. #263 conflicts (additive) with #264/#266 in `tests/backend/test_github_client.py` and `docs/BRANCH_REGISTRY.md`.

## Verdict: NO-GO for the first supervised cycle (6 first-cycle blockers; all fixable in one Path A batch plus Founder settings)

## 1. Execution-path map (critical handoffs)
| # | Handoff | Code | Evidence | State |
|---|---|---|---|---|
| H1 | Founder → goal | `POST /api/mainai/execution/goals` → `planner.create_goal` | E2E | works |
| H2 | Founder → envelope + spend grant | `execution_envelopes.authorize_execution_scope`, `provider_spend.authorize_provider_spend` (grant requires `execution_envelope_id`) | E2E-A | works |
| H3 | Goal → AI plan | `POST .../plan` → `propose_plan_via_ai` → `billed_execution_chat` | E2E-A/B | works; prompt has no target files and no `agent_assignment` |
| H4 | Plan → tasks | `create_plan` (task types, DAG, plan-time targeted tests) | TEST-1 | works |
| H5 | Worker routing | `worker._advance_mainai_execution_tasks` excludes ever-envelope goals; `_advance_authorized_supervisor_goals` | ROUTE-2, E2E-A | **BLOCKED** for billable goals |
| H6 | Approval gate | `executor.dispatch_ready_task` → `require_task_approval` (unknown types need approval) | hook tests | works |
| H7 | Job → AI edit | `_handle_repo_edit` → `prepare_code_agent_prompt` → `billed_execution_chat` → `_assert_repo_edit_writes_allowed` | E2E-B/B2, CTX | **BLOCKED** for realistic plans (new test file unnamed) |
| H8 | Spend accounting | `billed_chat`: provider call → allowlist/price check → reserve → settle | SPEND-1..7 | **SPEND-7 open** |
| H9 | Worktree, edit, commit | `worktree.create_task_worktree` (configured base, ownership marker), `commit_worktree_changes` | E2E-B2 | works |
| H10 | Targeted tests | `verify_task` in `<worktree>/backend` | E2E-B2 | works |
| H11 | Push + remote SHA | `push_worktree_branch` (non-force, `get_ref` == local HEAD) | E2E-B2 | works |
| H12 | Draft PR | `_handle_open_pr` → `create_pull_request(draft=True)`, base = configured | E2E-B2 | works (0 Contents/ref writes) |
| H13 | CI wait | `ci_wait.start_ci_wait` / `poll_ci_wait` → `evaluate_check_runs` (any failure = failed) | E2E-B2 / E2E-C | **BLOCKED** by the inherited npm-audit failure |
| H14 | Failure → retry/replan | `max_attempts=3`, `MAX_AUTO_REPLANS=3` (paid calls each) | E2E-C | amplifies H8/H13 |
| H15 | Independent review | Track B `agent_assignment` → `run_agent_assignment_task` → fake `claude` (isolated HOME, MainAI session) | E2E-D | works; requires service calls (no route) |
| H16 | Verification receipt | `mainai_verification_registry` | E2E-D | **not connected** (0 rows) |
| H17 | Recovery | `recovery_inspector` / salvage / idempotent push and PR | prior suites | works |
| H18 | Audit trail | task events, spend events, assignment audit events, draft PR | E2E | works (Track B CLI spend outside ledger) |

## 2. Blocker register
### First supervised cycle — P1 (blocking)
| ID | Finding | Evidence (repro) | Owner |
|---|---|---|---|
| FC-1 ROUTE | A billable goal (envelope + grant, required by default) never reaches Path A. The worker excludes it; the Supervisor captures it and the job stays `running`. | E2E-A; `integration_v1` ROUTE-2 | Cursor (Path A) |
| FC-2 CTX×planner | The AI planner neither asks for nor parses `target_files`. A realistic edit (module + new focused test) is refused: "Refusing to edit 'backend/tests/test_greeting_doc.py'". #266's own demos fail the same way, so #266 CI is red. | E2E-B; CI `dba9390` unit/integration=failure | Cursor |
| FC-3 SPEND-7 | Paid answers that are then refused (allowlist, unpriced, over-hold) are never counted: 3 paid, 0 counted, per case. | `path_a_v1` SPEND-7 | Cursor |
| FC-4 CI gate | Every branch fails `Frontend — npm audit` and the aggregate check, so every MainAI PR's CI wait ends failed → `retryable_failed`, then paid retries/replans. | E2E-C | Founder decision + Cursor |
| FC-5 Envelope scope | Path A never reads the envelope's `authorized_paths` or capabilities; once FC-1 routes envelope goals to Path A, edits are not confined to the authorized scope. | static: no use of envelope fields in `mainai_execution/` | Cursor (with FC-1) |
| FC-6 Prerequisites | No branch protection or rulesets on any branch; worktree/PR base default to stale `claude/det-kommer-mer-879lcm`; write token scope unverified; the grant's allowlisted model must have a price entry. | `gh api .../protection` 404; config defaults | Founder |

### First cycle — P2 (not blocking under supervision)
- P2-1: the Track B reviewer's verdict is not a verification receipt bound to the PR head SHA (E2E-D). The first cycle uses the external examiner and a manual record.
- P2-2: no API to bootstrap coordination agents or create assignments (E2E-D used services). Track B needs an operator script.
- P2-3: Track B CLI cost is outside `provider_spend` (bounded by `--max-budget-usd` × turns).
- P2-4: #263 vs #264/#266 conflicts (additive) need a defined integration order.
- P2-5: two `test_scoped_development_supervisor` tests fail locally on the base too (CI green); environment-specific.
- P2-6: the hard-coded `tests/backend/test_ok.py` default; the test conftest disables the spend gate for most modules.

### Later (autonomous / unattended) — not first-cycle
Runtime-writable authority rows (approval events, envelopes, spend grants); coordination-store DB guarantees; Founder credential root of trust (Sovereignty P1); Continuous Conversation P1s; Supervisor/Operator publish-and-review handoff; keychain-isolated agent credentials; Cursor/Codex resume verification; merge policy and unattended scheduling.

### Verified working (no action)
Draft-only PR path; verified push SHA; task branch never equals a base; legacy Contents-API route gated off; spend gate on by default; allowlist and unpriced refusal; blind/truncated-edit refusal; plan-time targeted-test enforcement; Track B MainAI-owned sessions, isolated HOME, turn limit and enablement re-check; full worker cycle with named files (E2E-B2).

## 3. Remediation batches (dependency order)
- **B0 — Founder (parallel, no code):**
  - Enable a branch protection or ruleset on the integration and default branches.
  - Designate the integration base (`GITHUB_WORKTREE_BASE_BRANCH`, `GITHUB_PR_BASE_BRANCH`).
  - Issue a fine-grained token (contents + pull_requests, this repo only).
  - Decide the CI gate: land the npm-audit remediation on the base, OR have CI wait evaluate only Founder-designated required checks.
- **B1 — Cursor, one Path A batch on #266:**
  - FC-3: reserve before the call; record refused paid spend in a transaction that survives the job rollback.
  - FC-1 + FC-5: dispatch Path A only for an active envelope explicitly authorizing Path A; enforce the envelope's `authorized_paths`/capabilities on every write; exclude that goal from the Supervisor; fail closed otherwise.
  - FC-2: planner prompt + parser carry `target_files`; verification-plan test targets map to editable paths (`backend/` prefix); update the demos so #266 CI is green.
  - FC-4: the CI-gate change per the B0 decision.
  - Regressions: E2E-A, E2E-B, E2E-C, SPEND-7, ROUTE-1/2.
- **B2 — Integration:** compose #263 + Path A (B1) + Track B (#267) on the integration base; resolve the additive conflicts; run the full regression and these E2E probes on the composed SHA.
- **B3 — Independent cross-examination** of the composed SHA (Path A by Claude; Track B by Cursor), plus CI green.
- **B4 — Supervised activation:**
  - Track B Claude-only smoke test (dedicated API-key login, isolated HOME).
  - Then one Path A goal ("docstring + focused test") with an envelope + grant ≤ $1.
  - The examiner reviews the draft PR at its exact SHA; the Founder merges manually.

## 4. GO criteria for the first supervised cycle
1. E2E-A HOLDS on the composed SHA: an authorized, billable goal reaches a verified push + DRAFT PR against the configured base, with every paid request counted.
2. E2E-B HOLDS: a realistic AI plan (module + new focused test) is accepted, and writes stay inside the envelope's `authorized_paths`.
3. E2E-C resolved per the B0 decision: no CI-driven paid replans, and the outcome reflects only the required checks.
4. SPEND-1..7, CTX-1/2, TEST-1, ROUTE-1/2 all HOLD; Track B probes HOLD.
5. CI green for the composed candidate (npm audit per the B0 decision); independent PASS verdicts recorded.
6. B0 prerequisites in place: protection/ruleset, configured base, scoped token, a priced and allowlisted model, real `SECRET_KEY`, `GITHUB_WRITE_ENABLED` on only for the run.

## Reproduce
```
git -C repo worktree add --detach ../wtcompose dba9390a6ea2a167300bfd87a52922b6b5153f54
git -C ../wtcompose merge --no-edit da4d96b2df9ef3345890e990e750c7bb15005040
git -C ../wtcompose merge --no-edit e33f494c4fc53704c494cecea788f3e22be36330   # resolve additive conflicts: keep both sides
REDIS_URL=redis://127.0.0.1:<port>/0 probes/e2e_v1/run.sh ../wtcompose          # local Postgres :5433 + Redis
```
