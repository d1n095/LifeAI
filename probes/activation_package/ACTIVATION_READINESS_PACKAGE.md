# Activation integration readiness & rehearsal package — first Founder-supervised MainAI cycle

Date 2026-10-17. Examiner: Claude, read-only / evidence-only.
- No production code, GitHub setting, branch, credential or signature was created or changed.
- No paid call, push, PR mutation, merge, deploy or Recall.
- Baseline: GO-gate audit `9208e5d`.
- GitHub was read through the Founder's admin CLI session (read-only calls only).
- Code was read at the current lane heads: Path A #276 `6c75174`; FC-4 #275 `eed3f4a`; #263 `e33f494`; Track B #267 `da4d96b`; Codex specs `8a5bdaf` (activation security), `9d9dc9e` (composition), `bf9616e` (Git transport).

Package contents (this directory):
| File | Purpose |
|---|---|
| `ACTIVATION_READINESS_PACKAGE.md` | this report (sections 1–12) |
| `FOUNDER_RUNBOOK.md` | zero → STOP-before-merge, each step with evidence / GO / NO-GO / recovery |
| `readiness_manifest.json` | machine-readable blocker ledger and readiness matrix |
| `ci_check_manifest.json` | canonical check manifest `{name, app identity, workflow, job, required, trigger context}` |
| `ruleset_phase1.json`, `ruleset_phase2.json` | exact ruleset API payloads (two-phase, see §5) |
| `github_app_package.json` | App manifest, token-minting scopes, forbidden permissions, NO-GO conditions |
| `attestation_schemas/*.schema.json`, `canonical_digest.py` | attestation JSON Schemas + canonicalization/digest tool (no signing) |

---

## 1. CI trigger readiness
Workflows (`.github/workflows/`, identical trigger blocks at `6c75174` and `eed3f4a`):

| Workflow | Triggers | Filters | Relevance |
|---|---|---|---|
| `CI` (`ci.yml`) | `push`, `pull_request` | `branches: [main, "claude/**", "cursor/**", "codex/**"]` for both; no `paths`/`paths-ignore`; no `pull_request_target`; `concurrency: ci-${{ github.workflow }}-${{ github.ref }}`, cancel-in-progress; **no top-level `permissions:`** | all required checks |
| `Build & push VPS images` | push to `main`, `workflow_dispatch` | — | never runs for activation refs (correct) |
| Dependency Graph (dynamic) | default branch | — | irrelevant |

Trigger matrix for the activation lane (current `ci.yml`):

| Context | Ref | Event | Runs today? | Needed? |
|---|---|---|---|---|
| **Task-branch CI** | push to `claude/mainai-task-<uuid>` (branch name fixed in `execution_job.py`) | `push` | **yes** (`claude/**`) | yes. The required checks bind to the PR head SHA, so these runs satisfy the ruleset. |
| **PR CI** | PR `claude/mainai-task-*` → `mainai/integration-founder-supervised-v1` | `pull_request` (filter matches the *base* branch) | **NO** (`mainai/**` absent) | recommended (tests the merge result) |
| **Activation-branch CI (creation)** | Founder creates `mainai/integration-…` at the activation SHA | `push` | **NO** | yes. Gives exact-SHA CI evidence *on the activation ref* (the commit is usually already CI'd on its source branch; record both). |
| **Post-merge CI** | Founder merges the PR → push to `mainai/integration-…` | `push` | **NO** | yes. Without it the integration tip after the first merge is never CI-verified. |
| Founder/agent lanes | `claude/**`, `cursor/**`, `codex/**` | both | yes | unchanged |

**Mismatches found:**
- **T-1:** `push.branches` lacks `mainai/**` (activation-branch and post-merge CI).
- **T-2:** `pull_request.branches` lacks `mainai/**` (PR CI).
- **T-3:** no workflow-level `permissions:`. Job token permissions follow the repo default (currently `read`, verified). If that default is ever flipped to write, AI-authored test code run by CI gets a write token.
- **T-4 (P2):** every `actions/checkout@v4` uses the default `persist-credentials: true`, so the job token sits in `.git/config` while AI-authored tests run (read-only today).
- **T-5 (P2):** third-party actions are pinned by tag, not SHA (`sha_pinning_required=false`).
- **T-6 (info):** five jobs plus `deploy-render` are gated on hard-coded legacy refs (`claude/det-kommer-mer-879lcm`, `claude/strato-vps-prep`, …), so they are always `skipped` on activation refs. That is harmless, but they must not be required (§2).
- **T-7 (info):** the task-branch name `claude/mainai-task-*` lives inside `claude/**`. If the prefix ever changes, task-branch CI silently stops. Bind the prefix in a test, or add `mainai/**` and move task branches under it later.
- **T-8 (info):** pushes made with a **GitHub App installation token do trigger workflows**; pushes made with an Actions `GITHUB_TOKEN` would not. The runtime must never push with an Actions token.

**Exact minimal workflow change (to be made later by an assigned builder, not now; it touches `.github`, so it must land in the composition before activation):**
```yaml
on:
  push:
    branches: [main, "claude/**", "cursor/**", "codex/**", "mainai/**"]      # T-1
  pull_request:
    branches: [main, "claude/**", "cursor/**", "codex/**", "mainai/**"]      # T-2
permissions:                                                                    # T-3 (top level)
  contents: read
```
Optional P2: `with: persist-credentials: false` on each `actions/checkout@v4` (T-4), and pin actions by commit SHA (T-5). There must be no change to job names: names are the required-check identities.

## 2. Required-check stability
Canonical manifest: `ci_check_manifest.json` (from real check runs at `eed3f4a`, two runs; app ids from the Checks API).

**Required (12), all `app_slug=github-actions`, `integration_id=15368`, workflow `CI`, run on every branch:**
1. `Frontend — TypeScript & ESLint` (`lint-and-typecheck`)
2. `Frontend — npm audit` (`npm-audit`)
3. `Frontend — build (docker)` (matrix)
4. `Frontend — build (vercel)` (matrix)
5. `Backend — unit/integration tests`
6. `Backend — RLS & session-security tests`
7. `Backend — account lifecycle & rate-limit tests`
8. `Backend — Alembic migration check`
9. `E2E — Playwright (full stack)`
10. `E2E — Library / Founder Knowledge Studio`
11. `E2E — same-origin proxy`
12. `All required checks passed`

Items 1–11 are individual checks; item 12 is the aggregate rollup.

- **Aggregate semantics:** `all-checks-passed` (`if: always()`) fails only on `failure`/`cancelled` and treats `skipped` as acceptable. On its own it would pass if a core job were skipped, so the 11 individual checks are required alongside it.
- **Not suitable as hard requirements:** the five branch-gated jobs (always `skipped`, and GitHub counts skipped as satisfying a required check, so requiring them proves nothing); `Deploy to Render` (disabled); `Vercel Preview Comments` (check run, app `vercel`/8329) and `Vercel` (commit status). These are third-party deploy signals and must never gate.
- **Substitution risk:** all names are distinct strings, but two families are similar (`Frontend — build (docker|vercel)`; three `Backend — …` names). The real risk is *app substitution*: another app or a commit status posting the same context. Mitigate by binding `integration_id: 15368` in the ruleset and `app_slug` in MainAI's CI wait.
- **Byte-exact names:** names contain U+2014 (—) and `&`, so copy them byte-exact.
- **MainAI CI-wait gaps (code):**
  - `ci_wait.evaluate_check_runs` (`ci_wait.py:96-118`, unchanged through `6c75174`) waits on *all* check runs on the SHA, including Vercel's.
  - It does not bind names or app.
  - It treats `skipped`/`neutral` as pass (`_PASSING_CONCLUSIONS`, `ci_wait.py:50`).
  - Codex spec §Required CI set (B-05) fixes all three.
- **Pagination dependency:** `GET …/commits/{sha}/check-runs` returns **30 of 37** runs on its default page (live at `eed3f4a`). The Path A lineage `list_check_runs` (`6c75174`, `github_client.py:96-99`) reads only page 1. **#263 replaces it with a paginated `per_page=100` version**, and in the composition (#263 merged) the paginated version wins (verified in local composition `165122d`). So **#263 is a hard composition dependency** (B-08): without it the CI wait can declare PASS while required runs are unseen.

## 3. Branch/base configuration — every location that must point at the integration branch
| Location | Current | Required for activation | Kind |
|---|---|---|---|
| env `GITHUB_WORKTREE_BASE_BRANCH` (`config.py:203`) | default `claude/det-kommer-mer-879lcm` | `mainai/integration-founder-supervised-v1` | config |
| env `GITHUB_PR_BASE_BRANCH` (`config.py:204`) | `""`, which falls back to the worktree base | same, set explicitly | config |
| `worktree.BASE_BRANCH` constant + `execution_worktree_base_branch()` fallback (`worktree.py:55, 61-70`) | **silent fallback to the stale branch when the env is empty** | activation mode must **fail closed** when either base setting is empty or ≠ envelope `base_branch` (Codex spec §14 startup validation) | code (B-06) |
| `models/mainai_recovery.PROTECTED_BRANCHES` (`:35`) | `{main, master, claude/det-kommer-mer-879lcm}` | add the integration branch (code guard `protected_execution_branches()` already unions the configured bases, so this is P2 defence-in-depth) | code P2 |
| migration `0033` CHECK `branch NOT IN ('main','master','claude/det-kommer-mer-879lcm')` | DB-level task-branch guard lacks the integration branch | P2: add via the activation migration (Codex spec already needs one migration) | code P2 |
| `.env.example:103-104` | stale comment | document the integration branch | docs P2 |
| `tests/backend/test_config_contract.py:150` | asserts the stale default | update with B-06 | test |
| GitHub default branch | `claude/det-kommer-mer-879lcm` | optional. Path A always passes an explicit base; changing the default is UI convenience only (Founder choice; DEFERRED) | GitHub setting |
| `render.yaml:41`, `ci.yml` branch-gated jobs / `deploy-render` | stale legacy refs | none (inert); DEFERRED cleanup | — |
| `production_adapter.py:23`, `substrate.py:66` `PROTECTED_REFS {main, master, …}` | Supervisor side | not used by Path A (Supervisor ≠ Path A goals); DEFERRED | — |

## 4. GitHub App configuration package (`github_app_package.json`)
| Layer | Requirement |
|---|---|
| **Installation permissions** | exactly `metadata:read, contents:write, pull_requests:write, checks:read, statuses:read`; **no events**, webhook inactive |
| **Repository scope** | owner user `d1n095`; installation "Only select repositories" = `[d1n095/LifeAI]` |
| **Forbidden** | `administration, workflows, actions, secrets, variables, environments, deployments, pages, packages, security_events, repository_hooks, repository_projects, issues, discussions, members, organization_*, single_file, vulnerability_alerts, dependabot_secrets, codespaces, attestations` (schema `installation.schema.json` uses `additionalProperties:false`, so any extra permission fails validation) |
| **Token minting** | by a Founder-controlled broker holding the App private key **outside** the MainAI worker. The read phase mints a down-scoped read-only token. The effect phase mints `contents:write, pull_requests:write` **only inside the one-shot effect executor** (Codex `bf9616e`). TTL ≤ 1 h; never persisted or placed in a worktree. |
| **Runtime token facts (provable by the runtime, read-only)** | `GET /installation/repositories` → exactly LifeAI; `GET /user` → **403** (not a user token); `GET /repos/d1n095/LifeAI/actions/permissions` → **403** (no administration); `GET /repos/d1n095/LifeAI/rules/branches/mainai%2Fintegration-founder-supervised-v1` → the expected effective rules. **Never probe with a write.** |
| **Founder/admin attestations** | App registration page (permissions, events), installation repo selection, `GET /app/installations/{id}` via App JWT, private-key custody statement (where the key lives; that MainAI cannot read it) |
| **Bot ≠ Founder** | the bot login ends in `[bot]`, `type=Bot`, `bot_user_id ≠ founder_user_id (d1n095)`. After the real cycle: the pushed ref and PR `user.login == <slug>[bot]`. Commit author is fixed `MainAI Executor <mainai-executor@lifeai.local>` (`worktree.py:56`), so **identity is proven by the token and the PR author, not the commit author string**. |
| **NO-GO** | any extra permission; selection ≠ selected/[LifeAI]; any event; `GET /user` 200; key or token reachable from the worker/agent env; any ruleset bypass actor; a fine-grained or classic PAT as runtime credential (on a user-owned repo it acts as the Founder) |

## 5. Ruleset package (`ruleset_phase1.json`, `ruleset_phase2.json`)
- **Target** `refs/heads/mainai/integration-founder-supervised-v1` (include exactly one ref, exclude none); `enforcement: active`; **`bypass_actors: []`**.
- **Rules:**
  - `deletion`; `non_fast_forward`;
  - `pull_request`:
    - `required_approving_review_count: 1`;
    - `dismiss_stale_reviews_on_push: true`;
    - `require_last_push_approval: true`;
    - `required_review_thread_resolution: true`;
    - `require_code_owner_review: false`;
    - `allowed_merge_methods: ["merge"]`;
  - `required_status_checks`: `strict: true`, `do_not_enforce_on_create: false`, the 12 contexts each with `integration_id: 15368`;
  - **Phase 2 only:** `creation`.
- **Why two phases:** with `bypass_actors: []` a `creation` rule would block the Founder from creating the branch. Phase 1 (no `creation`) is applied first, then the Founder creates the branch at `approved_base_sha`, then phase 2 adds `creation`. The snapshot and digest are taken after phase 2. There is no unprotected window, because the ruleset already targets the ref name before it exists.

**GitHub limitations that affect exact enforcement:**
1. **The repository admin can always edit or disable the ruleset.** Future immutability cannot be proven. Mitigation: receipt TTL ≤ 15 min, a re-snapshot plus digest comparison at each Founder checkpoint and immediately before any merge, and the audit log.
2. **Single human approver.** `require_last_push_approval` means that if the Founder ever pushes to the bot's PR branch, no one can approve. Recovery: never push to the task branch; close the PR and run a new cycle.
3. **`strict` up-to-date.** If the integration branch moves after the task branch forks, the PR is out of date. The bot has (and must have) no "update branch" authority. Recovery: a new cycle from the new approved SHA.
4. **Status-check picker.** The UI only offers contexts seen in the last 7 days. The API accepts the exact payload; use the API, or confirm all 12 ran within 7 days.
5. **`allowed_merge_methods` is a newer parameter.** If the API rejects it, omit it and set repo merge methods instead (`allow_squash_merge`/`allow_rebase_merge` false). Record which was used.
6. **Draft PRs** cannot be merged until marked ready. That is a Founder action, consistent with STOP-before-merge.
7. **Required signatures:** do not enable. Bot pushes via git are unsigned and would block the cycle.
8. **Ruleset "evaluate" mode is Enterprise-only;** use `active`.

## 6. Attestation tooling design (`attestation_schemas/`)
Payload schemas (JSON Schema 2020-12, `additionalProperties:false`):
- `repository_identity`
- `activation_sha` (approved_base_sha == branch tip == running code SHA, plus composition lanes with examiner evidence SHAs and `verdict: PASS`)
- `required_checks` (exactly 12, sorted, `github-actions`/15368)
- `ruleset_snapshot` (admin view reduced to stable fields + effective rules + classic protection `absent`)
- `no_bypass` (empty bypass list + runtime 403 probes)
- `installation` (exact permission object; bot ≠ Founder ids; TTL ≤ 3600)
- `bundle` (member digests + `runtime_config_sha256`; the single digest the preflight receipt binds)
- `attestation_envelope` (signer = Founder, `ssh-sig`, namespace `mainai-activation-v1`, `expires_at`)

**Canonicalization** (`canonical_digest.py`):
- UTF-8, sorted keys at every level, `(",", ":")`, `ensure_ascii=False`;
- floats rejected (integers only), which makes the output identical to RFC 8785 JCS on this value space;
- producers sort unordered arrays.

`payload_sha256 = sha256(canonical bytes)`.

**Signing (Founder, outside MainAI; not performed here):** `ssh-keygen -Y sign -f <founder_key> -n mainai-activation-v1 payload.canonical.json`. MainAI verifies against a pinned `allowed_signers` file, which is itself part of the reviewed activation SHA.

An example placeholder payload is included (`example_required_checks.PLACEHOLDER.json`). It is clearly fake and has no signature.

## 7. Preflight rehearsal (deterministic, unpaid, no remote mutation)
| # | Rehearsal | Proves | Tool / evidence |
|---|---|---|---|
| R-0 | compose the activation candidate locally (order §10); clean tree; record SHA | composition merges; additive conflicts resolved | `git merge` log; tree SHA |
| R-1 | full backend suites + examiner probe suites at the composed SHA (`probes/path_a_v1/*`, `probes/e2e_v1/test_path_a_269.py`, `test_path_a_271.py`, `test_task_authority_273.py` (+ #276 successor), E2E-A′), with a local bare remote, fake GitHub REST and fake provider | all code invariants (SPEND, CTX, TEST, SCOPE, CAP, FC, ROUTE, TA) | MATRIX output, 0 GAP on required rows |
| R-2 | real `create_task_worktree` with a dummy token value → `git config --get-regexp 'remote\|url\|credential'` and env scan | credential-free worktree and verification env (B-04) | config dump with no token |
| R-3 | push-grant state machine against the local bare remote: exact-binding mismatch, expiry, cancellation, concurrent consumption (one winner), retry needs a new grant | Codex spec push-grant P1s | test output |
| R-4 | CI-wait replay with recorded real check-run JSON from the composed SHA's GitHub runs (read-only GET, paginated): exact 12 + app binding; inject a Vercel failure (ignored), a same-name status from another app (rejected), a missing required check (waits), `skipped` on a required check (fails) | B-05, B-08 | test output |
| R-5 | **runtime-token read probes** (after the App exists): `/installation/repositories`, `/user`→403, `/actions/permissions`→403, `/rules/branches/<integration>` | §4 runtime facts | HTTP transcript (token redacted) |
| R-6 | **preflight dry-run** against the real repo (read-only) and a test DB: every fact in Codex spec §Deterministic preflight, with a spend grant whose provider calls are refused (no paid call) | GO/NO-GO matrix minus paid facts | receipt (rehearsal-flagged) + digest |
| R-7 | **negative preflight:** tamper each bound field in turn (base SHA, branch, repo, check set, ruleset digest, token digest, config digest, expiry) → NO-GO each | fail-closed binding | 8× NO-GO |
| R-8 | ruleset digest re-snapshot equals the attested digest | §5 limitation 1 | digest equality |

**Facts only provable in the real cycle:**
- the actual GitHub push accepted, and remote SHA == approved local SHA;
- the PR created as draft by `<slug>[bot]` against the integration branch;
- GitHub CI executing on the new SHA, with all 12 contexts from 15368 success;
- real provider usage and billing matching the ledger (invoice);
- ruleset enforcement observed on a real PR (the merge button blocked until approval after the last push).

## 8. First-cycle exact scope
Re-evaluation of the two-file probe (`backend/app/first_cycle_probe.py` + test):
- **Safe.** A new pure module imported by nothing.
- The repo-wide scanning tests (`test_mainai_execution_executor.py:349`, `storage/test_source_purge.py:1763/1813`, `mainai/*purity*` `iter_modules`) only flag specific patterns or packages, so a pure module in `app/` root trips none of them.
- It still adds production code.

**Safer recommendation (Option A, test-only, one file):**
- **target_files / envelope paths:** exactly `["backend/tests/backend/test_first_cycle_probe.py"]` (new file).
- **Verification:** `targeted_tests: tests/backend/test_first_cycle_probe.py` (maps to the same file; new file, so editable under TA-8b).
- **Instruction:** "Add a focused test file asserting existing, price-independent invariants of `app.providers.pricing.estimate_cost`:
  - an unpriced provider/model returns `None`;
  - `ollama` returns `Decimal('0')`;
  - the result is non-negative and monotonic in token counts.

  Do not modify any other file."
  - `estimate_cost` is pure, with no I/O, DB or network.
  - The invariants do not depend on list prices.
- **Capabilities:** `["path_a","read_file","repo_edit","run_tests","push","open_pr"]`. `push` and `open_pr` are effect capabilities, each additionally gated by its own Founder grant or approval.
- **Forbidden (envelope excludes, and code also enforces):**
  - any path other than the one file;
  - `backend/app/**`, migrations, `frontend/**`, `.github/**` (dot-parts are rejected by `validate_target_path`, and the App lacks `workflows`), `docs/**`;
  - deploy/VPS/Render scripts; auth, RLS, Recall, credentials;
  - Track B (`agent_assignment`; all `LIFE_AGENT_ADAPTER_ENABLED__*` unset);
  - merge.
- **Success condition (all required):**
  - the plan has exactly one `repo_edit` whose created-event `target_files == [that file]`, plus `open_pr`;
  - the local commit diff (`git diff --name-only --no-renames <approved_base_sha>..HEAD`) == exactly that file;
  - the targeted test passes locally;
  - the push grant is consumed once and remote SHA == local SHA;
  - a draft PR by `<slug>[bot]`, base = integration branch, head SHA = approved SHA;
  - the 12 required contexts succeed on that SHA (app 15368);
  - spend ledger ≤ grant;
  - independent review PASS bound to repo + PR + head SHA;
  - **no merge**.

**Option B** (the two-file module + test) is acceptable if the Founder wants proof of a production-file edit. Its envelope is the two exact files, and the same success condition applies with the two-file diff.

## 9. Founder runbook
See `FOUNDER_RUNBOOK.md`: 17 steps, each with expected evidence, GO, NO-GO and recovery.

## 10. Blocker ledger (consolidated; machine-readable in `readiness_manifest.json`)
| ID | Blocker | Class | Owner | Status | Evidence required |
|---|---|---|---|---|---|
| B-01 | Path A task authority TA-8b (P1) / TA-10 (P2) | code | Cursor | fix published as #276 `6c75174`; **UNKNOWN** until independently examined | examiner PASS at the #276 freeze SHA |
| B-02 | FC-4 exception policy A6/A7 (P1) | code | Codex | **FAIL** (#275 RETURN_TO_BUILDER, evidence `6d56ff0`) | PASS at the #275 successor SHA |
| B-03 | Activation-security substrate: envelope activation fields + migration, push grants, preflight receipts, remote-effect predicate, PR identity, startup validation | code | Codex | **UNKNOWN** (spec `8a5bdaf` only) | PASS at the implementation SHA |
| B-04 | Credential-free Git transport: token in `.git/config` (`worktree.py:196`); model-written tests and agents can read it (Track B TB-3c/TB-7, Codex `bf9616e`) | code (P0) | Codex | **FAIL** | PASS + R-2 |
| B-05 | CI wait exact required set: 12 names, app binding, ignore Vercel, no skipped-as-pass for required | code | Codex (part of B-03) | **FAIL** (current `ci_wait.py:50,96-118`) | PASS + R-4 |
| B-06 | Base branch fails closed (no stale fallback; both settings explicit and == envelope) | code | Codex (spec §14) | **FAIL** (`worktree.py:55,61-70`) | PASS + test |
| B-07 | `ci.yml` T-1/T-2/T-3 (`mainai/**`, top-level `permissions`) | code (.github) | **UNASSIGNED** | **FAIL** | change merged into the composition; exact-SHA CI evidence on the integration ref |
| B-08 | #263 paginated check runs must be in the composition | composition | composer (Codex composition lane) | **UNKNOWN** | composition manifest lists `e33f494` (or successor) |
| B-09 | Exclude Track B #267 from the activation composition (Track B OFF); otherwise its P1s apply | composition decision | Founder | **UNKNOWN** | composition manifest without #267, or adapters provably disabled |
| B-10 | Composed activation SHA: full regression + examiner probes (R-1) + exact-SHA CI green | evidence | examiner + CI | **UNKNOWN** | activation SHA |
| G-01 | Ruleset phase 1/2 applied, digest attested | config | Founder | **FAIL** (none) | `ruleset_snapshot` + `no_bypass` attestations |
| G-02 | Integration branch created at `approved_base_sha` | config | Founder | **UNKNOWN** (correctly absent) | `activation_sha` attestation |
| G-03 | GitHub App created/installed per §4; broker outside the worker | config | Founder | **FAIL** (none) | `installation` attestation + R-5 |
| G-04 | Vercel preview builds of `claude/mainai-task-*` disabled or non-secret | config (P2) | Founder | **UNKNOWN** | Vercel ignored-build-step or env review |
| G-05 | Actions default token read; no PR approval by Actions | config | — | **PASS** (observed) | re-observe at preflight |
| G-06 | No deploy keys / extra collaborators / webhooks; auto-merge off | config | — | **PASS** (observed) | re-observe at preflight |
| F-01 | Runtime config: bases, `GITHUB_WRITE_ENABLED` session-only, auto-merge false, legacy route false, spend gate true, only `openai` configured, `CHAT_FALLBACK_ORDER=openai`, real `SECRET_KEY`, all Track B adapters disabled | config | Founder | **UNKNOWN** | `runtime_config_sha256` |
| F-02 | Pricing attestation `openai/gpt-4o-mini` | evidence | Founder | **UNKNOWN** | signed note |
| F-03 | Attestation bundle signed; preflight GO receipt ≤ 15 min | evidence | Founder | **UNKNOWN** | bundle digest + receipt |
| D-01..D-07 | Track B: TB-1 envelope effect, TB-2 builder scope, TB-3b reviewer OS read-only, TB-4 merged truth, TB-5 negative verdict, E2E-D receipt, TB-6b env allowlist | **can defer (Track B OFF)** | — | DEFERRED | required before any Track B use |
| P2-01 | `PROTECTED_BRANCHES` / migration 0033 CHECK lack the integration branch | code P2 | Codex (activation migration) | DEFERRABLE | — |
| P2-02 | `ci.yml` T-4 persist-credentials, T-5 SHA pinning | code P2 | unassigned | DEFERRABLE | — |
| P2-03 | GitHub default branch stale; `render.yaml`/legacy gated jobs | config/doc | Founder | DEFERRED | — |

**Composition order** (aligned with Codex `9d9dc9e`, Track B removed):
1. `2fbe20a`
2. Path A final (#264→…→#276 lineage, frozen PASS)
3. #263 (union-resolve `test_github_client.py` + registry)
4. FC-4 successor of #275
5. activation-security implementation (B-03/B-05/B-06), built on that exact composed parent
6. Git transport (B-04)
7. the `ci.yml` change (B-07)
8. final exact-SHA battery + examiner pass, then that SHA becomes `approved_base_sha`

## 11. Readiness matrix
`readiness_manifest.json` lists every item above with `status ∈ {PASS, FAIL, UNKNOWN, DEFERRED}`, `owner`, `evidence_sha_required`, `class` and `depends_on`.
- **Rule:** any FAIL or UNKNOWN among non-deferred items ⇒ NO-GO.
- **Current:** PASS 2, FAIL 8, UNKNOWN 9, DEFERRED/deferrable 10 ⇒ **NO-GO**.

## 12. Final no-drip sweep (same domain), all included above
New in this package beyond the GO-gate audit:
- T-2 (PR CI), T-3 (`permissions`), T-4, T-5, T-7, T-8;
- B-05 detail (skipped/neutral = pass; Vercel included);
- **B-08 pagination (30/37 live)**;
- B-06 silent stale fallback;
- P2-01 DB CHECK / `PROTECTED_BRANCHES`;
- ruleset two-phase creation and limitations 1–8;
- bot identity via token/PR author, not the commit author string;
- token-broker custody;
- Option A test-only scope.

Nothing else was found in this domain:
- no other workflow, `paths` filter, `pull_request_target`, CODEOWNERS, Dependabot config or Vercel repo config file;
- no deploy keys, webhooks or extra collaborators;
- Actions token read-only.
