from sqlalchemy import text
from sqlalchemy.engine import Engine

RLS_STATEMENTS = [
    "ALTER TABLE conversations ENABLE ROW LEVEL SECURITY",
    # FORCE is required because the app connects as the table owner (created the tables via
    # SQLAlchemy) — Postgres exempts owners from RLS by default unless FORCE is set.
    "ALTER TABLE conversations FORCE ROW LEVEL SECURITY",
    "ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY",
    # Founder Knowledge Studio v1 (migration 0006) — documents moved from "shared company
    # knowledge" to owner-scoped once Knowledge Studio made per-document access control a
    # real product requirement (see app/models/document.py's docstring update).
    "ALTER TABLE documents ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE documents FORCE ROW LEVEL SECURITY",
    "ALTER TABLE knowledge_versions ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE knowledge_versions FORCE ROW LEVEL SECURITY",
    "ALTER TABLE knowledge_import_jobs ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE knowledge_import_jobs FORCE ROW LEVEL SECURITY",
    "ALTER TABLE source_relationships ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE source_relationships FORCE ROW LEVEL SECURITY",
    # Claim-level trust (migration 0007, STEG 10) — see app/models/knowledge_claim.py.
    "ALTER TABLE knowledge_claims ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE knowledge_claims FORCE ROW LEVEL SECURITY",
    "ALTER TABLE claim_relationships ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE claim_relationships FORCE ROW LEVEL SECURITY",
    # Audio/video import v1 (migration 0009, STEG 12) — see app/models/media_url_import.py.
    "ALTER TABLE media_url_imports ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE media_url_imports FORCE ROW LEVEL SECURITY",
]

# One policy per table: rows are only visible/writable when they belong to the user bound
# to the current request (set via `SET LOCAL app.current_user_id` in app/deps.py). Missing
# `app.current_user_id` (e.g. a raw admin/migration connection) resolves to NULL, which never
# matches — default-deny, not default-allow.
# NULLIF guards against a Postgres quirk: a custom GUC that was SET LOCAL and then reverted
# (e.g. after a mid-request commit, before the next transaction's after_begin re-applies it)
# reads back as '' rather than NULL. Casting '' straight to ::uuid would raise a DB error
# instead of just correctly matching no rows.
POLICY_DEFINITIONS = [
    {
        "table": "conversations",
        "name": "conversations_isolation",
        "expr": "user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "document_chunks",
        "name": "document_chunks_isolation",
        # Deliberately narrower than Document itself (shared company knowledge, see the
        # docstring below) — this is a pgvector-migration-specific requirement: chunk/
        # embedding rows are strictly per-owner, so two users' uploaded material is never
        # readable or searchable by each other even though the Document row describing it
        # is still shared metadata. See app/rag/vector_store.py and app/rag/ingest.py.
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "documents",
        "name": "documents_isolation",
        "expr": "uploaded_by = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "knowledge_versions",
        "name": "knowledge_versions_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "knowledge_import_jobs",
        "name": "knowledge_import_jobs_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "source_relationships",
        "name": "source_relationships_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "knowledge_claims",
        "name": "knowledge_claims_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "claim_relationships",
        "name": "claim_relationships_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "media_url_imports",
        "name": "media_url_imports_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
]


def apply_rls(engine: Engine) -> None:
    """Idempotently enable Postgres Row-Level Security on user-owned tables.

    Conversations, document_chunks, documents, knowledge_versions, knowledge_import_jobs,
    source_relationships, knowledge_claims, claim_relationships and media_url_imports all
    have strict per-user isolation (see migrations 0006/0007/0009 —
    docs/FOUNDER_KNOWLEDGE_STUDIO_V1.md). Projects/tasks
    remain intentionally shared company knowledge (see docs/MAINAI_0.1_PLAN.md) and only track
    `created_by` for attribution, not access control — that distinction predates Founder
    Knowledge Studio and wasn't part of tonight's change.
    """
    with engine.begin() as conn:
        for statement in RLS_STATEMENTS:
            conn.execute(text(statement))

        for policy in POLICY_DEFINITIONS:
            exists = conn.execute(
                text("SELECT 1 FROM pg_policies WHERE tablename = :t AND policyname = :n"),
                {"t": policy["table"], "n": policy["name"]},
            ).first()
            if exists:
                continue
            conn.execute(
                text(
                    f"CREATE POLICY {policy['name']} ON {policy['table']} "
                    f"USING ({policy['expr']}) WITH CHECK ({policy['expr']})"
                )
            )
