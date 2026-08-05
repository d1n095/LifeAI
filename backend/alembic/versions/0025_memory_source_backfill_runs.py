"""Durable production backfill-run reporting (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8,
PR #31's own module docstring for `app/rag/memory_source_backfill.py`): the last blocker that
docstring names before `backfill_memory_source_units()` may ever be run against production —
"a durable run record with (at minimum): a run id, owner_id, started_at/completed_at, status,
the per-source-kind/skipped/failed/already_done counters this module already computes, a
resumable cursor ..., and a claim-level failure log". This migration adds exactly that.

`memory_source_backfill_runs` — one row per owner-scoped backfill run. `mode` separates
`dry_run` (read-only, tallies only, never writes `memory_source_id` — see
`memory_source_backfill.py`'s own dry_run docstring) from `real` (actually resolves and links
claims) so a caller can never confuse a rehearsal with a real run in the durable record itself.
`status` is `pending` until the first `advance_backfill_run()` call, `running` while more
candidates remain, `completed` ONLY when a call to `backfill_memory_source_units()` reports
zero remaining candidates for this owner (never inferred from a batch cap being hit — a capped
run that still has real candidates left stays `running`, not `completed`, so this table can
never make a "finished" claim that isn't actually true). `failed` is reserved for an unexpected
exception escaping the backfill call entirely (defense in depth; the backfill function itself
already catches and tallies ordinary per-claim failures into `failed_count` without aborting
the run). `cancelled` is operator-triggered.

`last_cursor_created_at`/`last_cursor_claim_id` is the resumable checkpoint: the newest
`(created_at, id)` pair actually considered (in any outcome — linked, skipped, or failed) by
the most recent `advance_backfill_run()` call. Real-mode runs don't strictly NEED this to stay
correct across a crash (a claim's own `memory_source_id IS NULL` is already the natural,
correct restart marker — see `memory_source_backfill.py`'s own concurrency/restart-safety
section), but dry-run mode writes nothing at all, so without an explicit cursor a resumed
dry-run would re-scan and DOUBLE-COUNT every claim from the very first `advance()` call. The
same cursor mechanism is applied uniformly to both modes for one consistent, always-correct
checkpoint, and for operator visibility ("how far did run X get") independent of mode.

`total_candidates_snapshot`/`already_done_snapshot` are point-in-time counts taken once when
the run is created — a progress denominator, not a guarantee (new claims can still be created,
or already-linked ones re-queried, while a run is in flight; this is documented, not hidden).

Deliberately owned/scoped like every other owner table (`owner_id` FK `users.id` ON DELETE
CASCADE, standard RLS isolation policy below) — NOT added to `backend/scripts/
s1a_privilege_policy.py`'s `_PROTECTED_TABLES` narrow-privilege allowlist, because unlike
`storage_deletion_tasks` (indirect access to a real physical DELETE) a row here carries no
privileged side effect at all: it is pure progress/outcome metadata about claims the owner
already has RLS-scoped access to. `mainai_app`'s ordinary wide `GRANT ALL` (ensure_app_role.py)
plus RLS is the correct, proportionate privilege level, same as `KnowledgeClaim`/`ImportJob`.

`memory_source_backfill_failures` — one row per claim that failed to resolve or link during a
run, keyed by `(run_id, claim_id)` so a re-considered claim in a later run never produces a
duplicate row for the SAME run. `reason` is the exact structural failure string
`_resolve_locator()`/`_apply()` already produce (document/chunk/version ids and owner id only —
never `KnowledgeClaim.claim_text`, per the explicit "claim-level error records without storing
claim text" requirement). `claim_id` is a plain FK, ON DELETE CASCADE — if the claim itself is
later deleted (e.g. its source document is purged), the failure record documenting it is
meaningless and correctly disappears with it.
"""

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE memory_source_backfill_runs (
            id uuid PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            mode varchar(16) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'pending',
            batch_size integer NOT NULL,
            total_candidates_snapshot integer NOT NULL DEFAULT 0,
            already_done_snapshot integer NOT NULL DEFAULT 0,
            processed_count integer NOT NULL DEFAULT 0,
            exact_chunk_count integer NOT NULL DEFAULT 0,
            degraded_version_count integer NOT NULL DEFAULT 0,
            missing_document_only_count integer NOT NULL DEFAULT 0,
            skipped_unresolvable_count integer NOT NULL DEFAULT 0,
            failed_count integer NOT NULL DEFAULT 0,
            batches_completed integer NOT NULL DEFAULT 0,
            last_cursor_created_at timestamptz,
            last_cursor_claim_id uuid,
            error_summary text,
            started_at timestamptz,
            completed_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_msbr_mode CHECK (mode IN ('dry_run', 'real')),
            CONSTRAINT ck_msbr_status CHECK (
                status IN ('pending', 'running', 'completed', 'failed', 'cancelled')
            ),
            CONSTRAINT ck_msbr_batch_size CHECK (batch_size > 0),
            CONSTRAINT ck_msbr_counters_non_negative CHECK (
                total_candidates_snapshot >= 0 AND already_done_snapshot >= 0
                AND processed_count >= 0 AND exact_chunk_count >= 0
                AND degraded_version_count >= 0 AND missing_document_only_count >= 0
                AND skipped_unresolvable_count >= 0 AND failed_count >= 0
                AND batches_completed >= 0
            ),
            CONSTRAINT ck_msbr_cursor_pair CHECK (
                (last_cursor_created_at IS NULL) = (last_cursor_claim_id IS NULL)
            ),
            -- Founder review round 3 (point 6): a database-enforced tripwire for the
            -- outcome-bucket invariant app/rag/memory_source_backfill_run.py's
            -- _make_on_claim_outcome() maintains by construction (each considered claim
            -- increments processed_count and exactly ONE outcome bucket, atomically, in the
            -- same commit — see that module's docstring). Monotonicity (counters never
            -- decrease, cursor never moves backward) is enforced at the service layer
            -- (_assert_monotonic()) rather than here, since a plain CHECK constraint only ever
            -- sees the current row and cannot compare against the pre-UPDATE value.
            CONSTRAINT ck_msbr_processed_count_matches_sum CHECK (
                processed_count = exact_chunk_count + degraded_version_count
                    + missing_document_only_count + skipped_unresolvable_count + failed_count
            )
        );

        CREATE INDEX ix_msbr_owner_id ON memory_source_backfill_runs (owner_id);
        CREATE INDEX ix_msbr_status ON memory_source_backfill_runs (status);
        -- Idempotent reruns / no silently-duplicated concurrent runs: only one active
        -- (pending or running) run per owner at a time, regardless of mode. A caller that
        -- wants to start a new run while one is still active must resume the existing one.
        CREATE UNIQUE INDEX uq_msbr_one_active_per_owner ON memory_source_backfill_runs (owner_id)
            WHERE status IN ('pending', 'running');

        CREATE TABLE memory_source_backfill_failures (
            id uuid PRIMARY KEY,
            run_id uuid NOT NULL REFERENCES memory_source_backfill_runs (id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            claim_id uuid NOT NULL REFERENCES knowledge_claims (id) ON DELETE CASCADE,
            reason text NOT NULL,
            attempt_count integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_msbf_run_claim UNIQUE (run_id, claim_id),
            CONSTRAINT ck_msbf_attempt_count CHECK (attempt_count > 0)
        );

        CREATE INDEX ix_msbf_run_id ON memory_source_backfill_failures (run_id);
        CREATE INDEX ix_msbf_owner_id ON memory_source_backfill_failures (owner_id);
    """)

    # --- RLS ---------------------------------------------------------------------------
    op.execute("""
        ALTER TABLE memory_source_backfill_runs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memory_source_backfill_runs FORCE ROW LEVEL SECURITY;
        CREATE POLICY memory_source_backfill_runs_isolation ON memory_source_backfill_runs
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE memory_source_backfill_failures ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memory_source_backfill_failures FORCE ROW LEVEL SECURITY;
        CREATE POLICY memory_source_backfill_failures_isolation ON memory_source_backfill_failures
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS memory_source_backfill_failures;
        DROP TABLE IF EXISTS memory_source_backfill_runs;
    """)
