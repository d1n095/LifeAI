# R15 evidence — how each file was produced

All files here were produced on 2026-11-19 against a local stack: Postgres 16 on port 5433,
API on `127.0.0.1:8011`, worker (`python -m app.worker`), Ollama on `127.0.0.1:11434`,
frontend on `localhost:3021`. Nothing here is a mock: every number came from a real request or a
real test run against the rebuilt code.

| File | Produced by | Notes |
| --- | --- | --- |
| `ai_benchmark.json` | `backend/scripts/benchmarks/local_chat_model_benchmark.py --models qwen2.5:0.5b-instruct qwen2.5:3b-instruct gemma3:4b` | Swedish project questions through the real `/api/chat` path with the real `PROJEKTSTATUS` block. Fails hard on invented numbers or false action claims (see `verdict()`). |
| `founder_journey.json` | `founder_journey.json` (script: `backend/scripts/acceptance/founder_r15_journey.py`) | login → project status → Swedish question → goal → AI plan → approval (if any) → worker execution → report → status after → follow-up. |
| `restart_check.json` | `backend/scripts/acceptance/restart_check.py` run after stopping and restarting API + worker | read-only: proves the same goals/reports/priorities survive a process restart. |

## Rebuilding the gate database

```bash
set -a; . /tmp/lifeai-r15.env; set +a
backend/scripts/acceptance/reset_gate_db.sh
```

The script drops only `lifeos_r15_gate`, applies migrations `0001 → 0089`, grants the restricted
`mainai_app` role and runs `scripts/security/apply_runtime_privileges.py`. The API refuses to start
without that last step, which is why it is part of the reset rather than a manual note.

## What is deliberately absent

- No GitHub live snapshot: `GITHUB_TOKEN`/`GITHUB_REPO` are not configured, so the product reports
  "inte konfigurerad" and no snapshot could be truthfully recorded.
- No `z-ai` gate run: the CLI is not installed; `first_cycle_go` stays `false`.
- No off-host backup evidence: no destination or encryption key exists in this environment.
