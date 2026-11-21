# V8 secure delivery and staging integration pipeline

These commands move an agent-delivered Git bundle into a verified, isolated integration
candidate without modifying the agent's branches or production data.

## Safety model

- Bundle import verifies the SHA-256 manifest before touching the repository.
- `git bundle verify` rejects corrupt bundles and missing prerequisites.
- The only ref created is `refs/imports/lifeai/<commit>`; no branch is updated and no push
  is performed.
- A bundle is never checked out or executed during validation. The optional worktree is
  detached at the exact commit and is removed without touching the original commit.
- Symlinks, submodules, unsafe paths, credential files and high-confidence secret patterns
  are rejected. Findings contain paths and rule names, never the matching secret line.
- Conflict analysis is read-only and never resolves a conflict. Security-sensitive overlap
  is marked for manual review.

## Commands

```bash
# 1. Import and verify an agent bundle.
python scripts/integration/bundle_import.py \
  --bundle delivery/v7-testlab/lifeai-v7-testlab.bundle \
  --manifest delivery/v7-testlab/SHA256SUMS \
  --expected-commit 8917f0ca4f5a64e57a8eda623d57763145bd9cde \
  --expected-base 56190209cdfb462f2127f4bc04f91fc2b9b5efe3 \
  --target-ref origin/integration/founder-alpha-20261121 \
  --json-out delivery/v8-integration/evidence/import-v7.json

# 2. Compare the imported commit with the integration branch.
python scripts/integration/conflict_analysis.py \
  --candidate 8917f0ca4f5a64e57a8eda623d57763145bd9cde \
  --target origin/integration/founder-alpha-20261121 \
  --out delivery/v8-integration/evidence/conflicts-v7.json

# 3. Build the immutable staging manifest from verified delivery inputs.
python scripts/integration/release_manifest.py build \
  --input delivery/v8-integration/imports.json \
  --out delivery/v8-integration/staging-manifest.json

# 4. Run exact-SHA staging checks in a detached worktree.
python scripts/integration/staging_build.py \
  --sha 8917f0ca4f5a64e57a8eda623d57763145bd9cde \
  --database-url "postgresql://postgres@127.0.0.1:55432/lifeos_v8_staging_pytest" \
  --app-database-url "postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeos_v8_staging_pytest" \
  --redis-url "redis://127.0.0.1:56379/15" \
  --junit-out delivery/v8-integration/evidence/staging-security.xml \
  --out delivery/v8-integration/evidence/staging-build.json

# 5. Prove that abandoning staging preserves commits and leaves no worktree/ref.
python scripts/integration/rollback_verify.py \
  --source-ref 8917f0ca4f5a64e57a8eda623d57763145bd9cde \
  --database-url "postgresql://postgres@127.0.0.1:55432/lifeos_v8_staging_pytest" \
  --out delivery/v8-integration/evidence/rollback.json
```

The database URL is only accepted when it is loopback and its name contains
`test`, `staging` or `pytest`. Staging verification never runs `alembic upgrade` against
that database; pytest's own isolated test fixture may create/drop its disposable database.
The existing PR #461 backup/restore implementation remains
`backend/scripts/acceptance/backup_restore_check.py`; V8 does not introduce a second one.

## Status semantics

- `PASS`: every required check ran and passed.
- `FAIL`: a code, security, provenance or migration defect was found.
- `BLOCKED`: an environmental prerequisite (test database, frontend `node_modules`, etc.)
  was unavailable, so a PASS is not claimed.
