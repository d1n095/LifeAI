# First-cycle adversarial acceptance harness

Isolated test/evidence lane. Not a production implementation. Not an audit memo.

This harness is the GO/NO-GO rehearsal for composing:

- frozen Path A `#276` (do not amend);
- Claude activation-readiness `#277` (Claude is correcting it);
- Codex activation-security (push grants, credential-free remotes, exact-SHA CI).

It must run against a later composed candidate **without changing that candidate's production code**.

Frozen examiner SHAs this harness does not modify:

- `#276` independent review PASS/FREEZE: `8ddaaee94fa1637b489125aee047a5726a5641f8`
- `#277` independent exam RETURN_TO_BUILDER: `f3abe631e23ff77ed059ae712a3f76afc87251b8`

Base of this branch: frozen `#276` tip `09b87e9b70dc02889d39c2bae3d56458509d49cb`.

## What this is

Executable tests under `backend/tests/backend/first_cycle_acceptance/`:

- a deterministic, network-free first-cycle rehearsal (fake provider responses, fake spend, local git + bare remote, intercepted GitHub REST, controlled check-run fixtures, isolated worktrees);
- mandatory publication attacks from Claude `#276` P2 (TA-12, TA-13, gitlinks, dirty trees, out-of-scope deletions, exact history);
- real-subprocess credential isolation;
- Founder grant attacks against a harness-local contract, with production binding marked `BLOCKED` until Codex publishes interfaces;
- spend containment against production `billed_execution_chat` / `provider_spend`;
- exact-set CI + review-receipt STOP.

Unimplemented production boundaries **FAIL** or **BLOCKED**. They are never mocked into a fake PASS.

## Reproduction

No live paid provider. No GitHub writes. No merge, deploy, or Recall.

From the repository root, with the usual backend test Postgres/Redis (same as `tests/backend`):

```bash
cd backend
export LIFEAI_TEST_DATABASE_NAME=lifeos_fc_accept
export DATABASE_URL=postgresql://lifeos@127.0.0.1:5432/${LIFEAI_TEST_DATABASE_NAME}
export APP_DATABASE_URL=postgresql://mainai_app:mainai_app@127.0.0.1:5432/${LIFEAI_TEST_DATABASE_NAME}
export FIRST_CYCLE_ACCEPTANCE_MATRIX=/opt/cursor/artifacts/first_cycle_acceptance_matrix.json
python -m pytest tests/backend/first_cycle_acceptance -q --tb=short
```

If `mainai_app` uses a different password locally, set `APP_DATABASE_URL` to match. The session fixture still migrates a disposable database.

Path A regression (feasibly overlapping, still no live APIs):

```bash
python -m pytest tests/backend/test_mainai_execution_path_a_first_cycle.py -q --tb=short
```

Expect the acceptance suite to be **red on frozen `#276`**. That is the point: current production does not yet meet first-cycle GO. `go` in the matrix is true only when every row is `PASS`.

## Status vocabulary

Closed set, written to `docs/examiner/first_cycle_acceptance_matrix.json` and `$FIRST_CYCLE_ACCEPTANCE_MATRIX`:

| Status | Meaning |
|---|---|
| `PASS` | Production (or a bound production helper) met the bar, or the harness oracle itself held. |
| `FAIL` | Production was exercised and did not meet the bar. Pytest fails the test. |
| `BLOCKED` | Required production interface is not present. Do not invent compatibility. Pytest passes after recording. |
| `UNKNOWN` | Founder/GitHub evidence outside this process is required. |

Each row records `test_id`, `boundary`, `candidate_sha`, `production_module`, and `detail`.

## How to compose later

1. Check out the composed candidate (Path A + activation-readiness + Codex activation-security) as a worktree. **Do not copy this harness into that production tree by editing production modules.**
2. Either merge this test-only branch, or copy `backend/tests/backend/first_cycle_acceptance/` and `docs/examiner/FIRST_CYCLE_ACCEPTANCE_HARNESS.md` as a pure test addendum.
3. Set `FIRST_CYCLE_ACCEPTANCE_SHA` to the composed candidate's 40-hex SHA if the tests run from a dirty tree.
4. Re-run the commands above.
5. GO only if `go: true` in the matrix (zero `FAIL` / `BLOCKED` / `UNKNOWN`, at least one `PASS`).

The harness binds Codex symbols when they appear:

- `app.mainai_execution.push_grants`
- `app.mainai_execution.activation_security`
- `app.mainai_execution.remote_effect`

Until one of those modules exists, grant **production** rows stay `BLOCKED`. The harness-local `GrantStore` is the executable contract, not a claim that production implements it.

CI acceptance uses the 12 GitHub Actions checks, app slug `github-actions` / id `15368`, success-only conclusions, and both push and `pull_request` suites. That bar matches `#277`'s exact-set intent. Frozen `#276` still has legacy `evaluate_check_runs` (skipped/neutral pass). That is a `FAIL`, not a patched PASS.

## Simulated lifecycle

`run_rehearsal()` in `_lifecycle.py`:

Goal → Founder authorization → planning → repo edit → verification → local commit → exact-SHA push grant → simulated push → remote observation → **separate** draft-PR authorization → exact-SHA CI → review receipt → STOP.

The simulated effect executor is the only process that may arm a one-use test credential / grant file. Worker, test, and reviewer subprocesses must not. Worktree `.git/config` must not contain a token. The bare remote's `pre-receive` hook refuses ungranted pushes and requires the incoming SHA to match the grant. That hook is a **test double** for remote enforcement, not production code to copy.

## Remaining production dependencies (not implemented here)

These must exist on the composed candidate before GO. This PR does not implement them.

1. Exact-SHA, one-use **push** grant distinct from a **draft-PR** grant (Codex). Bindings: missing approval, wrong owner/goal/task/job/worktree/branch/repo, stale base SHA, changed local commit, consumed/expired/cancelled grant, restart, concurrent consume, unknown network + exact remote-tip reconciliation.
2. Credential-free worktree remotes. `_authed_remote_url` must not embed `GITHUB_TOKEN` in `.git/config`. Credential helpers and Git env injection must not supply authority. Cancellation/recovery must not revive credentials.
3. Publication of **full history** `base..pushed_sha`: exactly one commit; regular-file-only objects; no symlink (TA-13); no gitlink/submodule; no untracked/staged discrepancy; deleted out-of-scope paths fail; intermediate commits cannot conceal out-of-scope paths behind a clean final diff (TA-12).
4. `#277` exact-set CI wait (`evaluate_required_check_runs`), success-only, both suites, wrong app/SHA/missing/skipped/neutral/Vercel extras.
5. Review receipt STOP: negative review, changed PR head, reviewer ≠ builder, missing receipt.
6. No paid replan after inherited CI failure.
7. Production `push_worktree_branch` / `_finalize_repo_edit` must not push without a live exact-SHA grant.

Do not modify `#276`, `#277`, Codex's branch, FC-4, or Track B production code to satisfy these from this lane.

## Layout

```
backend/tests/backend/first_cycle_acceptance/
  _adapters.py          production import binding
  _matrix.py            PASS/FAIL/BLOCKED/UNKNOWN
  _git.py               isolated git + grant hook
  _publication.py       first-cycle publication oracle
  _grants.py            harness grant contract
  _ci.py                exact-set CI + review oracle
  _credentials.py       subprocess probes + one-use askpass
  _lifecycle.py         Goal→STOP rehearsal
  test_e2e_simulated_cycle.py
  test_push_content_attacks.py
  test_credential_isolation.py
  test_founder_grants.py
  test_spend_containment.py
  test_ci_and_review.py
  test_matrix_export.py
```
