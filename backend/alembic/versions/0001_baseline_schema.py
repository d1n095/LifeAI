"""Baseline schema — MainAI 0.1 M1-M3 (auth/RLS, provider/usage, chat/projects/documents).

Reconstructed from the exact DDL `Base.metadata.create_all()` previously produced (verified
via `pg_dump --schema-only` against a live database built from these models), not
hand-transcribed from the ORM definitions — this is the one-time bridge from
`create_all`-managed schema to Alembic-managed schema. Every migration after this one is a
normal incremental change.

Revision ID: 0001
Revises:
Create Date: 2026-07-18
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TYPE documentsource AS ENUM ('upload', 'website', 'code', 'manual');
        CREATE TYPE indexstatus AS ENUM ('pending', 'indexing', 'indexed', 'failed');
        CREATE TYPE messagerole AS ENUM ('user', 'assistant', 'system');
        CREATE TYPE projectstatus AS ENUM ('idea', 'active', 'paused', 'done');
        CREATE TYPE taskpriority AS ENUM ('low', 'medium', 'high', 'critical');
        CREATE TYPE taskstatus AS ENUM ('todo', 'in_progress', 'done');
        CREATE TYPE userrole AS ENUM ('admin', 'member');
    """)

    op.execute("""
        CREATE TABLE users (
            id uuid NOT NULL PRIMARY KEY,
            email character varying(320) NOT NULL,
            password_hash character varying(256) NOT NULL,
            role userrole NOT NULL,
            is_active boolean NOT NULL,
            created_at timestamp without time zone NOT NULL
        );
        CREATE UNIQUE INDEX ix_users_email ON users USING btree (email);
    """)

    op.execute("""
        CREATE TABLE conversations (
            id uuid NOT NULL PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id),
            title character varying(256) NOT NULL,
            created_at timestamp without time zone NOT NULL,
            updated_at timestamp without time zone NOT NULL
        );
    """)

    op.execute("""
        CREATE TABLE messages (
            id uuid NOT NULL PRIMARY KEY,
            conversation_id uuid NOT NULL REFERENCES conversations(id),
            role messagerole NOT NULL,
            content text NOT NULL,
            provider character varying(64),
            model character varying(128),
            source_document_ids text,
            created_at timestamp without time zone NOT NULL
        );
    """)

    op.execute("""
        CREATE TABLE documents (
            id uuid NOT NULL PRIMARY KEY,
            uploaded_by uuid REFERENCES users(id),
            title character varying(512) NOT NULL,
            source documentsource NOT NULL,
            source_url character varying(1024),
            category character varying(128),
            file_path character varying(1024),
            content_preview text,
            status indexstatus NOT NULL,
            chunk_count integer NOT NULL,
            error_message text,
            created_at timestamp without time zone NOT NULL,
            updated_at timestamp without time zone NOT NULL
        );
    """)

    op.execute("""
        CREATE TABLE projects (
            id uuid NOT NULL PRIMARY KEY,
            created_by uuid REFERENCES users(id),
            name character varying(256) NOT NULL,
            description text,
            status projectstatus NOT NULL,
            created_at timestamp without time zone NOT NULL,
            updated_at timestamp without time zone NOT NULL
        );
    """)

    op.execute("""
        CREATE TABLE tasks (
            id uuid NOT NULL PRIMARY KEY,
            project_id uuid REFERENCES projects(id),
            created_by uuid REFERENCES users(id),
            title character varying(512) NOT NULL,
            description text,
            status taskstatus NOT NULL,
            priority taskpriority NOT NULL,
            suggested_by_ai boolean NOT NULL,
            created_at timestamp without time zone NOT NULL,
            updated_at timestamp without time zone NOT NULL
        );
    """)

    op.execute("""
        CREATE TABLE provider_config (
            id uuid NOT NULL PRIMARY KEY,
            role character varying(32) NOT NULL UNIQUE,
            provider character varying(32) NOT NULL,
            model character varying(128) NOT NULL,
            is_active boolean NOT NULL,
            updated_at timestamp without time zone NOT NULL
        );
    """)

    op.execute("""
        CREATE TABLE company_info (
            id uuid NOT NULL PRIMARY KEY,
            key character varying(128) NOT NULL,
            label character varying(256) NOT NULL,
            content text NOT NULL,
            updated_at timestamp without time zone NOT NULL
        );
        CREATE UNIQUE INDEX ix_company_info_key ON company_info USING btree (key);
    """)

    op.execute("""
        CREATE TABLE usage_log (
            id uuid NOT NULL PRIMARY KEY,
            user_id uuid REFERENCES users(id) ON DELETE SET NULL,
            conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
            role character varying(32) NOT NULL,
            provider character varying(32) NOT NULL,
            model character varying(128) NOT NULL,
            prompt_tokens integer NOT NULL,
            completion_tokens integer NOT NULL,
            cost_usd numeric(14,6),
            created_at timestamp without time zone NOT NULL
        );
    """)

    op.execute("""
        CREATE TABLE audit_log (
            id uuid NOT NULL PRIMARY KEY,
            user_id uuid,
            action character varying(128) NOT NULL,
            entity_type character varying(64),
            entity_id character varying(64),
            ip_address character varying(64),
            detail text,
            created_at timestamp without time zone NOT NULL
        );
    """)

    # Row-Level Security: conversations are strictly isolated per user at the database
    # layer, not just in application code. FORCE is required because the app connects as
    # the table owner — Postgres exempts owners from RLS by default unless FORCE is set.
    # See backend/app/rls.py (still called at startup too, as an idempotent safety net) and
    # docs/MAINAI_0.1_PLAN.md.
    op.execute("""
        ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
        ALTER TABLE conversations FORCE ROW LEVEL SECURITY;
        CREATE POLICY conversations_isolation ON conversations
            USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE audit_log;
        DROP TABLE usage_log;
        DROP TABLE company_info;
        DROP TABLE provider_config;
        DROP TABLE tasks;
        DROP TABLE projects;
        DROP TABLE documents;
        DROP TABLE messages;
        DROP TABLE conversations;
        DROP TABLE users;

        DROP TYPE userrole;
        DROP TYPE taskstatus;
        DROP TYPE taskpriority;
        DROP TYPE projectstatus;
        DROP TYPE messagerole;
        DROP TYPE indexstatus;
        DROP TYPE documentsource;
    """)
