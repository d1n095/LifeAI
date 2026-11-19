# GitHub enforcement — exact administrator actions

These are repository settings, not code. Nothing here is claimed active until an administrator
confirms it in the GitHub UI or API; the Security workflow prints `NOT ENFORCED` for CodeQL until
then, and no branch-protection evidence exists in this repository.

## Verified repository state — 2026-11-19

Read with `gh api`, not inferred from documentation:

| Fact | Observed |
|---|---|
| default branch | `claude/det-kommer-mer-879lcm` (`gh repo view --json defaultBranchRef`) |
| `main` exists | **no** — `branches/main` returns 404, `ls-remote --heads main` is empty |
| PR #461 base | `claude/det-kommer-mer-879lcm` |
| branch protection on `main` | 404 (branch does not exist) |
| rulesets | none returned |
| secret scanning / push protection | **enabled** |
| Dependabot security updates | **disabled** |
| CodeQL | gated by `CODEQL_ENABLED` + GHAS; no code-scanning evidence observed |
| `Security`, `All required checks passed` on `b060ecf` | successful **runs**, not required checks |

Consequence: today a pull request can merge without any of the security gates. The workflow code
is fail-closed, but the repository does not require it.

## Step 0 — establish a protected default branch (blocking)

Protection cannot be configured on a branch that does not exist. An administrator must first create
`main` at the reviewed baseline (or rename the intended baseline and update the default), then
retarget the open PRs. This is a repository-administration action and was **not** performed here.

## 1. Required status checks on `main`

Settings → Branches → branch protection rule for `main` (or Rulesets → new branch ruleset):

- Require a pull request before merging: **on**
- Required approvals: **1** (the founder; CODEOWNERS review below)
- Require status checks to pass before merging: **on**
- Required checks (exact context names):
  - `Security` — the workflow-level name in `.github/workflows/security.yml`
  - `All required checks passed` — the fail-closed gate job in `.github/workflows/ci.yml`
- Require branches to be up to date before merging: **on**
- Do not allow bypassing the above settings (including administrators): **on**

API equivalent (replace `OWNER/REPO`):

```bash
gh api -X PUT repos/OWNER/REPO/branches/main/protection \
  -F required_status_checks.strict=true \
  -F 'required_status_checks.contexts[]=Security' \
  -F 'required_status_checks.contexts[]=All required checks passed' \
  -F enforce_admins=true \
  -F required_pull_request_reviews.required_approving_review_count=1 \
  -F required_pull_request_reviews.dismiss_stale_reviews=true \
  -F restrictions=
```

## 2. CODEOWNERS review

`.github/CODEOWNERS` already maps the protected paths. Enforcement requires the rule above with
"Require review from Code Owners" enabled:

```bash
gh api -X PATCH repos/OWNER/REPO/branches/main/protection/required_pull_request_reviews \
  -F require_code_owner_reviews=true
```

Verify the file resolves for a privileged path, for example:

```bash
gh api repos/OWNER/REPO/contents/.github/CODEOWNERS --jq .path
```

## 3. CodeQL (requires GitHub Advanced Security)

CodeQL is prepared in `.github/workflows/security.yml` but the job is gated by the repository
variable `CODEQL_ENABLED` and needs GHAS on a private repository.

1. Settings → Code security → enable **GitHub Advanced Security** (or confirm the plan includes it).
2. Settings → Secrets and variables → Actions → Variables → new variable:
   - Name: `CODEQL_ENABLED`
   - Value: `true`
3. Re-run the Security workflow and confirm a `CodeQL` analysis appears under Security → Code
   scanning. Until that exists, treat CodeQL as **not enforced**.

API check:

```bash
gh api repos/OWNER/REPO/code-scanning/alerts --jq 'length'   # requires GHAS
```

## 4. What the repository code already guarantees

- Every security scanner is fail-closed; no workflow uses `continue-on-error`.
- `backend/scripts/security/verify_policy_lock.py` fails on a modified control file and on a newly
  added control file that is not in the lock.
- `backend/scripts/security/verify_agent_capabilities.py` fails when the runtime's capabilities
  diverge from the reviewed manifest.
- The `All required checks passed` gate fails if any required job did not succeed.

These are verified by `backend/tests/security/test_security_controls.py` and the Security workflow;
they do not replace the administrator settings above, and the report never presents them as if they
did.

## Addendum 2026-11-19 — verified, not just described

`backend/scripts/ops/verify_release_governance.py` performs this document's checks read-only and
prints the exact commands, without running them. Current output
(`docs/evidence/governance-report.json`):

| Prerequisite | Observed |
|---|---|
| default branch | `claude/det-kommer-mer-879lcm` |
| `main` | **missing** — `GET /repos/d1n095/LifeAI/branches/main` → 404 |
| protection on `main` | **missing** — 404 (no branch to protect) |
| rulesets | none (`[]`) |
| `.github/CODEOWNERS` at the candidate commit | **present** (1,113 bytes at `d029298`) |
| `Security` context | **verified** — workflow name in `.github/workflows/` |
| `All required checks passed` context | **verified** — real job in CI run `44502219106` (success) |
| secret scanning / push protection | enabled / enabled |
| Dependabot security updates | disabled |
| GitHub Advanced Security | not enabled (CodeQL stays gated) |

`ready_to_apply: False` — the only blockers are `branch.main` and `protection.main`. Every other
prerequisite is satisfied, so creating the branch and protecting it is a two-command operation
once the founder authorises it at a moment when no other agent is mid-work on the current
default branch.

One correction to the table above: `CODEOWNERS` is queried **at the candidate ref**. Asking for
it without a ref resolves against the current default branch, which does not carry it, and the
first run reported a false "missing prerequisite".
