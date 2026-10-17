# Activation GO-gate evidence audit — first Founder-supervised MainAI cycle

Date 2026-10-17. Examiner: Claude, read-only.
- No GitHub setting, branch, token, ruleset or candidate was created or changed.
- No paid API call.
- GitHub state was read through the Founder's existing CLI session (`d1n095`, repository admin). That session is itself the **Founder bypass identity** and must never be the runtime identity.

## 0. Observed GitHub state (2026-10-17, read-only API)
| Fact | Observed | Consequence |
|---|---|---|
| Repository | `d1n095/LifeAI`, **public**, owner type **User** (not an organization) | Repository rulesets are available. Org-level rulesets, custom repository roles and org bypass lists do not exist, so "no broad org bypass" is satisfied by construction. Any admin-role bypass comes only from the repo ruleset's own `bypass_actors`. |
| Collaborators | only `d1n095` (admin); deploy keys 0 | no other human or key with push |
| Webhooks | none returned | — |
| Rulesets | `GET /repos/d1n095/LifeAI/rulesets` → `[]` | **no ruleset exists** |
| Branch protection | no `main` branch; nothing protected | **no protection anywhere** |
| Default branch | `claude/det-kommer-mer-879lcm` (`64c07d5`, 2026-09-01, does not contain `2fbe20a`) | stale (FC-6). Code default `GITHUB_WORKTREE_BASE_BRANCH` is the same stale branch (`config.py:203`) |
| Integration branch `mainai/integration-founder-supervised-v1` | **does not exist** (404) | correct: must not be created before authorization |
| Other `mainai/*` | `mainai/release-founder-alpha` = `8cc4875` (Personal Recall ingestion, 2026-09-19) | unrelated, must not be used as base |
| Composition base | `codex/founder-alpha-final-composed-candidate` = `2fbe20a` | current base of the candidate lanes |
| Actions | enabled, `allowed_actions=all`, `sha_pinning_required=false`; default workflow token **read**, cannot approve PRs | good. Consider pinning actions (P2) |
| Workflows | `CI` (`.github/workflows/ci.yml`), `Build & push VPS images` (push to `main`/dispatch only), Dependabot graph | — |
| `ci.yml` triggers | `push` and `pull_request` only for branches `main, claude/**, cursor/**, codex/**` | **`mainai/**` is not covered.** PR CI does not run on PRs targeting the integration branch, and **no CI runs on the integration branch after a merge**. The MainAI task branch `claude/mainai-task-<id>` is covered by `claude/**` push CI, and required checks bind to the head SHA, so the PR's head is still checked. See C-7. |
| Check-run producers at a recent SHA (`eed3f4a`) | GitHub Actions app **`github-actions`, app id 15368**; Vercel app **`vercel`, id 8329** (check "Vercel Preview Comments"); commit status context **`Vercel`** (deployment status) | the required-check binding uses the integration id |

## 1. Integration branch
- **Name:** `mainai/integration-founder-supervised-v1`.
- **Source SHA semantics:** created by the Founder **at exactly the activation SHA**, i.e. the final composed commit after all candidate lanes are frozen, independently PASSed and composed:
  - #263 GitHub read-only discovery;
  - final Path A (task-authority lineage, currently #276 pending exam);
  - FC-4 #275 successor;
  - activation-security implementation;
  - credential-free worktree fix.

  `approved_base_sha` in the envelope and preflight receipt == that branch tip == the `activation_sha` of the running MainAI code (one SHA for the first cycle).
- **Before cycle start the branch must contain no other commit.** Any movement means the receipt is stale and the result is NO-GO (Codex spec boundaries 1–5).
- MainAI config must point at the branch: `GITHUB_WORKTREE_BASE_BRANCH=GITHUB_PR_BASE_BRANCH=mainai/integration-founder-supervised-v1`. It must never use the default branch.
- **Not created.** Creation requires explicit Founder authorization and happens only after the ruleset (§2) exists, so the branch is never unprotected.

## 2. Required ruleset (repository ruleset, target = exactly `refs/heads/mainai/integration-founder-supervised-v1`, enforcement **active**)
| Rule | Required value | Note |
|---|---|---|
| `bypass_actors` | **`[]` (empty)** | No Repository-admin role, no app, no deploy key. The Founder merges through the normal PR flow (approval + checks). Emergency changes are made by editing the ruleset, which is audited and changes the snapshot digest, so preflight goes NO-GO. |
| `pull_request` | `required_approving_review_count: 1`, `dismiss_stale_reviews_on_push: true`, `require_last_push_approval: true`, `required_review_thread_resolution: true`, `require_code_owner_review: false` (no CODEOWNERS) | The bot authors the PR; the Founder's approval must come after the last push. `allowed_merge_methods` should be one method (recommended `merge` or `squash`, Founder's choice), recorded in the snapshot. |
| `required_status_checks` | `strict_required_status_checks_policy: true`; contexts per §3, each with `integration_id: 15368` (GitHub Actions) | Binding the app id prevents a same-name status from another app or a token satisfying the check. |
| `non_fast_forward` | present | force-push disabled |
| `deletion` | present | deletion disabled |
| `creation` | present | after the Founder has created the branch: nobody re-creates it |
| `update` | **absent** | updates arrive only through PR merge; with `bypass_actors=[]` direct pushes are already refused by `pull_request` |
| `required_linear_history` / `required_signatures` | optional (Founder choice); record in the snapshot | |

**Founder final merge authority** is retained because the only path to update the branch is a PR the Founder approves (after the last push) and merges. MainAI's runtime identity has no merge capability in code (Path A has no merge implementation; `github_auto_merge_enabled=false`), and the bot cannot approve its own PR.

**Task branches** (`claude/mainai-task-*`) are deliberately not covered by the ruleset; the bot must be able to push them. Optional hardening: a second ruleset on `claude/mainai-task-*` with `non_fast_forward` + `deletion` (non-force push only).

## 3. Required check names (stable job `name:` values, verified at `eed3f4a`, two runs, app `github-actions`/15368)
**Individual required checks (always run on every branch, no `if:`):**
1. `Frontend — TypeScript & ESLint`
2. `Frontend — npm audit` (the FC-4 lane; it runs `npm run audit:policy:test` + `check-npm-audit.js` after #275's successor)
3. `Frontend — build (docker)`
4. `Frontend — build (vercel)`
5. `Backend — unit/integration tests`
6. `Backend — RLS & session-security tests`
7. `Backend — account lifecycle & rate-limit tests`
8. `Backend — Alembic migration check`
9. `E2E — Playwright (full stack)`
10. `E2E — Library / Founder Knowledge Studio`
11. `E2E — same-origin proxy`

**Aggregate rollup (also required):** `All required checks passed`.
- It `needs:` the 11 above plus five branch-gated jobs, and fails on any `failure`/`cancelled`.
- Its logic treats `skipped` as acceptable, which is why the individual checks are required as well: requiring them closes the "core job skipped" hole.

**Must NOT be required:**
- Branch-gated jobs: `Combined container — build, run, verify` and the four `VPS …` / `Strato VPS compose topology …` jobs. They only run on hard-coded branches and are always `skipped` here.
- `Deploy to Render (PERMANENTLY DISABLED …)`.
- Vercel's `Vercel` commit status and `Vercel Preview Comments` check. These are external deployment signals, not product verification; requiring them would hand merge-gating to a third-party deploy.
- `Dependency Graph`.

**MainAI's own CI wait must bind the same 12 `{name, app_slug=github-actions}` pairs.** Current code (`ci_wait.evaluate_check_runs`, `ci_wait.py:96-118`) waits for *every* check run on the SHA, including Vercel's, and does not bind names or app. This is Codex spec §Required CI set (a code item).

Duplicates: each check appears twice per SHA (push run + PR run). Both must pass.

## 4. Runtime GitHub identity
**Recommended:** a dedicated **GitHub App** (for example `mainai-runtime`), installed on user account `d1n095` with **repository selection = only `d1n095/LifeAI`**.
- Not a fine-grained PAT: on a user-owned repo a PAT acts *as the Founder* (repository admin), which makes the runtime actor the bypass identity.
- Not a classic PAT.

Short-lived installation tokens (1 h), minted by a control plane that holds the App private key outside MainAI's runtime.

| Permission | Required | Why |
|---|---|---|
| `metadata: read` | yes | mandatory |
| `contents: write` | yes | push the task branch (non-force) |
| `pull_requests: write` | yes | open a **draft** PR |
| `checks: read`, `statuses: read` | yes | CI wait |
| `administration` | **forbidden** | rulesets/settings/branch creation of protected names |
| `workflows` | **forbidden** | GitHub refuses App pushes that modify `.github/workflows/*` (a server-side guard on top of `validate_target_path` rejecting dot-parts) |
| `actions` (write), `secrets`, `variables`, `environments`, `deployments`, `pages`, `packages`, `security_events`, `repository_hooks`, `members`, `organization_*` | **forbidden** | no deploy, secret, CI-control or hook authority |
| `issues`, `discussions` | not needed (forbidden for v1) | |

**Evidence split**

| Claim | Runtime token can prove | Founder/admin attestation must prove | Control-plane observation |
|---|---|---|---|
| Actor is not the Founder | the installation token is not a user token: `GET /user` → 403 "Resource not accessible by integration"; commits/PRs authored as `<app>[bot]` | App slug/id and installation id are MainAI's | after the first push: the PR author is `mainai-runtime[bot]` |
| Exact repository installation | `GET /installation/repositories` → exactly `[d1n095/LifeAI]`, `repository_selection` | — | — |
| Allowed / forbidden permissions | read-only negative probes only: `GET /repos/d1n095/LifeAI/actions/permissions` → 403 (no administration); `GET /repos/d1n095/LifeAI/rulesets/{id}` shows no `bypass_actors` field to non-admins. **Never attempt a write as a probe.** | the installation `permissions` object as returned to the App JWT (`GET /app/installations/{id}`) or at token minting (`POST /app/installations/{id}/access_tokens` response `permissions`) — canonical JSON → SHA-256 | — |
| Token is short-lived and not persisted | `expires_at` of the minted token ≤ 1 h; credential-free worktree (C-5) | — | — |

## 5. No-bypass proof (Founder/admin attestation, captured with the Founder's admin session, never MainAI's)
1. `GET /repos/d1n095/LifeAI/rulesets/{id}` (admin view includes `bypass_actors`) → `bypass_actors == []`, `enforcement == "active"`, conditions `ref_name.include == ["refs/heads/mainai/integration-founder-supervised-v1"]`, `exclude == []`.
2. `GET /repos/d1n095/LifeAI/rulesets?includes_parents=true` → no other ruleset targets the branch (user repo, so there are no parent/org rulesets).
3. `GET /repos/d1n095/LifeAI/rules/branches/mainai/integration-founder-supervised-v1` (effective rules, readable by the runtime token too) → exactly the rules in §2.
4. `GET /repos/d1n095/LifeAI/branches/mainai%2Fintegration-founder-supervised-v1/protection` → 404 (no classic protection mixing in), or an attested equivalent.
5. The App installation permissions snapshot shows `administration` and `workflows` absent (§4).
6. The ruleset JSON (from 1) and the effective-rules JSON (from 3) are canonicalized (sorted keys, no volatile `updated_at`/`_links` fields) → SHA-256 = `ruleset_attestation_digest`.

## 6. Activation attestation inputs (immutable; consumed by Codex preflight `mainai_activation_preflight_receipts`)
| Input | Exact value / source |
|---|---|
| `repository_identity` | `d1n095/LifeAI` (+ repo id from `GET /repos`) |
| `integration_branch` = `base_branch` | `mainai/integration-founder-supervised-v1` |
| `approved_base_sha` = `activation_sha` | 40-hex tip of the integration branch at creation |
| `required_check_contexts` | the 12 `{name, app_slug:"github-actions"}` pairs of §3 (canonical JSON, sorted) |
| `ruleset_attestation_digest` | §5.6 |
| `runtime_token_identity` | `app_slug`, `app_id`, `installation_id`, bot login |
| `runtime_token_attestation_digest` | SHA-256 of canonical `{installation_id, repository_selection, repositories:[full_name], permissions}` |
| `runtime_config_digest` | SHA-256 of the activation-relevant settings: `GITHUB_REPO`, both base branches, `GITHUB_WRITE_ENABLED=true`, `GITHUB_AUTO_MERGE_ENABLED=false`, `MAINAI_LEGACY_PREPARE_PR_WRITES_ENABLED=false`, `MAINAI_EXECUTION_REQUIRE_PROVIDER_SPEND=true`, `MAINAI_EXECUTION_GOAL_SPEND_CAP_USD`, `DEFAULT_LLM_PROVIDER/MODEL`, all `LIFE_AGENT_ADAPTER_ENABLED__*` = false (Track B off) |
| `execution_envelope_id`, `provider_spend_authorization_id` | §7–8 rows |
| `observed_at`, `expires_at` | receipt ≤ 15 min; never beyond envelope, grant or attestation expiry |
| signer / provenance | the Founder's attestation, signed by a key or identity outside MainAI (e.g. an SSH- or GPG-signed attestation commit or file the preflight verifies against a pinned public key). MainAI cannot author it (Codex spec). |

## 7. Provider / spend (no paid call made)
- **One provider/model:** `openai` / `gpt-4o-mini`, the code default (`config.py:124-125`). It has a pinned price in `providers/pricing.py` (in 0.00015 / out 0.0006 USD per 1k), and the Founder attests it equals today's list price (freshness cannot be proven by code).
  - Path A uses only `authorized[:1]` (no fallback, SPEND-9). Still set `CHAT_FALLBACK_ORDER=openai` so that no other provider is even configured.
  - Configure **no** other provider keys in the activation runtime.
- **Grant** (`authorize_provider_spend`, bound to the envelope): `allowed_providers=["openai"]`, `allowed_models=["gpt-4o-mini"]`, `max_requests=5` (plan + optional audit + edit + one retry + slack), `max_cost_usd=0.50`, `max_cost_per_request_usd=0.10`, `max_prompt_tokens=60000`, `max_completion_tokens=12000`, `expires_at` = the session window (e.g. +4 h).
- **Goal cap:** `MAINAI_EXECUTION_GOAL_SPEND_CAP_USD` ≥ 0.50 (default 5.0 is fine; recommended 1.0).
- **Evidence:** the grant row, plus the #272 / SPEND-8a/b/c probes HOLD on the activation SHA (prompt bound by UTF-8 bytes, `max_tokens` = completion hold, cost-hold reconciliation).

## 8. First-cycle scope (safest envelope profile)
- One **fresh** goal, never Supervisor-governed, with no inserted tasks.
- Instruction: "Add `backend/app/first_cycle_probe.py` exposing a pure function `normalize_label(text: str) -> str` (strip + collapse whitespace + lowercase) and a focused test."
  - It creates **two new files**, touches no existing production file, and nothing imports the module.
  - The focused test is new, so the TA-8b lesson-guard path is irrelevant.
- **Envelope:**
  - `authorized_paths = ["backend/app/first_cycle_probe.py", "backend/tests/backend/test_first_cycle_probe.py"]` (exact files, no directory grant);
  - `authorized_capabilities = ["path_a", "read_file", "repo_edit", "run_tests", "push", "open_pr"]` (`push` per the activation-security implementation);
  - `authorized_risk = "low"`;
  - the activation fields of §6.
- **Excluded:** Track B / `agent_assignment` (all adapters disabled), migrations, `.github` (also impossible: dot-part validation + no `workflows` permission), frontend, deploy, auth, Recall, credentials, `merge`.
- Before starting: `SELECT count(*) FROM engineering_lessons WHERE status='active' AND regression_test IS NOT NULL AND applies_to::jsonb ? 'repo_edit'` is recorded.

## 9. Founder manual checkpoints (in order; each produces evidence)
1. Preflight GO receipt (`POST /goals/{id}/preflight`) at the activation SHA; record the receipt digest.
2. Create the goal; authorize the envelope (§8) and the spend grant (§7).
3. `POST /goals/{id}/plan` (one paid call); inspect the plan.
   - Exactly one `repo_edit` with `target_files` = the two exact paths (structured created event), plus `open_pr`.
   - No other task types.
   - Reject and re-plan otherwise.
4. Approve the `repo_edit` task only.
5. After the edit: inspect the local commit in the worktree.
   - `git diff --name-only --no-renames <approved_base_sha>..HEAD` = exactly the two files.
   - Read the diff; targeted test PASS checkpoint present.
   - No credential in `.git/config` (C-5).
6. Approve the **exact-SHA one-shot push grant** (`POST /tasks/{id}/push-grants` with `local_commit_sha`).
7. Verify the remote branch `claude/mainai-task-<id>` tip == the approved local SHA, pushed by `mainai-runtime[bot]`.
8. Separately approve `open_pr`. Verify a **draft** PR, base = integration branch, head SHA = the approved SHA, author = the bot.
9. Wait for exact-SHA CI: all 12 required contexts from `github-actions` pass on that SHA. Inherited failure → stop (no paid replan).
10. Independent review by the external examiner (Claude/Cursor). Track B stays disabled. Record a verification receipt bound to repo + PR + head SHA.
11. **STOP.** Do not merge in the first cycle unless the Founder separately decides to. Any merge is the Founder's own manual action through the ruleset (approval after the last push).

## 10. Final GO checklist (each item: PASS with evidence / FAIL / UNKNOWN ⇒ NO-GO). Status as of 2026-10-17.
### Code evidence (independent exam at the exact activation SHA)
| # | Item | Status |
|---|---|---|
| C-1 | Path A final (task authority incl. TA-8b/TA-10 fix, #276) independently PASS | UNKNOWN (#276 not yet examined) ⇒ NO-GO |
| C-2 | SPEND-1..10 + 8a/b/c, CTX-1/2, TEST-1, SCOPE-1, CAP-1, FC-2b, ROUTE-1..5, TA-1..11 HOLD at the activation SHA | UNKNOWN (activation SHA not composed) |
| C-3 | FC-4 successor of #275 (A6/A7 fixed) PASS; `Frontend — npm audit` green at the activation SHA | FAIL (#275 RETURN_TO_BUILDER) |
| C-4 | Activation-security implementation (envelope SHA fields, one-shot push grant, preflight + receipts, effect predicate, exact required-check CI wait) PASS | UNKNOWN (spec `8a5bdaf` only) |
| C-5 | Credential-free worktrees: no token in `.git/config`; verification runs without a credential (Track B audit TB-3c/TB-7 P0) | FAIL (open) |
| C-6 | Track B disabled: `agent_assignment` cannot launch (all `LIFE_AGENT_ADAPTER_ENABLED__*` unset/false; preferably refused in code) | UNKNOWN (config) |
| C-7 | `ci.yml` triggers include `mainai/**` for `push` and `pull_request` (PR CI and post-merge CI on the integration branch) | FAIL (not present) |
| C-8 | Composition at the activation SHA: #263 + Path A final + FC-4 + activation security + C-5 merge cleanly; full regression + exact-SHA CI green | UNKNOWN |

### GitHub / admin evidence
| # | Item | Status |
|---|---|---|
| G-1 | Ruleset of §2 exists, active, targeting exactly the integration branch, `bypass_actors=[]` | FAIL (no rulesets) |
| G-2 | Required checks = the 12 contexts of §3 with `integration_id 15368`, strict | FAIL (no ruleset) |
| G-3 | Integration branch created by the Founder at `approved_base_sha` == activation SHA, after G-1; no further commits | UNKNOWN (not created; correct for now) |
| G-4 | GitHub App `mainai-runtime` installed only on `d1n095/LifeAI` with exactly the §4 permissions (`administration`/`workflows` absent); attestation digest | FAIL (no App) |
| G-5 | Runtime proofs: `GET /user` 403 for the installation token; `GET /installation/repositories` == [LifeAI]; `GET /repos/.../actions/permissions` 403 | UNKNOWN |
| G-6 | Ruleset attestation digest (§5) signed by the Founder key | UNKNOWN |
| G-7 | Actions default token `read`, cannot approve PRs | **PASS** (`GET /actions/permissions/workflow` → read / false) |
| G-8 | No deploy keys, no extra collaborators, no webhooks | **PASS** (0 keys; only `d1n095`; no hooks) |
| G-9 | Repo `allow_auto_merge=false` | **PASS** |
| G-10 | (P2) Vercel preview builds of `claude/mainai-task-*` are ignored or non-secret | UNKNOWN |

### Founder manual evidence
| # | Item | Status |
|---|---|---|
| F-1 | Runtime config digest: base branches = integration branch, `GITHUB_WRITE_ENABLED=true` only for the session, auto-merge false, legacy route false, spend gate true, only `openai` configured, `CHAT_FALLBACK_ORDER=openai` | UNKNOWN |
| F-2 | `SECRET_KEY` is a real secret (not `change-me-in-production`) | UNKNOWN |
| F-3 | Pricing attestation for `openai/gpt-4o-mini` (list price == table) | UNKNOWN |
| F-4 | Envelope (§8) and spend grant (§7) rows created with exact values | UNKNOWN |
| F-5 | Preflight GO receipt (≤ 15 min old) bound to every digest above | UNKNOWN |
| F-6 | Lesson precondition query recorded | UNKNOWN |
| F-7 | Checkpoints 3–11 of §9 each recorded; STOP before merge | UNKNOWN (cycle not started) |

**Current overall: NO-GO.**
- 6 FAIL: C-3, C-5, C-7, G-1, G-2, G-4.
- The rest are UNKNOWN, apart from 3 PASS: G-7, G-8, G-9.

**Shortest path to GO:**
1. Code lanes land C-1, C-3, C-4, C-5, C-7, then compose (C-8).
2. The Founder creates the App and ruleset (G-1, G-2, G-4), then the branch (G-3).
3. Attestations (G-5, G-6, F-1..F-3).
4. Envelope, grant, preflight (F-4, F-5).
5. Manual cycle (F-7).

## Reproduce (read-only)
```
gh api repos/d1n095/LifeAI --jq '{default_branch,visibility,allow_auto_merge,owner:.owner.type}'
gh api repos/d1n095/LifeAI/rulesets
gh api repos/d1n095/LifeAI/branches/mainai%2Fintegration-founder-supervised-v1
gh api repos/d1n095/LifeAI/actions/permissions/workflow
gh api "repos/d1n095/LifeAI/commits/<sha>/check-runs?per_page=100" --jq '.check_runs[]|[.name,.app.slug,.app.id]|@tsv'
gh api repos/d1n095/LifeAI/keys; gh api repos/d1n095/LifeAI/collaborators; gh api repos/d1n095/LifeAI/hooks
sed -n 1,12p .github/workflows/ci.yml; awk '/all-checks-passed:/,/deploy-render:/' .github/workflows/ci.yml
```
