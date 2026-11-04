"""Bind supervision to canonical jobs, workforce grants and immutable evidence.

No job lifecycle is stored here. mainai_jobs owns currentness/leases. These are
execution contracts, observations, receipts and delivery bookkeeping.
"""
from alembic import op

revision = '0076_supervision_fences'
down_revision = '0075_supervision_base'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
    ALTER TABLE mainai_supervision_agents DROP CONSTRAINT mainai_supervision_agents_pkey;
    ALTER TABLE mainai_supervision_agents ADD PRIMARY KEY(owner_id, agent_id);
    ALTER TABLE mainai_supervision_agents ADD COLUMN observation_seq bigint NOT NULL DEFAULT 0;
    ALTER TABLE mainai_supervision_agents ADD COLUMN last_progress_at timestamptz;
    ALTER TABLE mainai_supervision_agents ADD COLUMN session_state varchar(24) NOT NULL DEFAULT 'unknown';
    ALTER TABLE mainai_supervision_agents ADD COLUMN misses integer NOT NULL DEFAULT 0;
    ALTER TABLE mainai_supervision_agents ADD COLUMN last_health_at timestamptz;
    ALTER TABLE mainai_supervision_messages ADD COLUMN agent_id varchar(128);
    ALTER TABLE mainai_supervision_messages ADD COLUMN lease_generation integer;
    ALTER TABLE mainai_supervision_messages ADD COLUMN delivery_token uuid;
    ALTER TABLE mainai_supervision_messages ADD COLUMN delivery_until timestamptz;
    ALTER TABLE mainai_supervision_messages ADD COLUMN facts jsonb NOT NULL DEFAULT '{}';
    ALTER TABLE mainai_supervision_messages ADD CONSTRAINT fk_supervision_message_owner
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE;
    ALTER TABLE mainai_supervision_agents ADD CONSTRAINT fk_supervision_agent_job_owner
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE;
    ALTER TABLE mainai_budget_reservations ADD CONSTRAINT fk_supervision_budget_job_owner
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE;

    CREATE TABLE mainai_supervision_bindings (
      job_id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      program varchar(128) NOT NULL, assignment_id uuid NOT NULL,
      fallback_assignments jsonb NOT NULL DEFAULT '[]',
      worktree text NOT NULL, branch varchar(256) NOT NULL, base_sha varchar(40) NOT NULL,
      required jsonb NOT NULL, criteria jsonb NOT NULL, remaining jsonb NOT NULL,
      portable boolean NOT NULL DEFAULT false, review_required boolean NOT NULL DEFAULT true,
      priority integer NOT NULL DEFAULT 0, max_continuations integer NOT NULL DEFAULT 8,
      return_count integer NOT NULL DEFAULT 0, fix_depth integer NOT NULL DEFAULT 0,
      parent_job_id uuid, cooldown_until timestamptz,
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE,
      FOREIGN KEY(assignment_id,owner_id) REFERENCES workforce_assignments(id,owner_id),
      FOREIGN KEY(parent_job_id,owner_id) REFERENCES mainai_jobs(id,owner_id),
      CHECK(jsonb_array_length(criteria) BETWEEN 1 AND 64),
      CHECK(max_continuations BETWEEN 1 AND 32), CHECK(fix_depth BETWEEN 0 AND 3)
    );
    CREATE INDEX ix_supervision_bindings_owner_program ON mainai_supervision_bindings(owner_id,program);
    CREATE TABLE mainai_supervision_attempts (
      id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      job_id uuid NOT NULL, assignment_id uuid NOT NULL, agent_id varchar(128) NOT NULL,
      process_nonce varchar(128) NOT NULL, generation integer NOT NULL,
      owner_epoch integer NOT NULL, global_epoch integer NOT NULL,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      UNIQUE(job_id,generation), UNIQUE(id,owner_id),
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE,
      FOREIGN KEY(assignment_id,owner_id) REFERENCES workforce_assignments(id,owner_id)
    );
    CREATE TABLE mainai_supervision_providers (
      profile_id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      availability varchar(24) NOT NULL, session_nonce varchar(128) NOT NULL,
      observed_at timestamptz NOT NULL, valid_until timestamptz NOT NULL,
      FOREIGN KEY(profile_id,owner_id) REFERENCES workforce_agent_profiles(id,owner_id) ON DELETE CASCADE,
      CHECK(availability IN ('available','unavailable','exhausted','quarantined','rate_limited'))
    );
    CREATE TABLE mainai_supervision_dependencies (
      owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      child_id uuid NOT NULL, parent_id uuid NOT NULL,
      required_sha varchar(40) NOT NULL, certified boolean NOT NULL DEFAULT true,
      PRIMARY KEY(child_id,parent_id), CHECK(child_id <> parent_id),
      FOREIGN KEY(child_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE,
      FOREIGN KEY(parent_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE
    );
    CREATE TABLE mainai_supervision_journal (
      id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      job_id uuid NOT NULL, attempt_id uuid, kind varchar(48) NOT NULL,
      facts jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE,
      CHECK(pg_column_size(facts) <= 8192)
    );
    CREATE INDEX ix_supervision_journal_owner_time ON mainai_supervision_journal(owner_id,created_at);
    CREATE TABLE mainai_supervision_evidence (
      id uuid PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      job_id uuid NOT NULL, attempt_id uuid NOT NULL, kind varchar(24) NOT NULL,
      sha varchar(40) NOT NULL, source varchar(128) NOT NULL,
      facts jsonb NOT NULL, observed_at timestamptz NOT NULL, valid_until timestamptz NOT NULL,
      UNIQUE(id,owner_id), FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE,
      FOREIGN KEY(attempt_id,owner_id) REFERENCES mainai_supervision_attempts(id,owner_id),
      CHECK(kind IN ('tests','artifact','review','result','usage')),
      CHECK(pg_column_size(facts) <= 8192)
    );
    CREATE INDEX ix_supervision_evidence_job_kind ON mainai_supervision_evidence(owner_id,job_id,kind);
    CREATE TABLE mainai_supervision_resources (
      resource_key varchar(128) PRIMARY KEY, owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      job_id uuid NOT NULL, attempt_id uuid NOT NULL,
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE,
      FOREIGN KEY(attempt_id,owner_id) REFERENCES mainai_supervision_attempts(id,owner_id)
    );
    CREATE TABLE mainai_supervision_spend_links (
      usage_id uuid PRIMARY KEY REFERENCES provider_spend_usage_events(id),
      owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      job_id uuid NOT NULL, attempt_id uuid NOT NULL, expires_at timestamptz NOT NULL,
      phase varchar(16) NOT NULL DEFAULT 'reserved',
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE,
      FOREIGN KEY(attempt_id,owner_id) REFERENCES mainai_supervision_attempts(id,owner_id),
      CHECK(phase IN ('reserved','dispatched','settled','released','uncertain'))
    );
    CREATE TABLE mainai_supervision_receipts (
      message_id uuid PRIMARY KEY REFERENCES mainai_supervision_messages(id) ON DELETE CASCADE,
      owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      job_id uuid NOT NULL, attempt_id uuid NOT NULL, received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      FOREIGN KEY(job_id,owner_id) REFERENCES mainai_jobs(id,owner_id) ON DELETE CASCADE
    );
    """)
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"""CREATE POLICY {table}_owner ON {table}
          USING(owner_id = NULLIF(current_setting('app.current_user_id', true),'')::uuid)
          WITH CHECK(owner_id = NULLIF(current_setting('app.current_user_id', true),'')::uuid)""")
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON {table} TO mainai_app")
    op.execute("""
    CREATE FUNCTION supervision_immutable_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'immutable supervision receipt'; END $$;
    """)
    for table in ('mainai_supervision_attempts','mainai_supervision_journal','mainai_supervision_evidence'):
        op.execute(f"CREATE TRIGGER immutable_receipt BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION supervision_immutable_receipt()")
        op.execute(f"REVOKE UPDATE,DELETE ON {table} FROM mainai_app")


TABLES = ('mainai_supervision_bindings','mainai_supervision_attempts','mainai_supervision_providers',
          'mainai_supervision_dependencies','mainai_supervision_journal','mainai_supervision_evidence',
          'mainai_supervision_resources','mainai_supervision_spend_links','mainai_supervision_receipts')


def downgrade():
    for table in reversed(TABLES):
        op.execute(f'DROP TABLE {table}')
    op.execute('DROP FUNCTION supervision_immutable_receipt()')
    for table in ('mainai_supervision_agents','mainai_supervision_messages','mainai_budget_reservations'):
        name = {'mainai_supervision_agents':'agent_job','mainai_supervision_messages':'message','mainai_budget_reservations':'budget_job'}[table]
        op.execute(f'ALTER TABLE {table} DROP CONSTRAINT fk_supervision_{name}_owner')
    for col in ('observation_seq','last_progress_at','session_state','misses','last_health_at'):
        op.execute(f'ALTER TABLE mainai_supervision_agents DROP COLUMN {col}')
    for col in ('agent_id','lease_generation','delivery_token','delivery_until','facts'):
        op.execute(f'ALTER TABLE mainai_supervision_messages DROP COLUMN {col}')
    # Refuse lossy downgrade if two owners share an agent name.
    op.execute('ALTER TABLE mainai_supervision_agents DROP CONSTRAINT mainai_supervision_agents_pkey')
    op.execute('ALTER TABLE mainai_supervision_agents ADD PRIMARY KEY(agent_id)')
