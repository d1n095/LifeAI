# Founder Alpha QA gate — BLOCKED

Commit: `c527543cc33722dcc6bea7b7c04606723dff803b`

- **PASS** — exact commit SHA: `"c527543cc33722dcc6bea7b7c04606723dff803b"`
- **PASS** — tracked tree clean: `"clean"`
- **PASS** — PostgreSQL reachable: `""`
- **PASS** — Redis reachable: `""`
- **PASS** — Alembic revision: `"0091"`
- **PASS** — installation matrix: `{"PASS": 12, "FAIL": 0, "BLOCKED": 0}`
- **PASS** — resource/performance probe: `{"status": "PASS", "blockers": []}`
- **PASS** — backup/restore recovery: `"PASS"`
- **PASS** — backend tests: `""`
- **PASS** — real browser E2E: `""`
- **BLOCKED** — F01-F24 program matrix: `""`

## Known limitations

- Z.ai CLI-dependent tests are not run in this environment.
- `zip`-dependent tests are skipped when the binary is unavailable.
- pg_stat_statements is not installed, so slow-query statistics are BLOCKED rather than guessed.
- Resource-exhaustion behavior (CPU/memory/disk limits) is not measured.
- Exact-head GitHub Actions cannot run without an approved push.
