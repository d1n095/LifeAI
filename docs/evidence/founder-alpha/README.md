# Founder Alpha system reliability evidence

Generated against `claude/founder-alpha-reliability` at QA-gate commit
`216f29098474a58be88cffdf65f060d8d6000b08`. The evidence commit after it changes only
documentation, never application code.

## Files

- `install-matrix.json` — 12 real-service installation/failure scenarios.
- `resource-probe.json` — startup, API, CPU/RAM, DB/queue, disk and concurrency measurements.
- `recovery.json` — isolated PostgreSQL dump/checksum/restore, RLS and restored-API checks.
- `playwright-resilience.json` — 3/3 real browser tests for reload, network interruption and
  project/assignment creation.
- `backend-tests.xml` — 31 focused backend tests (startup/head check, RLS, durable delivery).
- `qa-gate/qa-gate.json` and `qa-gate/qa-gate.md` — the executable gate result.

## Result

**PASS** at the gate commit. The gate checked exact SHA, clean tracked tree, PostgreSQL, Redis,
Alembic `0091`, install matrix, JUnit totals, Playwright JSON, recovery and performance.

## Known limitations

- Exact-head GitHub Actions was not run because no push was approved.
- 10 tests require the Z.ai CLI and 1 requires `zip`.
- `pg_stat_statements` is not installed; slow-query statistics are BLOCKED, not guessed.
- Resource exhaustion (CPU/memory/disk limits) was not measured.
- The installation matrix covers the listed failure modes, not every possible provider or
  network failure.
