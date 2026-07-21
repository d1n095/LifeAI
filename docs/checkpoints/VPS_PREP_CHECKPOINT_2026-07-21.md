# VPS Prep Checkpoint — 2026-07-21

Machine-readable snapshot of the `claude/strato-vps-prep` branch state after Phase 5-9 of the
Strato VPS preparation mission (manual-gated deploy + tested rollback, backup/restore,
operations runbook + threat model). Committed to the repo so this checkpoint survives outside
any single session's scratchpad. See `docs/VPS_ARCHITECTURE.md` for the full system picture
and `docs/STRATO_VPS_DEPLOY.md` for the deploy procedure this checkpoint validates.

```json
{
  "branch": "claude/strato-vps-prep",
  "head_commit": "3139044",
  "head_commit_full": "3139044b0a76471ab108fc0c284f8d35960a4c52",
  "working_tree": "clean",
  "ci_run_id": 29804082443,
  "ci_run_conclusion": "success",
  "required_job_status": {
    "vps-deploy-rollback-test": "PASS (job 88550882534, all 16 steps green)",
    "vps-compose-verify": "PASS (job 88550882558, all 20 steps green)",
    "vps-scripts-check": "PASS (job 88550882602, all steps green)",
    "vps-backup-restore-test": "PASS (job 88550882577, all 13 steps green)"
  },
  "all_required_checks_passed_job": "PASS (job 88551249266)",
  "completed_task_ids": [115, 116, 117, 118, 119, 120, 121, 122],
  "completed_task_summary": {
    "115": "Strato VPS: build and verify secure distribution (overall umbrella)",
    "116": "Phase 1: architecture & trust-boundary audit doc (docs/VPS_ARCHITECTURE.md)",
    "117": "Phase 2: Docker production hardening",
    "118": "Phase 3: deepen vps-compose-verify CI checks",
    "119": "Phase 4: idempotent VPS bootstrap scripts",
    "120": "Phase 5+6: manual-gated deploy + tested rollback (scripts/vps/deploy.sh, rollback.sh)",
    "121": "Phase 7: backup/restore scripts + policy doc (scripts/vps/backup.sh, restore.sh, docs/VPS_BACKUP_RESTORE.md)",
    "122": "Phase 8+9: operations runbook + threat model (docs/VPS_OPERATIONS_RUNBOOK.md, docs/VPS_THREAT_MODEL.md)"
  },
  "two_bugs_fixed_this_session": [
    {
      "bug": "deploy.sh Step 8 died immediately on `docker compose up -d` failure, never attempting automatic rollback",
      "root_cause": "An image that never passes its own HEALTHCHECK makes Compose's service_healthy dependency gate fail `compose up -d` itself before deploy.sh's own health-polling ever runs; Compose's Recreate had already replaced the previous good containers by that point, leaving the site down with zero rollback attempt.",
      "fix_commit": "3103ac7",
      "files": ["scripts/vps/deploy.sh", "scripts/vps/rollback.sh"],
      "fix_details": "Both failure shapes (compose-up failure, post-start health/verification failure) now funnel through one shared rollback block; original failure reason preserved in the deployment JSON (compose_up_failed / not_healthy_within_timeout / health_endpoint_unreachable) instead of one generic label; rollback.sh now verifies a real request through Caddy after restoring, not just container health; single rollback invocation per run, no loop/recursion; exits non-zero either way; rollback.sh still refuses to act with no prior known-good record."
    },
    {
      "bug": "ci.yml's own assertion for identifying the broken deployment's record was unreliable",
      "root_cause": "select(.backend_image | contains(\"bad\")) matched on a content-addressed sha256 digest, where the literal substring \"bad\" appearing is pure coincidence, not a reliable identifier. Confirmed the broken image's real digest did not contain \"bad\" in run 29803681256, causing the assertion to compare against an empty string even though the deploy.sh fix above was already working correctly underneath.",
      "fix_commit": "3139044",
      "files": [".github/workflows/ci.yml"],
      "fix_details": "Replaced digest-content matching with select(has(\"backend_image\")) | sort_by(.timestamp) | last — deterministic because each deploy.sh invocation writes exactly one new timestamped record, rollback.sh's own *-rollback.json records have no backend_image field (also fixes a latent null|contains() jq crash on those), and the broken attempt always runs after the good one so its record is always the latest one with a backend_image."
    }
  ],
  "remaining_tasks": {
    "123": "VPS Phase 10: supply-chain CI hardening (new isolated job — shellcheck/yamllint/hadolint/trivy-style checks, deliberately scoped to avoid touching the ~13 existing verified production CI jobs)",
    "124": "VPS Phase 13+14: secrets inventory (names/categories only, no values) + docs consolidation, including a docs/vps/START_HERE.md entry point",
    "125": "VPS Phase 15: final verification package + machine-readable handover checkpoint per the mission's original required format"
  },
  "exact_first_action_when_real_vps_available": "Follow docs/STRATO_VPS_DEPLOY.md from Steg 1: run scripts/vps/00_preflight.sh through scripts/vps/50_enable_auto_updates.sh in order on the fresh Ubuntu server, then scripts/vps/30_setup_directories.sh, populate /etc/lifeai/lifeai.env from the required_env_var_names list in scripts/vps/lib.sh's $LIFEAI_REQUIRED_ENV_VARS (see docs/VPS_SECRETS_INVENTORY.md once Phase 13 exists, or the list in lib.sh directly), then run scripts/vps/deploy.sh --confirm for the first real deploy. Nothing in this checkpoint or the branch has touched the real VPS in any way — this is genuinely the first action.",
  "production_safety_confirmation": "main branch: untouched (no merge performed, still separate from claude/strato-vps-prep). Render: untouched (no deploy triggered — the deploy-render job in ci.yml only fires for pushes to claude/det-kommer-mer-879lcm, none occurred; all work is on claude/strato-vps-prep only). Real Strato VPS: never contacted — no SSH, no real domain, no real secrets used anywhere; all deploy.sh/rollback.sh/backup.sh/restore.sh testing used fake local Docker registries, fake digest-pinned throwaway images, and fake secrets files under CI's own ephemeral workspace or local scratch directories. No paid infrastructure created."
}
```
