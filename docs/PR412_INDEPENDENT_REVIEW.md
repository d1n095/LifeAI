# PR #412 independent security review package

## Immutable subject

- PR: https://github.com/d1n095/LifeAI/pull/412
- Base: `0a4ed372dd2672c25c50e324860bd6d909b872ab`
- Candidate: `bff25dbc398094f453be00479db8027e33b8c3bd`
- Tree: `3f1a14ebdeb907eeea38adbd9315cb6b6f950419`
- Builder CI: runs `43700090179` and `43700087152`
- Builder result: 2,394 passed, 1 skipped. This is not certification.

Review only the immutable candidate. If the PR head changes, stop and bind a new review.

## Threat model

An attacker may have `mainai_app`, may submit direct SQL allowed by that role, may alter
same-named schema objects before an operator runs recovery, or may present a database whose
`alembic_version` says `0069` while its real schema comes from a different published history.
The attacker must not cause silent stamping, provenance fabrication, STOP-history mutation,
runtime access to migration authority, or data loss during downgrade/refusal.

The migration/admin role is trusted to execute Alembic. Compromise of that role or PostgreSQL
superuser is outside this review.

## Original independent findings

Inherited #386 findings:

1. Recovery accepted conflicting PR #237 STOP tables before deleting an ancestor revision.
2. Recovery checked provenance column names but not types/nullability/defaults.
3. Recovery used substring locality checks instead of complete validated CHECK definitions.

#400 findings:

4. Published #237 uses `ck_mainai_stop_prov_object`, not `ck_mainai_stop_prov`.
5. Startup's broad grants reopened lineage and STOP tables after migration-time revocation.
6. Same-named altered STOP constraints/indexes passed the fingerprint.
7. Locality classification accepted unknown CHECK definitions containing `local`.

Original review evidence is linked from PR #400 and gate comment:
https://github.com/d1n095/LifeAI/pull/359#issuecomment-6884181803

## Exact diff and identity commands

```bash
gh api repos/d1n095/LifeAI/compare/0a4ed372dd2672c25c50e324860bd6d909b872ab...bff25dbc398094f453be00479db8027e33b8c3bd
gh pr diff 412 --repo d1n095/LifeAI
gh api repos/d1n095/LifeAI/git/commits/bff25dbc398094f453be00479db8027e33b8c3bd
```

Expected changed scope: migration 0074, dual-0069 recovery, shared startup privilege policy,
and their PostgreSQL tests. No production data or external provider is required.

## Reproduction

Use a disposable PostgreSQL cluster only. Set separate admin and `mainai_app` URLs, then run:

```bash
cd backend
python -m alembic heads
python -m pytest -q \
  tests/backend/test_migration_0069_lineage_bridge.py \
  tests/backend/test_migration_0069_recovery.py \
  tests/backend/test_runtime_table_privileges.py \
  tests/backend/test_founder_alpha_migration_composition.py
```

The exact adversarial fixtures are in those files. They construct genuine PR #237 tables,
wrong/nullability columns, same-named `CHECK (true)`, non-unique same-named indexes,
`NOT VALID` constraints, combined histories and startup privilege replay.

## Required independent decisions

Return PASS/FAIL/BLOCKED for each numbered finding above. Additionally verify:

- a rejected migration leaves `alembic_version`, audit history and protected rows unchanged;
- canonical 0073 downgrade/upgrade preserves disclosure rows;
- PR #237-origin downgrade refuses and preserves STOP rows;
- `mainai_app` retains zero direct privileges after startup-policy replay;
- no unknown exact-0069 schema is classified or stamped;
- the composed verification manifest has one Alembic head and excludes PRs #403/#408/#411.

Do not approve composition merely because builder CI is green.
