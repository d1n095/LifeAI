# Track B authority and truth-status audit (first-cycle composition)

**Independence disclosure.** Claude built Track B (#265 `39a7b81`, #267 `da4d96b`). This is a **builder self-audit** done at the Founder's request. It is not an independent examination and certifies nothing. Cursor's independent cross-examination of Track B remains required. The probes are published so they can be re-run independently.

## Scope
- Latest frozen Track B lineage: `2fbe20a → 39a7b81 (#265) → da4d96b (#267)`.
- Intended first-cycle composition, built locally and never pushed: `05df3db` (#271, Path A top) + `da4d96b` (#267) + `e33f494` (#263) = local `165122d`. Merges are clean apart from the two known additive conflicts (union-resolved).
- Read against Codex's activation-security spec `8a5bdaf` and composition audit `9d9dc9e`.

Method:
- Probes run the real worker, Path A execution job and Track B dispatch/session code.
- A fake `claude` executable acts, as an ordinary OS process, the way a CLI agent with file and shell tools could act.
- No live agent, provider, GitHub write, merge, deploy or Recall.
- One read-only GitHub API read was made earlier (default workflow token = `read`).

Probes: `probes/e2e_v1/test_track_b_authority.py` (TB-1..TB-7), plus E2E-D in `test_first_cycle.py`.

## Verdict: NOT activation-ready. 1 P0, 6 P1, 2 P2. Reviewer-only use is reachable after a short, ordered fix list (§6).

| ID | Sev | Finding | Evidence (probe → result) | Code |
|---|---|---|---|---|
| TB-3c / TB-7 | **P0** (activation) | The Path A worktree persists a reusable GitHub write credential: `origin = https://x-access-token:<token>@github.com/<repo>.git`. Every process run inside it can read the credential, and push with it: a Track B reviewer or builder, **and the model-written tests Path A executes during verification**. That leaks the token to the agent's provider transcript, and the one-shot push grant can be bypassed. | TB-3c: `remote.origin.url carries token=True; readable by the agent process=True`. TB-7: a model-written focused test read `.git/config` during `verify_task` and wrote it out; the token was present. | `mainai_execution/worktree.py:119-120, 196`; `execution_job.py` verify in `<worktree>/backend` |
| TB-1 | P1 | `agent_assignment` has **no envelope effect authority**. The only gates are Founder task approval and adapter enablement. The CLI was launched under a `path_a` envelope with no agent capability, and also with no envelope at all. Its cwd was an arbitrary directory outside every MainAI worktree; the envelope's `authorized_paths` were never consulted. | TB-1: both cases `cli_launched=True`, task `completed`. | `execution_job.py:693-697` (no `_envelope_for_path_a_task`); `session.run_agent_assignment_task` (`assignment.worktree_path` used as cwd) |
| TB-2 | P1 | Builder `allowed_paths` is **metadata only**. A `read_write` assignment (lease acquired, `allowed_paths=[backend/app/x.py]`, `--permission-mode acceptEdits`) wrote an out-of-scope file, `../escape_relative.txt`, an absolute path outside the checkout, and a file through a symlink pointing outside. All four landed. There is no post-run diff/scope check or discard. | TB-2: `files outside scope present={out_of_scope.py: True, ../escape_relative.txt: True, absolute: True, via symlink: True}` | `adapters.py` ClaudeCodeDialect `acceptEdits`; no post-run check in `dispatch`/`execution_control` |
| TB-3b | P1 | Reviewer read-only is **CLI policy only** (`--permission-mode plan` for Claude, `--mode plan` for Cursor, `sandbox_mode=read-only` for Codex). Nothing at OS/Git level stops the process: an edit and a `git commit` landed in the reviewer's checkout. Whether the real Claude CLI in plan mode would attempt or be permitted shell/Git is **UNKNOWN** (live verification not authorized). Only Codex's read-only sandbox is OS-enforced. | TB-3a HOLDS (`plan` in argv). TB-3b: `edit landed`, `HEAD subject='agent commit'`. | `adapters.py:308-370` |
| TB-4 | P1 | A PR reference is recorded as **merged**: `apply_dispatch_result` stores `merge_result="merged"` whenever `succeeded and pr_ref`, as *deterministic* intelligence evidence. Not reached by today's conversation flow (it never sets `pr_ref`), but the generic result API records false truth. | TB-4: `recorded deterministic evidence={"merge_result": "merged"}`, assignment `completed`. | `agent_coordination/dispatch.py:389` |
| TB-5 | P1 | **"Reviewed" is conflated with "review passed".** A reviewer answering `VERDICT: RETURN_TO_BUILDER` still completes the Path A `agent_assignment` task and unblocks its dependents; the verdict is never parsed. | TB-5: `agent_assignment=completed`, the dependent was dispatched. | `session.run_agent_assignment_task` returns the answer as `work_result`; `execution_job` completes the task |
| E2E-D | P1 | No exact-SHA review receipt: the reviewer's answer stays a self-report on the assignment, and `mainai_verification_records` rows = 0. | E2E-D (receipt) GAP; E2E-D (run, isolated HOME) HOLDS. | `mainai_verification_registry` unused by Track B |
| TB-6b | P2 | The env allowlist forbids only HOME/config names. It forwards secret-bearing names when configured: `SECRET_KEY` (the HMAC root for MainAI session ids), `GITHUB_TOKEN`, `DATABASE_URL`. | TB-6b: forwarded `['GITHUB_TOKEN','SECRET_KEY']`. | `adapter_config.py:73-76, 230-253` |
| — | P2 | Track B CLI cost is outside `provider_spend`; it is bounded only by `--max-budget-usd` × turn limit (Claude) or by the timeout (Cursor/Codex, which need explicit unbudgeted acknowledgement). | static | `adapter_config.py:162-199` |

**Reconfirmed (TB-6, E2E-D, 219 Track B tests on the composition: all passed)**
- Session ids are MainAI-derived (HMAC over owner + assignment + provider under `SECRET_KEY`) and never read back from storage or agent output.
- HOME is isolated: the Founder HOME and `~/.claude` are refused, and the CLI ran with the isolated HOME.
- Unsupported resume dialects (Cursor, Codex) fail closed.
- No secret is forwarded by default.
- Turn limit and per-follow-up re-authorization hold.
- **Track B contains no merge, deploy, push or Recall operation** (grep of `agent_coordination/`).

## 4. Truth-state trace (composition)
| State | Where it is set today | What it actually means | Problem |
|---|---|---|---|
| `completed` (repo_edit task) | `execution_job` after verify PASS + finalize | local targeted tests passed in the worktree, plus a non-force push whose remote SHA equals local HEAD | OK. It is not CI-verified until `open_pr` |
| PR opened | `_handle_open_pr` → draft PR; `open_pr` task `completed` only after `ci_wait` satisfied | a draft PR exists and the checks seen at that time passed | CI set is not yet exact (Codex spec §Required CI set) |
| reviewed | Track B assignment `waiting_review` → `close_agent_session(accepted)` → `completed`; the Path A `agent_assignment` task is `completed` as soon as the agent answers | "the agent answered" | **TB-5**: no verdict semantics |
| verified | `mainai_verification_records` exists, but nothing in Path A or Track B writes it | — | **E2E-D**: no receipt |
| merged | No Path A code sets it (the Founder merges manually). `dispatch.apply_dispatch_result` writes `merge_result="merged"` from `pr_ref` | — | **TB-4**: must be UNKNOWN unless GitHub reports `merged=true` for that PR and head SHA |
| goal `completed` | all tasks completed | draft PR + CI pass (+ review answered) | should be labelled "ready for Founder review", never "done/merged" |

## 5. Smallest exact-SHA review receipt (no migration)
Reuse `mainai_verification_records` through `mainai_verification_registry.record_verification_attestation`:
- `component_id` = repository `owner/repo`; `candidate_id` = `pr:<number>`.
- `candidate_sha` = the PR head SHA, **re-read from GitHub at record time** and equal to the reviewer checkout `HEAD` captured before launch. Any mismatch → no receipt.
- `builder_identity` = `mainai:path_a:task:<repo_edit task id>`; `examiner_identity` = `agent:<agent_key>:assignment:<id>`. Use `examiner_class="mainai_agent"`, `identity_assurance="mainai_bound_session"`. The existing independence check (builder ≠ examiner) is enforced.
- `review_result` comes only from a structured verdict line (`VERDICT: PASS|RETURN_TO_BUILDER`). Anything else becomes `inconclusive`.
- `source_provenance` = `{assignment_id, attempt_id, mainai_session_id, instruction_sha256, final_message_sha256, execution_envelope_id, reviewer_checkout_head, pr_head_at_record, adapter_mode, recorded_by}`.
- A receipt is **evidence only**. It grants no push, PR, merge, deploy or Recall authority, and is superseded when the PR head moves.

## 6. Activation-security compatibility (Codex spec `8a5bdaf`)
- **Envelopes / effect authority:** Track B has none (TB-1). Add an `agent_dispatch` effect, i.e. envelope capability `agent_dispatch` + role, alongside `push`/`open_pr`. In v1 allow **reviewer + read_only** only; builder stays disabled in Path A.
- **One-shot push grant:** bypassable while any worktree holds a credential (P0). A credential-free origin is a precondition of the grant design.
- **Immutable activation SHA:** the reviewer checkout must be created at the exact PR head SHA from the approved base lineage, and the receipt binds it (§5).
- **No merge/deploy/Recall:** holds for Track B. TB-4 must not leak a "merged" truth claim into downstream consumers.

## 7. Remediation order (implementation-ready)
1. **P0 — credential-free worktrees** (`worktree.py:196`).
   - `origin = https://github.com/<repo>.git`.
   - Inject the token only for the single fetch/push command: `GIT_ASKPASS` helper or `git -c http.extraHeader=...` passed via env, never written to `.git/config`.
   - Assert after creation that `git config --get-regexp url|remote` contains no credential.
   - Run `verify_task` with a scrubbed env and no credential on disk.
   - This is a prerequisite of Codex spec step 7 (push grant).
2. **P1 — `agent_assignment` authority.** Path A handler requires the current active envelope with `path_a` + `agent_dispatch` and role=reviewer, mode=read_only. The workspace must be a MainAI-owned, credential-free review checkout at the exact PR head SHA (ownership marker verified). Refuse `read_write` in Path A (builder disabled for v1).
3. **P1 — reviewer read-only enforcement independent of the CLI.**
   - Snapshot `HEAD` and `git status --porcelain` before; require both unchanged after.
   - Any change invalidates the review and discards the checkout.
   - Fresh disposable checkout per review. Use Codex `read-only` sandbox where available.
4. **P1 — truth states.**
   - `dispatch.py:389` must never infer merged: `merge_result` stays None/unknown unless authoritative GitHub merge evidence is supplied.
   - Parse a structured verdict. A negative or unparseable verdict fails/blocks the `agent_assignment` task, so dependents do not unblock.
5. **P1 — exact-SHA receipt** per §5, written when the reviewer verdict is PASS.
6. **P2** — deny secret-bearing env names in the allowlist (`SECRET_KEY`, `DATABASE_URL`, `GITHUB_TOKEN`, `*_PASSWORD`, `*_SECRET*`, `*_TOKEN`, except the agent's own provider key variable). Record the Track B CLI budget in the spend audit.
7. **Later, before any builder use:** post-run scope check (changed paths ⊆ `allowed_paths`, no symlinks, nothing outside the checkout via a before/after snapshot) plus an OS sandbox.

Regressions: TB-1..TB-7 and E2E-D (receipt) flip to HOLDS. Keep TB-3a, TB-6, E2E-D (run) and the 219 Track B tests.

## Reproduce
```
git worktree add --detach ../wtTB 05df3db2cee1aad3607543e548d6fd9cd2c36d31
git -C ../wtTB merge --no-edit da4d96b2df9ef3345890e990e750c7bb15005040
git -C ../wtTB merge --no-edit e33f494c4fc53704c494cecea788f3e22be36330   # merge=union for docs/BRANCH_REGISTRY.md, tests/backend/test_github_client.py
REDIS_URL=redis://127.0.0.1:<port>/0 probes/e2e_v1/run.sh ../wtTB test_track_b_authority.py -k tb    # local Postgres :5433 + Redis
REDIS_URL=... probes/e2e_v1/run.sh ../wtTB -k e2e_d
```
