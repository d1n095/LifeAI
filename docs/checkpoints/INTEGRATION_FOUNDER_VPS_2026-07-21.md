# Integration Checkpoint — Founder Knowledge Studio v1 × Strato VPS Prep — 2026-07-21

Machine-readable snapshot of `claude/integrate-founder-vps`, an isolated integration branch
created from `claude/strato-vps-prep` and merged with `claude/founder-knowledge-studio-v1` to
verify the two feature lines actually work together — CI, backend startup, migrations, Docker
topology, and VPS deploy/rollback/backup all exercised against the merged code. Neither source
branch was modified. Nothing in this branch's history has touched the default branch, Render,
production, or the real Strato VPS.

See `docs/checkpoints/VPS_PREP_CHECKPOINT_2026-07-21.md` for the prior, VPS-only checkpoint this
one builds on.

```json
{
  "integration_branch": "claude/integrate-founder-vps",
  "head_commit": "52f8878",
  "head_commit_full": "52f8878185ab7e7cf4ab7e52c821aa426ba3f965",
  "working_tree": "clean",
  "source_branches": {
    "claude/strato-vps-prep": {
      "commit": "3d1fda9",
      "commit_full": "3d1fda9f2d8e4d78dad62291a8421dab3f4bd1fb",
      "status": "untouched by this integration work — verified via git rev-parse immediately before writing this checkpoint"
    },
    "claude/founder-knowledge-studio-v1": {
      "commit": "893ef74",
      "commit_full": "893ef740a1e93538a04bb09176788280b010d0b4",
      "status": "untouched by this integration work — verified via git rev-parse immediately before writing this checkpoint",
      "note": "claude/night-shift-mainai-web was verified (git merge-base --is-ancestor) to already be a full ancestor of this branch before starting, so it required no separate merge."
    }
  },
  "commits_in_this_integration_branch": [
    {
      "commit": "2e39d51",
      "type": "merge",
      "summary": "Merge claude/founder-knowledge-studio-v1 into claude/integrate-founder-vps",
      "conflicts": "Exactly one file overlapped since the branches' common ancestor: backend/app/main.py. Git's automatic 3-way merge combined both sides correctly (verified by direct inspection, not just absence of conflict markers): the VPS side's call_with_db_retry() wrapping apply_rls()/bootstrap_founder_user() at startup, and the Founder Knowledge Studio side's library/workbench router registration plus two new production-startup guards (_check_no_placeholder_secrets, _check_cookies_secure) and a credential-redaction helper. Both touch different statements within the same function and compose without semantic conflict. All ~75 other changed files were purely additive on each side (Alembic migrations 0006-0010 extend the existing linear chain with no branching; RAG/library/workbench/media-import backend+frontend code; VPS bootstrap/deploy/rollback/backup scripts, hardened Dockerfiles/compose, and CI jobs)."
    },
    {
      "commit": "52fa38a",
      "type": "ci-gate",
      "summary": "Extend 5 branch-gated CI jobs to also run on claude/integrate-founder-vps",
      "detail": "combined-container-verify, vps-scripts-check, vps-compose-verify, vps-deploy-rollback-test, and vps-backup-restore-test were each gated to run only on their own single source branch (claude/strato-vps-prep or claude/verify-combined-container). Pushing the merge commit left them skipped, so an earlier CI run (29807695776) reported all-checks-passed=success without ever having exercised the VPS/combined-container topology against the merged code. Fixed by adding an OR clause to each job's existing `if:` condition rather than replacing it, so claude/strato-vps-prep and claude/verify-combined-container are unaffected. deploy-render's gate (claude/det-kommer-mer-879lcm only) was left untouched."
    },
    {
      "commit": "f63b4d6",
      "type": "ci-fixture-fix",
      "summary": "Fix a genuine integration defect: two VPS CI fixtures used the now-rejected placeholder FOUNDER_EMAIL",
      "detail": "Once the branch-gate commit above let vps-compose-verify and vps-deploy-rollback-test actually run, CI run 29807988506 failed both — real, not spurious. Root cause: both jobs run docker-compose.vps.yml, which hardcodes ENVIRONMENT=production (the real VPS always runs with that gate active). The merged _check_no_placeholder_secrets() (added by claude/founder-knowledge-studio-v1) correctly rejects FOUNDER_EMAIL=founder@lifeos.local as the still-default config.py value once ENVIRONMENT=production — so the backend refused to start and both jobs' containers went unhealthy. Every other FOUNDER_EMAIL=founder@lifeos.local occurrence in ci.yml (backend-tests, rls-security-tests, account-rate-limit-tests, migration-check, e2e-tests, combined-container-verify) runs with ENVIRONMENT left at its development default, so the guard never fires there — those were correctly left unchanged. Fixed only the two affected fixtures to FOUNDER_EMAIL=ci-founder@lifeai-vps-ci.invalid (a syntactically valid, obviously-fake RFC 2606 .invalid address, not one of the three placeholder values the guard checks). The guard itself was not weakened, bypassed, or removed."
    },
    {
      "commit": "afe118d",
      "type": "ci-aggregation-fix",
      "summary": "Fix a genuine CI aggregation defect: all-checks-passed did not actually require the 5 VPS/combined-container jobs",
      "detail": "all-checks-passed's needs: list never included combined-container-verify or the four vps-* jobs, so even after the branch-gate commit let them run on this branch, their failure (as seen in run 29807988506: vps-compose-verify and vps-deploy-rollback-test both failed) could not affect the aggregate — it reported success anyway. Fixed by adding all five to needs:. The existing if: always() keeps all-checks-passed running even when a dependency is skipped, and the existing contains(needs.*.result, 'failure'/'cancelled') check now covers them. Jobs that are still branch-gated off correctly report result=\"skipped\" on branches that don't enable them (e.g. claude/det-kommer-mer-879lcm at the time), which the check already treats as passing, so nothing there changed. Only a genuine failure of one of these jobs on a branch where it actually executes now fails the aggregate. deploy-render's own gate (claude/det-kommer-mer-879lcm only, needs.all-checks-passed.result == 'success') was left untouched — this does not enable any deployment."
    },
    {
      "commit": "a24c318",
      "type": "docs",
      "summary": "Add this checkpoint file (initial version)",
      "detail": "Documentation-only commit recording the state as of CI run 29812450928 (commit afe118d)."
    },
    {
      "commit": "92eb9eb",
      "type": "security-ci-fix",
      "summary": "Permanently disable the obsolete Render deploy CI job",
      "detail": "deploy-render previously called Render's Deploy Hook URLs whenever RENDER_BACKEND_DEPLOY_HOOK_URL/RENDER_FRONTEND_DEPLOY_HOOK_URL secrets were set — its \"disabled\" state depended entirely on those secrets staying absent, so adding them back at any point would have silently re-enabled real Render deploys on every push to claude/det-kommer-mer-879lcm. Removed the curl/secrets-reading steps entirely (not just gated further), so no code path in this workflow can ever contact Render regardless of what secrets exist in the repo. Kept as a no-op job (not deleted) so branch-protection rules referencing it by name keep resolving, with its name and log output now explicitly stating it's permanently disabled and why. Also marked docs/RENDER_DEPLOY.md and the relevant paragraph in docs/OPERATIONS.md as superseded by the Strato VPS architecture — historical investigation content (LifeAI-1 naming, pgvector/database-role research, SMTP troubleshooting) preserved, but deployment-instruction sections clearly flagged as no longer applicable. OPERATIONS.md also notes a linked Render Blueprint's dashboard-side \"Auto Sync\" setting is outside this repo's code control and should be confirmed off manually if ever enabled."
    },
    {
      "commit": "52f8878",
      "type": "ci-policy",
      "summary": "Extend the 5 VPS/combined-container branch gates to also run on claude/det-kommer-mer-879lcm, ahead of promotion",
      "detail": "combined-container-verify, vps-scripts-check, vps-compose-verify, vps-deploy-rollback-test, and vps-backup-restore-test's branch gates each got one more OR clause for claude/det-kommer-mer-879lcm (the default branch), so that after this integration branch is promoted, every future push to the default branch keeps exercising the VPS/combined-container topology instead of silently going back to skipping it — the same gap already found and fixed for the integration branch itself in commit 52fa38a. All previously-enabled branches (claude/strato-vps-prep, claude/verify-combined-container, claude/integrate-founder-vps) are unaffected. deploy-render's gate and permanently-disabled no-op body (commit 92eb9eb) were left untouched."
    }
  ],
  "local_verification_done_before_ci": [
    "tests/backend/: 262 passed",
    "tests/security/ (RLS isolation + session auth): 22 passed",
    "tests/account/ (registration, verification, password reset, rate limiting, founder-only, deletion): 43 passed",
    "Alembic migration-check job reproduced step-by-step against the merged chain (0001-0010): fresh install to head — pass; downgrade to base and back up (round-trip) — pass; upgrade from 0002 with pre-existing user+conversation rows, verified both rows survived the upgrade to head — pass",
    "frontend: npx tsc --noEmit — clean",
    "frontend: npx eslint . — clean",
    "frontend: npx next build (Docker mode, NEXT_PUBLIC_API_URL set) — .next/standalone produced as expected",
    "frontend: npx next build (Vercel mode, VERCEL=1) — .next/standalone correctly absent"
  ],
  "ci_run_id": 29815074887,
  "ci_run_conclusion": "success",
  "ci_run_commit": "52f8878",
  "ci_run_note": "This is the run after the CI-policy commit (52f8878) that extended the 5 VPS/combined-container branch gates to also cover claude/det-kommer-mer-879lcm. It confirms that change alone did not break anything on claude/integrate-founder-vps before promotion was attempted. Earlier run 29812450928 (commit afe118d) was the first fully-green run after the merge and CI defect fixes; both are recorded here for a complete history.",
  "job_results": {
    "lint-and-typecheck (Frontend — TypeScript & ESLint)": "success",
    "npm-audit (Frontend — npm audit)": "success",
    "frontend-build (docker)": "success",
    "frontend-build (vercel)": "success",
    "backend-tests (Backend — unit/integration tests)": "success",
    "rls-security-tests (Backend — RLS & session-security tests)": "success",
    "account-rate-limit-tests (Backend — account lifecycle & rate-limit tests)": "success",
    "migration-check (Backend — Alembic migration check)": "success",
    "e2e-tests (E2E — Playwright, full stack)": "success",
    "same-origin-proxy-test (E2E — same-origin proxy)": "success",
    "combined-container-verify (Combined container — build, run, verify)": "success",
    "vps-scripts-check (VPS bootstrap scripts — shellcheck + dry-run)": "success",
    "vps-compose-verify (Strato VPS compose topology — build, run, verify)": "success",
    "vps-deploy-rollback-test (VPS deploy.sh / rollback.sh — real deploy, failure, and rollback cycle)": "success",
    "vps-backup-restore-test (VPS backup.sh / restore.sh — archive structure, checksums, restore behavior)": "success",
    "all-checks-passed (All required checks passed)": "success — genuinely required all 15 jobs above, including the 5 VPS/combined-container jobs, not just an aggregate name",
    "deploy-render (Deploy to Render (PERMANENTLY DISABLED — superseded by Strato VPS))": "skipped on this run — this push was to claude/integrate-founder-vps, not claude/det-kommer-mer-879lcm, so the job's own gate correctly excluded it. On the eventual promotion push to claude/det-kommer-mer-879lcm this job WILL run (its gate matches that branch), but its body now makes no network call at all regardless — see commit 92eb9eb."
  },
  "promotion_readiness": "YES — verified via: (1) all 16 jobs on CI run 29815074887 individually confirmed success/correctly-skipped, (2) Render deploy path permanently neutralized at the code level (commit 92eb9eb), not just via absent secrets, (3) the 5 VPS/combined-container jobs' gates now also cover claude/det-kommer-mer-879lcm so they keep running after promotion (commit 52f8878), (4) claude/det-kommer-mer-879lcm confirmed a strict ancestor of this branch (0 commits behind, 63 commits ahead as of this checkpoint) so promotion can be a genuine non-force fast-forward.",
  "prior_failing_run_for_context": {
    "ci_run_id": 29807988506,
    "conclusion": "failure",
    "commit": "52fa38a",
    "why_it_failed": "First run after extending the branch gates (commit 52fa38a) — this was the first time vps-compose-verify and vps-deploy-rollback-test actually executed against the merged code, and they found the real FOUNDER_EMAIL placeholder-fixture defect described in commit f63b4d6 above. Its own all-checks-passed job incorrectly stayed green on this run because the aggregation defect (fixed in afe118d) had not yet been fixed — this is the specific case that defect fix corrects."
  },
  "integration_defects_found_and_fixed": [
    "CI branch-gate gap: 5 VPS/combined-container jobs never ran on the integration branch, so their pass/fail was invisible (fixed in 52fa38a)",
    "CI fixture defect: FOUNDER_EMAIL=founder@lifeos.local in 2 production-mode VPS CI fixtures collided with the merged production placeholder-secret guard (fixed in f63b4d6)",
    "CI aggregation defect: all-checks-passed did not depend on the 5 VPS/combined-container jobs, so their failure couldn't fail the aggregate (fixed in afe118d)"
  ],
  "no_application_or_production_behavior_changed": "All three fixes above are CI-only (workflow branch gates, CI-only test fixtures, CI aggregation logic). No production code path, no default value in backend/app/config.py, and no behavior of _check_no_placeholder_secrets() itself was changed, weakened, bypassed, or removed.",
  "exact_next_action_when_strato_vps_is_delivered": "Once claude/det-kommer-mer-879lcm is promoted (fast-forwarded) to this integration branch's HEAD, the first real action on the actual Strato VPS remains what docs/checkpoints/VPS_PREP_CHECKPOINT_2026-07-21.md already specifies: follow docs/STRATO_VPS_DEPLOY.md from Steg 1 (scripts/vps/00_preflight.sh through 50_enable_auto_updates.sh, then 30_setup_directories.sh), populate /etc/lifeai/lifeai.env from the required env vars in scripts/vps/lib.sh, then scripts/vps/deploy.sh --confirm for the first real deploy. This checkpoint update itself performs no deploy of any kind — see promotion_readiness above and the promotion record this checkpoint is about to be followed by.",
  "promotion_plan": "claude/det-kommer-mer-879lcm (at ab225da at the time of this checkpoint update, a strict ancestor of this branch) will be fast-forwarded to claude/integrate-founder-vps's HEAD (52f8878) via a plain non-force push (git push origin 52f8878:refs/heads/claude/det-kommer-mer-879lcm) — never git push --force. claude/strato-vps-prep, claude/founder-knowledge-studio-v1, and claude/integrate-founder-vps itself are all left in place, not deleted. After the push, CI on claude/det-kommer-mer-879lcm will be verified job-by-job, including confirming deploy-render (which will now match that branch's gate and execute) performs no network/deployment action.",
  "production_safety_confirmation": "Default branch (claude/det-kommer-mer-879lcm): at the time of this checkpoint update, still at ab225da — untouched by any merge or push in this integration work so far; promotion (a plain fast-forward, see promotion_plan above) is the very next action after this checkpoint is committed. Render: untouched — deploy-render's code path can no longer make any network call at all (commit 92eb9eb), independent of which branch or which secrets exist; CI run 29815074887 shows it as 'skipped' on this branch (correct, since this push wasn't to the default branch). Production: untouched — nothing in this branch's history contacts any real production system. Real Strato VPS: never contacted — no SSH, no real domain, no real secrets anywhere; vps-compose-verify, vps-deploy-rollback-test, and vps-backup-restore-test all ran against fake local Docker images, fake digest-pinned throwaway builds, and fake CI-only secrets files (including the corrected FOUNDER_EMAIL=ci-founder@lifeai-vps-ci.invalid) inside GitHub Actions' own ephemeral runners. claude/strato-vps-prep and claude/founder-knowledge-studio-v1: both confirmed unchanged at their pre-integration commits (3d1fda9 and 893ef74 respectively) — this integration work only ever committed to and pushed claude/integrate-founder-vps."
}
```
