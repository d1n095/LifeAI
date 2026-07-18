from sqlalchemy import text
from sqlalchemy.engine import Engine

RLS_STATEMENTS = [
    "ALTER TABLE conversations ENABLE ROW LEVEL SECURITY",
    # FORCE is required because the app connects as the table owner (created the tables via
    # SQLAlchemy) — Postgres exempts owners from RLS by default unless FORCE is set.
    "ALTER TABLE conversations FORCE ROW LEVEL SECURITY",
    "ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY",
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
]


def apply_rls(engine: Engine) -> None:
    """Idempotently enable Postgres Row-Level Security on user-owned tables.

    Conversations and document_chunks have strict per-user isolation. Documents/projects/
    tasks themselves are still intentionally shared company knowledge (see
    docs/MAINAI_0.1_PLAN.md) and only track `created_by` for attribution, not access control
    — document_chunks (the pgvector-backed embedded text actually used for search) is a
    deliberate exception to that, not a contradiction of it: see
    app/models/document_chunk.py's docstring.
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
