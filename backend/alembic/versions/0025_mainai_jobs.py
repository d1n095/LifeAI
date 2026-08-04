"""MainAI Runtime Truthfulness and Durable Job Foundation.

Adds the first general-purpose, owner-scoped, durable job model for MainAI's own runtime —
distinct from `knowledge_import_jobs` (Life Library's ZIP/file import pipeline specifically)
and `agent_tasks`/`agent_task_events` (the founder-wide GitHub code/review agent-dispatch
loop, not owner-scoped, no lease/heartbeat/cancellation). The goal this migration exists to
serve: MainAI must never be able to claim it "started" or "is working" on something unless a
real, persisted row exists that a human (or an automated recovery pass) can independently
observe, cancel, and see fail or complete — see app/mainai_jobs/contract.py for the response
modes this schema backs.

Three tables, same RLS-per-table pattern every owner-scoped table in this codebase already
uses (see app/rls.py, and migration 0007's `knowledge_claims`/`claim_relationships` for the
most directly comparable precedent — this migration follows that exact template):

  - `mainai_jobs`: one row per durable job. Columns map directly to the founder's required
    fields: persistent id, job_type, owner, the five lifecycle timestamps
    (created/started/heartbeat/completed), status, progress counters, current_phase, a
    bounded-length safe public_message, a coarse error_category (never raw exception text —
    see app/mainai_jobs/service.py), retry_count, input_refs/output_refs (JSON artifact
    references), worker/provider/model attribution, cancel_requested/cancel_acknowledged, and
    an idempotency_key unique per owner.
  - `mainai_job_events`: append-only execution-event history (created/claimed/heartbeat/
    phase_changed/progress_updated/cancel_requested/cancel_acknowledged/completed/failed/
    cancelled/retry_scheduled) — application code only ever INSERTs here, never UPDATEs or
    DELETEs (enforced by convention + tests, not a DB-level immutability trigger, matching
    this codebase's existing append-only tables like `project_notes`).
  - `mainai_job_proposals`: the first job type's (`corpus_review`, app/rag/corpus_review_job.py)
    output. Deliberately its OWN table, NOT a write into `knowledge_claims` — the founder's
    explicit requirement that AI interpretation is never treated as founder-approved truth
    means this must not silently join the claim-trust system's own status/confidence
    machinery. A proposal starts `proposed` and can only ever become `dismissed`; nothing in
    this schema or its application code can promote a proposal into a KnowledgeClaim
    automatically.

`owner_id` is denormalized onto `mainai_job_events`/`mainai_job_proposals` (not looked up via
a join in the RLS policy) even though both are logically children of `mainai_jobs` — the same
choice `document_chunks.owner_id` already makes relative to `documents`, for the same reason:
a single-column, non-join RLS predicate is what every other policy in app/rls.py uses, and
mixing join-based and column-based policies in the same schema would be a real, easy-to-miss
inconsistency for a future reviewer.

Revision ID: 0025
Revises: 0018
Create Date: 2026-08-04

NOTE: this branch (claude/mainai-job-runtime-foundation) is built from the base branch
(claude/det-kommer-mer-879lcm), whose migration head is `0018` — NOT the S1A slice's `0024`
(that work lives entirely on the still-unmerged PR #31 branch). `down_revision` points at
`0018` deliberately, matching this branch's real base; the `0025` id itself was chosen to stay
clear of `0019`-`0024`'s number range purely so the two chains are easy to eyeball as distinct
during review — it does NOT make them independently mergeable. If both this branch and PR #31
merge as-is, the result is two divergent Alembic heads off `0018`. This branch's chain must be
rebased onto PR #31's actual final revision (this migration's `down_revision` updated from
`0018` accordingly) BEFORE this branch is opened as a PR — see docs/MAINAI_JOB_RUNTIME.md's
"Relationship to PR #31" section for the full explanation. Do not treat this numbering choice
as proof the two branches can be reviewed or merged in either order.
"""

from alembic import op

revision = "0025"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE mainai_jobs (
            id uuid NOT NULL PRIMARY KEY,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            job_type varchar(32) NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'queued',
            created_at timestamp without time zone NOT NULL,
            started_at timestamp without time zone,
            last_heartbeat_at timestamp without time zone,
            completed_at timestamp without time zone,
            progress_current integer NOT NULL DEFAULT 0,
            progress_total integer,
            current_phase varchar(64),
            public_message varchar(500),
            error_category varchar(32),
            retry_count integer NOT NULL DEFAULT 0,
            max_retries integer NOT NULL DEFAULT 3,
            input_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            output_refs jsonb NOT NULL DEFAULT '[]'::jsonb,
            locked_by varchar(128),
            provider varchar(32),
            model varchar(64),
            cancel_requested boolean NOT NULL DEFAULT false,
            cancel_acknowledged boolean NOT NULL DEFAULT false,
            lease_expires_at timestamp without time zone,
            idempotency_key varchar(128),
            created_by varchar(64) NOT NULL,
            CONSTRAINT ck_mainai_jobs_status CHECK (
                status IN ('queued', 'running', 'paused', 'completed', 'failed', 'cancelled')
            ),
            CONSTRAINT ck_mainai_jobs_progress_current_nonneg CHECK (progress_current >= 0),
            CONSTRAINT ck_mainai_jobs_progress_total_nonneg CHECK (progress_total IS NULL OR progress_total >= 0),
            CONSTRAINT ck_mainai_jobs_retry_count_nonneg CHECK (retry_count >= 0),
            CONSTRAINT ck_mainai_jobs_max_retries_nonneg CHECK (max_retries >= 0)
        );
        CREATE INDEX ix_mainai_jobs_owner_id ON mainai_jobs USING btree (owner_id);
        CREATE INDEX ix_mainai_jobs_status ON mainai_jobs USING btree (status);
        CREATE INDEX ix_mainai_jobs_lease_expires_at ON mainai_jobs USING btree (lease_expires_at);
        -- Idempotency is scoped per-owner, not global: two different owners submitting the
        -- same idempotency_key are unrelated requests, not a collision.
        CREATE UNIQUE INDEX uq_mainai_jobs_owner_idempotency_key
            ON mainai_jobs (owner_id, idempotency_key)
            WHERE idempotency_key IS NOT NULL;

        ALTER TABLE mainai_jobs ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_jobs FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_jobs_isolation ON mainai_jobs
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE TABLE mainai_job_events (
            id uuid NOT NULL PRIMARY KEY,
            job_id uuid NOT NULL REFERENCES mainai_jobs(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            event_type varchar(32) NOT NULL,
            detail jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamp without time zone NOT NULL,
            CONSTRAINT ck_mainai_job_events_event_type CHECK (
                event_type IN (
                    'created', 'claimed', 'heartbeat', 'phase_changed', 'progress_updated',
                    'cancel_requested', 'cancel_acknowledged', 'completed', 'failed',
                    'cancelled', 'retry_scheduled'
                )
            )
        );
        CREATE INDEX ix_mainai_job_events_job_id ON mainai_job_events USING btree (job_id);
        CREATE INDEX ix_mainai_job_events_owner_id ON mainai_job_events USING btree (owner_id);
        CREATE INDEX ix_mainai_job_events_created_at ON mainai_job_events USING btree (created_at);

        ALTER TABLE mainai_job_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_job_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_job_events_isolation ON mainai_job_events
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    op.execute("""
        CREATE TABLE mainai_job_proposals (
            id uuid NOT NULL PRIMARY KEY,
            job_id uuid NOT NULL REFERENCES mainai_jobs(id) ON DELETE CASCADE,
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            source_document_id uuid REFERENCES documents(id) ON DELETE SET NULL,
            source_chunk_id uuid REFERENCES document_chunks(id) ON DELETE SET NULL,
            proposal_type varchar(32) NOT NULL,
            proposal_text text NOT NULL,
            status varchar(16) NOT NULL DEFAULT 'proposed',
            created_at timestamp without time zone NOT NULL,
            CONSTRAINT ck_mainai_job_proposals_status CHECK (status IN ('proposed', 'dismissed'))
        );
        CREATE INDEX ix_mainai_job_proposals_job_id ON mainai_job_proposals USING btree (job_id);
        CREATE INDEX ix_mainai_job_proposals_owner_id ON mainai_job_proposals USING btree (owner_id);
        CREATE INDEX ix_mainai_job_proposals_source_document_id ON mainai_job_proposals USING btree (source_document_id);

        ALTER TABLE mainai_job_proposals ENABLE ROW LEVEL SECURITY;
        ALTER TABLE mainai_job_proposals FORCE ROW LEVEL SECURITY;
        CREATE POLICY mainai_job_proposals_isolation ON mainai_job_proposals
        USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
        WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("DROP TABLE mainai_job_proposals;")
    op.execute("DROP TABLE mainai_job_events;")
    op.execute("DROP TABLE mainai_jobs;")
