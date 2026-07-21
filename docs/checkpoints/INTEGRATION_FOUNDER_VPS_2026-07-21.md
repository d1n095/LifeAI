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
  "head_commit": "afe118d",
  "head_commit_full": "afe118d7abfa7dc7216d80506553e53d1cfa8808",
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
      "detail": "all-checks-passed's needs: list never included combined-container-verify or the four vps-* jobs, so even after the branch-gate commit let them run on this branch, their failure (as seen in run 29807988506: vps-compose-verify and vps-deploy-rollback-test both failed) could not affect the aggregate — it reported success anyway. Fixed by adding all five to needs:. The existing if: always() keeps all-checks-passed running even when a dependency is skipped, and the existing contains(needs.*.result, 'failure'/'cancelled') check now covers them. Jobs that are still branch-gated off correctly report result=\"skipped\" on branches that don't enable them (e.g. claude/det-kommer-mer-879lcm), which the check already treats as passing, so nothing there changed. Only a genuine failure of one of these jobs on a branch where it actually executes now fails the aggregate. deploy-render's own gate (claude/det-kommer-mer-879lcm only, needs.all-checks-passed.result == 'success') was left untouched — this does not enable any deployment."
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
  "ci_run_id": 29812450928,
  "ci_run_conclusion": "success",
  "ci_run_commit": "afe118d",
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
    "deploy-render (Deploy to Render)": "skipped — its gate (github.ref == 'refs/heads/claude/det-kommer-mer-879lcm') was never touched, and this branch is not that branch, so no deploy was triggered or possible"
  },
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
  "exact_next_action_when_strato_vps_is_delivered": "Do NOT deploy from claude/integrate-founder-vps directly and do NOT deploy from claude/strato-vps-prep or claude/founder-knowledge-studio-v1 individually — this integration branch exists only to prove the two feature lines merge and pass CI together. The default branch (claude/det-kommer-mer-879lcm) still carries its own Render-specific deploy gate (deploy-render, gated to that branch) that must not be triggered by an obsolete/premature deploy. Promotion of this integration branch's content into the default branch is a separate, explicit action to be handled on its own — not part of this checkpoint — specifically so that promotion can be reviewed for the Render deploy-gate implications before it happens. Once promotion is done deliberately, the first real action on the actual Strato VPS remains what docs/checkpoints/VPS_PREP_CHECKPOINT_2026-07-21.md already specifies: follow docs/STRATO_VPS_DEPLOY.md from Steg 1 (scripts/vps/00_preflight.sh through 50_enable_auto_updates.sh, then 30_setup_directories.sh), populate /etc/lifeai/lifeai.env from the required env vars in scripts/vps/lib.sh, then scripts/vps/deploy.sh --confirm for the first real deploy.",
  "production_safety_confirmation": "Default branch (claude/det-kommer-mer-879lcm): untouched — no merge or push to it occurred anywhere in this integration work. Render: untouched — deploy-render's gate is unchanged and restricted to the default branch only; CI run 29812450928 shows it as 'skipped' on this branch, confirming no deploy was triggered. Production: untouched — nothing in this branch's history contacts any real production system. Real Strato VPS: never contacted — no SSH, no real domain, no real secrets anywhere; vps-compose-verify, vps-deploy-rollback-test, and vps-backup-restore-test all ran against fake local Docker images, fake digest-pinned throwaway builds, and fake CI-only secrets files (including the corrected FOUNDER_EMAIL=ci-founder@lifeai-vps-ci.invalid) inside GitHub Actions' own ephemeral runners. claude/strato-vps-prep and claude/founder-knowledge-studio-v1: both confirmed unchanged at their pre-integration commits (3d1fda9 and 893ef74 respectively) — this integration work only ever committed to and pushed claude/integrate-founder-vps."
}
```
