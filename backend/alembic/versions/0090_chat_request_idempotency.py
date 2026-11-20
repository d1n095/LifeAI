"""R15 successor: durable client idempotency for POST /api/chat (migration 0090).

One owner-scoped claim per ``(owner_id, client_message_id)``. The claim is committed in the
same transaction as the user message and before any provider call, so a client retry of the
same logical send cannot create a second user message or a second billable provider call.
The unique constraint makes the guarantee race-safe; the chat route catches the loser's
IntegrityError and replays the winner's existing reply (or returns 409 while it is unresolved).

RLS shape is identical to the other owner-scoped tables (migration 0076/0089): ENABLE + FORCE,
policy on ``app.current_user_id``. No application DELETE path exists; rows disappear through
the owner/conversation/message ON DELETE CASCADE, not through a request path.
"""

from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE chat_request_claims (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            client_message_id varchar(128) NOT NULL,
            conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            created_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_chat_request_claims_owner_client UNIQUE (owner_id, client_message_id),
            CONSTRAINT ck_chat_request_claims_client_message_id CHECK (length(btrim(client_message_id)) > 0)
        );
        CREATE INDEX ix_chat_request_claims_owner_created ON chat_request_claims(owner_id, created_at);
        CREATE INDEX ix_chat_request_claims_conversation ON chat_request_claims(conversation_id);

        ALTER TABLE chat_request_claims ENABLE ROW LEVEL SECURITY;
        ALTER TABLE chat_request_claims FORCE ROW LEVEL SECURITY;
        CREATE POLICY chat_request_claims_isolation ON chat_request_claims
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        GRANT SELECT, INSERT ON chat_request_claims TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS chat_request_claims_isolation ON chat_request_claims;
        DROP TABLE IF EXISTS chat_request_claims;
        """
    )
