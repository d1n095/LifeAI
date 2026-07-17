from sqlalchemy import text
from sqlalchemy.engine import Engine

RLS_STATEMENTS = [
    "ALTER TABLE conversations ENABLE ROW LEVEL SECURITY",
    # FORCE is required because the app connects as the table owner (created the tables via
    # SQLAlchemy) — Postgres exempts owners from RLS by default unless FORCE is set.
    "ALTER TABLE conversations FORCE ROW LEVEL SECURITY",
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
]


def apply_rls(engine: Engine) -> None:
    """Idempotently enable Postgres Row-Level Security on user-owned tables.

    Conversations are the only table with strict per-user isolation in MainAI 0.1 —
    documents/projects/tasks are intentionally shared company knowledge (see
    docs/MAINAI_0.1_PLAN.md) and only track `created_by` for attribution, not access control.
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
