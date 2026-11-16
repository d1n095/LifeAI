"""Persist an owner-scoped import intent before canonical blob publication.

Revision ID: 0074_import_job_staging_intents
Revises: 0073_provider_disclosure_local
Create Date: 2026-11-16
"""

from alembic import op

revision = "0074_import_job_staging_intents"
down_revision = "0073_provider_disclosure_local"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE knowledge_import_jobs
            ADD COLUMN staging_storage_key varchar(140),
            ADD COLUMN upload_lease_token uuid,
            ADD COLUMN upload_lease_expires_at timestamp without time zone,
            ADD COLUMN staging_reconcile_count integer NOT NULL DEFAULT 0,
            ADD COLUMN staging_reconcile_after timestamp without time zone,
            ADD CONSTRAINT uq_knowledge_import_jobs_staging_storage_key UNIQUE (staging_storage_key),
            ADD CONSTRAINT ck_knowledge_import_jobs_staging_reconcile_count CHECK (staging_reconcile_count >= 0);

        CREATE INDEX ix_knowledge_import_jobs_upload_lease_expires_at
            ON knowledge_import_jobs (upload_lease_expires_at);
        CREATE INDEX ix_knowledge_import_jobs_staging_reconcile_after
            ON knowledge_import_jobs (staging_reconcile_after);
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION public.storage_key_still_referenced_global(p_storage_key text)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF p_storage_key IS NULL THEN
                RETURN false;
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.documents
                WHERE storage_key = p_storage_key AND deleted_at IS NULL
            ) THEN
                RETURN true;
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.knowledge_import_jobs j
                WHERE j.staging_storage_key = p_storage_key
                   OR (
                        j.source_storage_key = p_storage_key
                        AND (
                            j.status IN ('pending', 'running', 'blocked')
                            OR (j.status = 'partial' AND j.blocked_count > 0)
                            OR EXISTS (
                                SELECT 1 FROM public.documents sib
                                WHERE sib.import_job_id = j.id
                                  AND sib.deleted_at IS NULL
                                  AND sib.status::text IN (
                                      'pending', 'received', 'original_storing', 'original_stored',
                                      'extracting', 'extracted', 'awaiting_classification',
                                      'classifying', 'embedding', 'indexing'
                                  )
                            )
                        )
                   )
            ) THEN
                RETURN true;
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.project_sources
                WHERE storage_key = p_storage_key
            ) THEN
                RETURN true;
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.project_checkpoints
                WHERE brief_storage_key = p_storage_key
            ) THEN
                RETURN true;
            END IF;

            RETURN false;
        END;
        $$;
        REVOKE ALL ON FUNCTION public.storage_key_still_referenced_global(text) FROM PUBLIC;
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION public.enqueue_account_erasure_storage_task(
            p_operation_id uuid,
            p_storage_key text
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_caller uuid;
            v_owns boolean;
        BEGIN
            v_caller := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_caller IS NULL THEN
                RAISE EXCEPTION 'enqueue_account_erasure_storage_task: no authenticated user context';
            END IF;
            IF p_storage_key IS NULL OR btrim(p_storage_key) = '' THEN
                RAISE EXCEPTION 'enqueue_account_erasure_storage_task: storage_key is required';
            END IF;

            SELECT EXISTS (
                SELECT 1 FROM public.documents
                WHERE storage_key = p_storage_key AND uploaded_by = v_caller
                UNION ALL
                SELECT 1 FROM public.knowledge_import_jobs
                WHERE (source_storage_key = p_storage_key OR staging_storage_key = p_storage_key)
                  AND owner_id = v_caller
            ) INTO v_owns;
            IF NOT v_owns THEN
                RAISE EXCEPTION
                    'enqueue_account_erasure_storage_task: storage_key % is not owned by the caller', p_storage_key;
            END IF;

            INSERT INTO public.storage_deletion_tasks (id, operation_id, storage_key, reason, status)
            VALUES (gen_random_uuid(), p_operation_id, p_storage_key, 'account_erasure', 'pending')
            ON CONFLICT (operation_id, storage_key) DO NOTHING;
        END;
        $$;
        REVOKE ALL ON FUNCTION public.enqueue_account_erasure_storage_task(uuid, text) FROM PUBLIC;
    """)


def downgrade() -> None:
    op.execute("""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM public.knowledge_import_jobs
                WHERE staging_storage_key IS NOT NULL OR status = 'receiving'
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade migration 0074 while staged upload intents or references exist';
            END IF;
        END;
        $$;
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION public.storage_key_still_referenced_global(p_storage_key text)
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF p_storage_key IS NULL THEN
                RETURN false;
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.documents
                WHERE storage_key = p_storage_key AND deleted_at IS NULL
            ) THEN
                RETURN true;
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.knowledge_import_jobs j
                WHERE j.source_storage_key = p_storage_key
                  AND (
                      j.status IN ('pending', 'running', 'blocked')
                      OR (j.status = 'partial' AND j.blocked_count > 0)
                      OR EXISTS (
                          SELECT 1 FROM public.documents sib
                          WHERE sib.import_job_id = j.id
                            AND sib.deleted_at IS NULL
                            AND sib.status::text IN (
                                'pending', 'received', 'original_storing', 'original_stored',
                                'extracting', 'extracted', 'awaiting_classification',
                                'classifying', 'embedding', 'indexing'
                            )
                      )
                  )
            ) THEN
                RETURN true;
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.project_sources
                WHERE storage_key = p_storage_key
            ) THEN
                RETURN true;
            END IF;

            IF EXISTS (
                SELECT 1 FROM public.project_checkpoints
                WHERE brief_storage_key = p_storage_key
            ) THEN
                RETURN true;
            END IF;

            RETURN false;
        END;
        $$;
        REVOKE ALL ON FUNCTION public.storage_key_still_referenced_global(text) FROM PUBLIC;
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION public.enqueue_account_erasure_storage_task(
            p_operation_id uuid,
            p_storage_key text
        ) RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_caller uuid;
            v_owns boolean;
        BEGIN
            v_caller := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_caller IS NULL THEN
                RAISE EXCEPTION 'enqueue_account_erasure_storage_task: no authenticated user context';
            END IF;
            IF p_storage_key IS NULL OR btrim(p_storage_key) = '' THEN
                RAISE EXCEPTION 'enqueue_account_erasure_storage_task: storage_key is required';
            END IF;

            SELECT EXISTS (
                SELECT 1 FROM public.documents
                WHERE storage_key = p_storage_key AND uploaded_by = v_caller
                UNION ALL
                SELECT 1 FROM public.knowledge_import_jobs
                WHERE source_storage_key = p_storage_key AND owner_id = v_caller
            ) INTO v_owns;
            IF NOT v_owns THEN
                RAISE EXCEPTION
                    'enqueue_account_erasure_storage_task: storage_key % is not owned by the caller', p_storage_key;
            END IF;

            INSERT INTO public.storage_deletion_tasks (id, operation_id, storage_key, reason, status)
            VALUES (gen_random_uuid(), p_operation_id, p_storage_key, 'account_erasure', 'pending')
            ON CONFLICT (operation_id, storage_key) DO NOTHING;
        END;
        $$;
        REVOKE ALL ON FUNCTION public.enqueue_account_erasure_storage_task(uuid, text) FROM PUBLIC;
    """)

    op.execute("""
        DROP INDEX IF EXISTS public.ix_knowledge_import_jobs_upload_lease_expires_at;
        DROP INDEX IF EXISTS public.ix_knowledge_import_jobs_staging_reconcile_after;
        ALTER TABLE public.knowledge_import_jobs
            DROP CONSTRAINT IF EXISTS ck_knowledge_import_jobs_staging_reconcile_count,
            DROP CONSTRAINT IF EXISTS uq_knowledge_import_jobs_staging_storage_key,
            DROP COLUMN staging_storage_key,
            DROP COLUMN upload_lease_token,
            DROP COLUMN upload_lease_expires_at,
            DROP COLUMN staging_reconcile_count,
            DROP COLUMN staging_reconcile_after;
    """)
