"""Serialize canonical assignment creation at the database boundary.

Revision ID: 0101_coordination_fencing
Revises: 0100_rt3_cycle_webhooks
"""

from alembic import op

revision = "0101_coordination_fencing"
down_revision = "0100_rt3_cycle_webhooks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE FUNCTION enforce_agent_work_assignment_canonical_exclusivity() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            -- Every contender, including an explicit parallel-exploration assignment, takes
            -- the same transaction-scoped lock. This prevents an ungrouped check from racing
            -- an uncommitted insert under READ COMMITTED.
            PERFORM pg_catalog.pg_advisory_xact_lock(
                pg_catalog.hashtextextended(NEW.owner_id::text || ':' || NEW.canonical_work_key, 101)
            );
            IF NEW.parallel_exploration_group_id IS NULL
               AND NEW.status NOT IN ('failed', 'completed', 'cancelled', 'superseded')
               AND EXISTS (
                    SELECT 1
                    FROM public.agent_work_assignments AS existing
                    WHERE existing.owner_id = NEW.owner_id
                      AND existing.canonical_work_key = NEW.canonical_work_key
                      AND existing.id <> NEW.id
                      AND existing.status NOT IN ('failed', 'completed', 'cancelled', 'superseded')
               )
            THEN
                RAISE EXCEPTION 'active canonical work assignment already exists for owner/key'
                    USING ERRCODE = 'unique_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        REVOKE ALL ON FUNCTION enforce_agent_work_assignment_canonical_exclusivity() FROM PUBLIC;

        CREATE TRIGGER trg_agent_work_assignment_canonical_exclusivity
            BEFORE INSERT OR UPDATE OF owner_id, canonical_work_key, status, parallel_exploration_group_id
            ON agent_work_assignments
            FOR EACH ROW EXECUTE FUNCTION enforce_agent_work_assignment_canonical_exclusivity();

        CREATE FUNCTION enforce_agent_work_assignment_status_transition() RETURNS trigger
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NEW.status = OLD.status THEN
                RETURN NEW;
            END IF;
            IF NOT (
                (OLD.status = 'planned' AND NEW.status IN ('ready', 'waiting_dependency', 'cancelled')) OR
                (OLD.status = 'waiting_dependency' AND NEW.status IN ('ready', 'blocked', 'cancelled')) OR
                (OLD.status = 'ready' AND NEW.status IN ('running', 'waiting_agent', 'blocked', 'cancelled')) OR
                (OLD.status = 'waiting_agent' AND NEW.status IN ('running', 'blocked', 'cancelled')) OR
                (OLD.status = 'running' AND NEW.status IN ('waiting_review', 'ready_for_review', 'blocked', 'failed', 'completed', 'cancelled')) OR
                (OLD.status = 'waiting_review' AND NEW.status IN ('reviewing', 'cancelled')) OR
                (OLD.status = 'ready_for_review' AND NEW.status IN ('reviewing', 'cancelled')) OR
                (OLD.status = 'reviewing' AND NEW.status IN ('changes_requested', 'verified', 'completed', 'cancelled')) OR
                (OLD.status = 'changes_requested' AND NEW.status IN ('running', 'ready', 'cancelled')) OR
                (OLD.status = 'verified' AND NEW.status IN ('completed', 'cancelled')) OR
                (OLD.status = 'blocked' AND NEW.status IN ('ready', 'running', 'waiting_dependency', 'failed', 'cancelled')) OR
                (OLD.status = 'failed' AND NEW.status IN ('ready', 'superseded')) OR
                (OLD.status = 'completed' AND NEW.status = 'superseded')
            ) THEN
                RAISE EXCEPTION 'invalid work assignment status transition: % -> %', OLD.status, NEW.status
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$;
        REVOKE ALL ON FUNCTION enforce_agent_work_assignment_status_transition() FROM PUBLIC;

        CREATE TRIGGER trg_agent_work_assignment_status_transition
            BEFORE UPDATE OF status ON agent_work_assignments
            FOR EACH ROW EXECUTE FUNCTION enforce_agent_work_assignment_status_transition();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER trg_agent_work_assignment_status_transition ON agent_work_assignments;
        DROP FUNCTION enforce_agent_work_assignment_status_transition();
        DROP TRIGGER trg_agent_work_assignment_canonical_exclusivity ON agent_work_assignments;
        DROP FUNCTION enforce_agent_work_assignment_canonical_exclusivity();
        """
    )
