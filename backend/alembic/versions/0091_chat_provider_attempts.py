"""R15 successor: durable billable chat-attempt identity (migration 0091).

A chat provider call can time out or crash after the budget hold was reserved but before the
outcome is known. The in-memory Reservation object is then gone, so nothing can ever close the
hold. This table stores exactly the durable facts needed to reconcile it later:

* the exact ComputeBudgetStore reservation identities (JSON, no prompt/reply text),
* the provider/model and the invocation boundary (``reserved`` = never invoked),
* the safe evidence needed to settle from usage or conservatively.

Owner-scoped FORCE RLS, identical policy shape to chat_request_claims (migration 0090).
The runtime role gets SELECT/INSERT/UPDATE — no DELETE path exists; rows disappear only via
the owner/conversation/message cascades. Recovery never invokes a provider and never releases
an ambiguous hold without evidence (app/billing/chat_attempts.py).
"""

from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE chat_provider_attempts (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            assistant_message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            source_ref varchar(256) NOT NULL,
            provider varchar(64) NOT NULL,
            model varchar(128) NOT NULL,
            state varchar(24) NOT NULL,
            effect_boundary varchar(16),
            reservations jsonb NOT NULL DEFAULT '[]'::jsonb,
            evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
            reserved_tokens integer NOT NULL DEFAULT 0,
            reserved_cost_micros bigint NOT NULL DEFAULT 0,
            created_at timestamp NOT NULL DEFAULT now(),
            updated_at timestamp NOT NULL DEFAULT now(),
            CONSTRAINT uq_chat_provider_attempts_source_ref UNIQUE (source_ref),
            CONSTRAINT ck_chat_provider_attempts_state CHECK (
                state IN ('reserved','invoking','held','settled','released',
                          'settlement_pending','release_pending')
            ),
            CONSTRAINT ck_chat_provider_attempts_boundary CHECK (
                effect_boundary IS NULL OR effect_boundary IN ('pre_egress','ambiguous','observed')
            ),
            CONSTRAINT ck_chat_provider_attempts_reservations CHECK (
                jsonb_typeof(reservations) = 'array'
            ),
            CONSTRAINT ck_chat_provider_attempts_evidence CHECK (
                jsonb_typeof(evidence) = 'object'
            )
        );
        CREATE INDEX ix_chat_provider_attempts_owner_state
            ON chat_provider_attempts(owner_id, state, updated_at);
        CREATE INDEX ix_chat_provider_attempts_assistant
            ON chat_provider_attempts(assistant_message_id);

        ALTER TABLE chat_provider_attempts ENABLE ROW LEVEL SECURITY;
        ALTER TABLE chat_provider_attempts FORCE ROW LEVEL SECURITY;
        CREATE POLICY chat_provider_attempts_isolation ON chat_provider_attempts
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        GRANT SELECT, INSERT, UPDATE ON chat_provider_attempts TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP POLICY IF EXISTS chat_provider_attempts_isolation ON chat_provider_attempts;
        DROP TABLE IF EXISTS chat_provider_attempts;
        """
    )
