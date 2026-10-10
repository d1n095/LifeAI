"""One active founder-conversation decision per owner/conversation/key.

Revision ID: 0092_cc_one_active_decision
Revises: 0091_cc_p1_authority
Create Date: 2026-10-10

Isolated continuous-conversation child of `0091_cc_p1_authority`. Sibling Founder
Sovereignty remains `0090_founder_sovereignty` → `0091_fs_p1_authority`. Do not compose,
rebase, or renumber these heads until an integration order is approved. Alembic
`version_num` is varchar(32).
"""

from alembic import op

revision = "0092_cc_one_active_decision"
down_revision = "0091_cc_p1_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE public.founder_conversation_decisions AS d
        SET status = 'historical',
            superseded = true
        WHERE d.status = 'active'
          AND d.decision_key <> ''
          AND d.id NOT IN (
              SELECT kept.id
              FROM (
                  SELECT DISTINCT ON (owner_id, conversation_id, decision_key) id
                  FROM public.founder_conversation_decisions
                  WHERE status = 'active' AND decision_key <> ''
                  ORDER BY owner_id, conversation_id, decision_key, effective_at DESC, created_at DESC
              ) AS kept
          );

        CREATE UNIQUE INDEX IF NOT EXISTS uq_founder_conversation_one_active_decision
            ON public.founder_conversation_decisions (owner_id, conversation_id, decision_key)
            WHERE status = 'active' AND decision_key <> '';

        CREATE OR REPLACE FUNCTION record_founder_conversation_decision(
            p_owner_id uuid,
            p_conversation_id uuid,
            p_message_id uuid,
            p_decision_key varchar,
            p_statement text,
            p_value text,
            p_identifiers jsonb,
            p_effective_at timestamptz
        ) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog AS $$
        DECLARE
            v_id uuid;
            v_old uuid;
        BEGIN
            IF p_decision_key IS NULL OR btrim(p_decision_key) = '' THEN
                RAISE EXCEPTION 'decision_key is required';
            END IF;
            LOOP
                SELECT id INTO v_id
                FROM public.founder_conversation_decisions
                WHERE owner_id = p_owner_id
                  AND conversation_id = p_conversation_id
                  AND message_id = p_message_id
                  AND decision_key = p_decision_key
                LIMIT 1;
                IF v_id IS NOT NULL THEN
                    RETURN v_id;
                END IF;

                SELECT id INTO v_old
                FROM public.founder_conversation_decisions
                WHERE owner_id = p_owner_id
                  AND conversation_id = p_conversation_id
                  AND decision_key = p_decision_key
                  AND status = 'active'
                FOR UPDATE;

                v_id := gen_random_uuid();
                IF v_old IS NOT NULL THEN
                    UPDATE public.founder_conversation_decisions
                    SET status = 'historical',
                        superseded = true
                    WHERE id = v_old;
                END IF;

                BEGIN
                    INSERT INTO public.founder_conversation_decisions (
                        id, owner_id, conversation_id, message_id, topic, decision_key,
                        statement, value, identifiers, superseded, superseded_by, status,
                        effective_at, source_turn_id, created_at
                    ) VALUES (
                        v_id, p_owner_id, p_conversation_id, p_message_id, p_decision_key,
                        p_decision_key, p_statement, p_value, COALESCE(p_identifiers, '{}'::jsonb),
                        false, NULL, 'active', COALESCE(p_effective_at, now()), p_message_id, now()
                    );
                    IF v_old IS NOT NULL THEN
                        UPDATE public.founder_conversation_decisions
                        SET superseded_by = v_id
                        WHERE id = v_old;
                    END IF;
                    RETURN v_id;
                EXCEPTION WHEN unique_violation THEN
                    v_id := NULL;
                    v_old := NULL;
                END;
            END LOOP;
        END;
        $$;

        REVOKE ALL ON FUNCTION record_founder_conversation_decision(
            uuid, uuid, uuid, varchar, text, text, jsonb, timestamptz
        ) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION record_founder_conversation_decision(
            uuid, uuid, uuid, varchar, text, text, jsonb, timestamptz
        ) TO mainai_app;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP FUNCTION IF EXISTS record_founder_conversation_decision(
            uuid, uuid, uuid, varchar, text, text, jsonb, timestamptz
        );
        DROP INDEX IF EXISTS uq_founder_conversation_one_active_decision;
        """
    )
