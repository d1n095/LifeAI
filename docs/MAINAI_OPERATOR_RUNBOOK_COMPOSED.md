# MainAI Operator Runbook — Composed Executive (safe internal)

Companion to `docs/MAINAI_COMPOSED_EXECUTIVE_LOOP.md` and (after #234 merges)
`docs/MAINAI_STARTUP_READINESS.md` / `docs/MAINAI_FIRST_SAFE_INTERNAL_RUN.md`.

## What she can do today (safe internal)

| Action | How |
|---|---|
| Start an executive cycle | `run_executive_cycle(db, owner_id=..., founder_request=..., source_entity_id=...)` |
| Inspect status | `executive_status_snapshot(db, owner_id=..., session_id=...)` |
| Resume after kill/restart | `resume_executive_cycle(db, owner_id=..., session_id=...)` — durable checkpoint only |
| Workforce dry-run | Included in cycle with `activate_provider=False` (hard) |

## What she must NOT do yet

- Provider invoke (`activate_provider=True` refused)
- Treat memory / plan / staffing as execution authority
- Self-verify Claude gates #218 / #229 / #213 / #224

## Kill / disable (after #234 lands on tip)

- Kill-switch module: `app.workforce.kill_switch` (PR #234)
- Startup readiness levels: `app.mainai_startup_readiness` (never one boolean)
- Until #234 is merged, refuse provider paths via existing `vertical_slice` / `provider_worker` gates

## Health / dependencies (operator checklist)

1. Postgres reachable (migrations through current Alembic head)
2. App DB role + RLS session (`app.current_user_id`)
3. No requirement on external providers for SAFE INTERNAL RUN
4. Continuity: founder_memory notes with `provenance.kind == mainai_executive_continuity_v1`

## Shutdown / restart

1. Process stop is safe mid-cycle if a continuity checkpoint was written
2. On restart: load checkpoint by `session_id`; do not invent missing authority
3. Founder confirmation required before any authorize / provider path

## Founder inspectables (no raw table dump)

- current goal / phase / horizons
- executive work candidates (unreviewed only)
- authority_state (always false for executive-held authority)
- last recovery summary
- open risks (provider disabled; planning-only)

## Next ops after #234 merge

Rebase this branch onto tip, then wire `safe_internal_run` + kill-switch into
`run_executive_cycle` observability without enabling provider invoke.
