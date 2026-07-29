"""S1A (docs/MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8): the universal provenance core —
`memory_source_units` (the atomic, immutable source unit every KnowledgeClaim will eventually
point to via memory_source_id, replacing the plan to keep adding a new nullable source_X_id
column per future source type), its document-only subtype `document_source_units`, and an
append-only `memory_source_lifecycle_events` audit trail. Additive only: `KnowledgeClaim.
memory_source_id` is nullable, the old source_id/version_id/chunk_id columns are untouched
(see §4.8's six-phase cutover plan — this migration is only phase 1).

Scope is deliberately narrow (S1A only, per §4.8's S1A/S1B/S1C split): no message tables, no
knowledge_claim_evidence (no writer until S1C/P4). Backfill of existing document claims,
dual-write in app/rag/claims.py, the shared purge_source() service, and account export/
erasure integration are separate commits/PRs on top of this one, not part of this file.

Key design decisions this migration encodes (see §4.8 for the full reasoning):
- source_kind lives on document_source_units itself, structurally tied to its parent via a
  composite FK (memory_source_id, owner_id, source_kind) — otherwise purging several chunks
  from the same document version (all getting chunk_id=NULL) would collide in a
  locator-only unique index, since nothing would anchor a purged row to its original type.
- source_identity_key is the stable, immutable dedup key backfill/dual-write use for a
  race-safe find-or-create (SAVEPOINT pattern in application code, not a CTE).
- Real database-enforced immutability/lifecycle: BEFORE UPDATE/DELETE triggers, a
  DEFERRABLE exact-one-subtype constraint trigger, and a lifecycle state machine
  (active -> revoked -> active (restore) -> purged, terminal) reachable only through
  transition_own_memory_source()/transition_memory_source_admin().
- Privilege boundary that survives a reboot, not a session flag: mainai_app (the app's
  actual runtime role — see backend/scripts/ensure_app_role.py) gets exactly SELECT+INSERT
  on memory_source_units/document_source_units and SELECT-only on
  memory_source_lifecycle_events (least privilege, not just "no UPDATE/DELETE" — TRUNCATE,
  REFERENCES and TRIGGER are equally unneeded and equally revoked; TRUNCATE in particular
  is NOT subject to RLS at all, so leaving it granted would be a real bypass). mainai_app
  does not own these tables, so REVOKE actually holds, unlike RLS's owner-bypass problem.
  All lifecycle/erasure writes go through SECURITY DEFINER functions owned by the
  admin/migration role, with a real owner check inside
  transition_own_memory_source()/erase_owner_memory() — required regardless of RLS, since a
  SECURITY DEFINER function's own queries are STILL subject to RLS if its owning role lacks
  BYPASSRLS/superuser (contrary to an earlier draft's incorrect assumption that
  `SET row_security = off` provided a bypass — it does not; per Postgres's own docs it only
  turns a silent RLS-filtered result into an error, it never grants access RLS would
  otherwise deny). EXECUTE on the *_admin variants is never granted to mainai_app.
  search_path is pinned to pg_catalog only (no `public`, no implicit pg_temp priority) with
  EVERY relation reference in EVERY function body — trigger functions included, not just the
  four SECURITY DEFINER ones — fully schema-qualified as `public.<table>`. This matters
  structurally, not just stylistically: Postgres always checks a session's temporary schema
  first for an unqualified relation name regardless of search_path, so mainai_app (which can
  create temp tables by default) could otherwise shadow `documents`/`knowledge_versions`/
  `document_chunks`/etc. inside these functions with its own fake temp table and fool the
  ownership-chain validation trigger.

  The two owner-scoped functions (transition_own_memory_source, erase_owner_memory) do NOT
  need BYPASSRLS on the admin role: they only ever touch the row(s) belonging to
  `current_setting('app.current_user_id')`, which is already the exact value RLS's policy
  checks — the policy naturally allows it, and the function's own explicit ownership check
  is the real, RLS-independent enforcement regardless. The two admin-only functions
  (transition_memory_source_admin, erase_owner_memory_admin) are different: by design they
  must operate on an arbitrary owner's row without already knowing which owner in advance,
  which FORCE ROW LEVEL SECURITY makes structurally impossible for a non-exempt role — these
  two GENUINELY REQUIRE the admin/migration role to have BYPASSRLS (or be superuser).
  backend/scripts/apply_runtime_privileges.py verifies this explicitly and refuses to boot
  if it's missing, rather than leaving the admin functions silently broken. See
  tests/backend/test_memory_source_units.py's dedicated non-superuser/non-BYPASSRLS-owner
  test, which proves the owner-scoped functions work correctly without it and that its
  absence is detected, not assumed.

  This migration deliberately does NOT contain literal `REVOKE ... FROM mainai_app`/`GRANT
  ... TO mainai_app` statements, even though ALTER DEFAULT PRIVILEGES means mainai_app gets
  broad access to these tables the instant they're created here — matching every earlier
  migration's convention (see 0004's comment) of never naming that role directly in a
  migration file. The reason is concrete, not just stylistic: the "Backend — Alembic
  migration check" CI job (.github/workflows/ci.yml) runs `alembic upgrade head` against a
  bare `postgres`-superuser-only database where `mainai_app` is never created — a literal
  `REVOKE ... FROM mainai_app` there fails outright with "role mainai_app does not exist"
  and breaks that job (confirmed locally, not assumed). The actual narrowing — including the
  schema-level `REVOKE CREATE ON SCHEMA public FROM PUBLIC`, which also used to live here —
  is entirely backend/scripts/s1a_privilege_policy.py, applied by both
  backend/scripts/ensure_app_role.py (immediately, in the same transaction as its own broad
  GRANT ALL, whenever the S1A objects already exist) and backend/scripts/
  apply_runtime_privileges.py (right after this migration, for the deploy that creates them
  for the first time, and idempotently on every subsequent boot). A schema-wide privilege
  change belongs in that shared, atomic, re-verified policy — not in a migration whose own
  `downgrade()` has no way to know whether it's safe to restore the schema's previous
  CREATE-grant state (see downgrade() below: this migration's reversibility window is
  deliberately narrow — see "in use" guard). Between this migration applying and the
  privilege policy running, the app itself is not yet serving requests (same boot-order gap
  ensure_app_role.py's own password rotation already tolerates), so mainai_app briefly
  holding broader-than-final privileges on these specific tables during that window is not a
  live exposure.

- `source_role` on every `document_source_units` row is enforced (via a DEFERRABLE
  constraint trigger, not just application code) to be exactly `'unknown'` on its parent
  `memory_source_units` row. Documents are never directly attributable to `founder` or any
  other authored role at ingest time — `mainai_app` has direct INSERT on both tables (see
  above), so nothing in the database itself stopped a bug or a future code path from
  inserting `source_role='founder'` for a raw document upload, which — since the field is
  immutable once set — would have been a PERMANENT, uncorrectable false authority claim. Real
  verified attribution is a separate, later, versioned concern (a dedicated attribution
  model), never a side effect of a document insert.

- Lifecycle coherence is enforced field-by-field, not just "the right timestamp is set": an
  `active` row requires ALL FOUR of revoked_at/revocation_reason/purged_at/purge_reason to be
  NULL (not just revoked_at/purged_at, which an earlier, looser version of this CHECK
  allowed — a row could otherwise sit `active` while still carrying a stale
  revocation_reason/purge_reason from nowhere, since neither transition function ever clears
  those unless actually transitioning through that state). A `purged` row's revoked_at/
  revocation_reason are allowed to be non-NULL ONLY as a pair (preserved history from an
  earlier active->revoked->purged path) or NULL as a pair (a direct active->purged path) —
  never one set without the other. `memory_source_lifecycle_events.reason` is NOT NULL: every
  transition function already refuses a NULL/empty reason before it would ever reach this
  INSERT, so the column should say so structurally, not just via application-level trust.

- `content_hash`/`content_hash_version` are computed internally (SHA-256 over the exact UTF-8
  bytes of `content_text`, version-tagged `'sha256-utf8-v1'`) by app/rag/memory_source.py,
  never accepted as a caller-supplied value — a caller declaring an unverified hash as
  `exact` would let the database assert a snapshot's integrity it never actually checked.
  `ck_msu_content_hash_format` additionally enforces the DB-level shape (64 lowercase hex
  characters) whenever a hash is present, independent of whether the caller computed it
  correctly.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- memory_source_units ------------------------------------------------------------
    op.execute("""
        CREATE TABLE memory_source_units (
            id uuid NOT NULL PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id),

            source_kind varchar(32) NOT NULL,
            source_identity_key text NOT NULL,
            source_role varchar(16) NOT NULL,

            observed_at timestamptz NOT NULL,
            occurred_at timestamptz,
            occurred_at_basis varchar(16) NOT NULL DEFAULT 'unknown',

            content_text text,
            content_hash varchar(64),
            content_hash_version varchar(32),
            snapshot_status varchar(16) NOT NULL,

            lifecycle_status varchar(16) NOT NULL DEFAULT 'active',
            revoked_at timestamptz,
            revocation_reason text,
            purged_at timestamptz,
            purge_reason text,

            project_id uuid REFERENCES projects(id),

            created_at timestamptz NOT NULL DEFAULT now(),

            CONSTRAINT uq_msu_id_owner UNIQUE (id, owner_id),
            CONSTRAINT uq_msu_id_owner_kind UNIQUE (id, owner_id, source_kind),
            CONSTRAINT uq_msu_owner_identity UNIQUE (owner_id, source_identity_key),

            CONSTRAINT ck_msu_source_kind
                CHECK (source_kind IN ('document_chunk', 'document_version', 'document_record')),
            CONSTRAINT ck_msu_source_role
                CHECK (source_role IN ('founder', 'assistant', 'external', 'system', 'unknown')),
            CONSTRAINT ck_msu_snapshot_status
                CHECK (snapshot_status IN ('exact', 'degraded', 'missing')),
            CONSTRAINT ck_msu_lifecycle_status
                CHECK (lifecycle_status IN ('active', 'revoked', 'purged')),
            CONSTRAINT ck_msu_occurred_at_basis
                CHECK (occurred_at_basis IN ('explicit', 'source_metadata', 'inferred', 'unknown')),

            CONSTRAINT ck_msu_occurred_at_coherence CHECK (
                (occurred_at IS NULL AND occurred_at_basis = 'unknown')
                OR
                (occurred_at IS NOT NULL AND occurred_at_basis IN ('explicit', 'source_metadata', 'inferred'))
            ),

            CONSTRAINT ck_msu_content_matches_snapshot CHECK (
                (lifecycle_status = 'purged' AND content_text IS NULL AND content_hash IS NULL AND content_hash_version IS NULL)
                OR
                (lifecycle_status <> 'purged' AND (
                    (snapshot_status = 'exact' AND content_text IS NOT NULL AND content_hash IS NOT NULL AND content_hash_version IS NOT NULL)
                    OR
                    (snapshot_status IN ('degraded', 'missing') AND content_text IS NULL AND content_hash IS NULL AND content_hash_version IS NULL)
                ))
            ),

            -- 64 lowercase hex characters (SHA-256) whenever a hash is present at all —
            -- independent of whether the caller that computed it (app/rag/memory_source.py)
            -- got the algorithm right; a malformed hash is a database-level, not just an
            -- application-level, rejection.
            CONSTRAINT ck_msu_content_hash_format
                CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'),

            CONSTRAINT ck_msu_lifecycle_coherence CHECK (
                (lifecycle_status = 'active'
                    AND revoked_at IS NULL AND revocation_reason IS NULL
                    AND purged_at IS NULL AND purge_reason IS NULL)
                OR
                (lifecycle_status = 'revoked'
                    AND revoked_at IS NOT NULL AND revocation_reason IS NOT NULL
                    AND purged_at IS NULL AND purge_reason IS NULL)
                OR
                (lifecycle_status = 'purged'
                    AND purged_at IS NOT NULL AND purge_reason IS NOT NULL
                    -- revoked_at/revocation_reason are preserved as a PAIR from an earlier
                    -- active->revoked->purged transition (transition functions never clear
                    -- them on purge), or absent as a pair on a direct active->purged path —
                    -- never one set without the other.
                    AND (revoked_at IS NULL) = (revocation_reason IS NULL))
            )
        );
        CREATE INDEX ix_msu_owner_lifecycle ON memory_source_units (owner_id, lifecycle_status);
    """)

    # --- document_source_units ------------------------------------------------------------
    op.execute("""
        CREATE TABLE document_source_units (
            memory_source_id uuid NOT NULL PRIMARY KEY REFERENCES memory_source_units(id),
            owner_id uuid NOT NULL,
            source_kind varchar(32) NOT NULL,

            document_id uuid NOT NULL REFERENCES documents(id),
            version_id uuid REFERENCES knowledge_versions(id),
            chunk_id uuid REFERENCES document_chunks(id) ON DELETE SET NULL,

            FOREIGN KEY (memory_source_id, owner_id, source_kind)
                REFERENCES memory_source_units (id, owner_id, source_kind)
        );

        -- Type-scoped, not locator-scoped: a purged document_chunk row (chunk_id -> NULL via
        -- the FK's ON DELETE SET NULL) must never start colliding with document_version/
        -- document_record rows just because its locator went away.
        CREATE UNIQUE INDEX uq_dsu_chunk ON document_source_units (owner_id, chunk_id)
            WHERE source_kind = 'document_chunk' AND chunk_id IS NOT NULL;
        CREATE UNIQUE INDEX uq_dsu_version ON document_source_units (owner_id, document_id, version_id)
            WHERE source_kind = 'document_version';
        CREATE UNIQUE INDEX uq_dsu_record ON document_source_units (owner_id, document_id)
            WHERE source_kind = 'document_record';

        CREATE INDEX ix_dsu_document ON document_source_units (document_id);
    """)

    # --- memory_source_lifecycle_events (append-only audit trail) ------------------------
    op.execute("""
        CREATE TABLE memory_source_lifecycle_events (
            id uuid NOT NULL PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_id uuid NOT NULL REFERENCES users(id),
            memory_source_id uuid NOT NULL,
            from_status varchar(16) NOT NULL,
            to_status varchar(16) NOT NULL,
            -- NOT NULL: every SECURITY DEFINER transition function already refuses a NULL/
            -- empty reason before it ever reaches this INSERT (see transition_own_memory_
            -- source/transition_memory_source_admin below) -- the column says so structurally.
            reason text NOT NULL,
            actor_type varchar(16) NOT NULL,
            actor_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),

            FOREIGN KEY (memory_source_id, owner_id)
                REFERENCES memory_source_units (id, owner_id) ON DELETE CASCADE,

            CONSTRAINT ck_msle_from_status CHECK (from_status IN ('active', 'revoked', 'purged')),
            CONSTRAINT ck_msle_to_status CHECK (to_status IN ('active', 'revoked', 'purged')),
            CONSTRAINT ck_msle_actor_type CHECK (actor_type IN ('founder', 'system', 'admin', 'migration'))
        );
        CREATE INDEX ix_msle_source ON memory_source_lifecycle_events (memory_source_id);
    """)

    # --- KnowledgeClaim.memory_source_id (additive, phase 1 of the six-phase cutover) ----
    # Composite FK, not a plain single-column one: FK constraint checks are enforced by
    # Postgres independent of RLS (they run as the table owner internally), so a bare
    # `memory_source_id REFERENCES memory_source_units(id)` would let a claim owned by A
    # structurally reference a memory_source_units row owned by B the instant B's id is
    # known/guessable — RLS never gets a chance to reject that link, since RLS governs what
    # rows a QUERY can see/write, not what a FK constraint is allowed to reference. Tying the
    # FK to memory_source_units' own (id, owner_id) UNIQUE constraint makes owner_id mismatch
    # a constraint violation, not just a hidden-by-RLS row.
    op.execute("""
        ALTER TABLE knowledge_claims
            ADD COLUMN memory_source_id uuid;
        ALTER TABLE knowledge_claims
            ADD CONSTRAINT fk_knowledge_claims_memory_source_owner
            FOREIGN KEY (memory_source_id, owner_id)
            REFERENCES memory_source_units (id, owner_id) ON DELETE RESTRICT;
        CREATE INDEX ix_knowledge_claims_memory_source ON knowledge_claims (memory_source_id);
    """)

    # --- RLS ---------------------------------------------------------------------------
    op.execute("""
        ALTER TABLE memory_source_units ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memory_source_units FORCE ROW LEVEL SECURITY;
        CREATE POLICY memory_source_units_isolation ON memory_source_units
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE document_source_units ENABLE ROW LEVEL SECURITY;
        ALTER TABLE document_source_units FORCE ROW LEVEL SECURITY;
        CREATE POLICY document_source_units_isolation ON document_source_units
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);

        ALTER TABLE memory_source_lifecycle_events ENABLE ROW LEVEL SECURITY;
        ALTER TABLE memory_source_lifecycle_events FORCE ROW LEVEL SECURITY;
        CREATE POLICY memory_source_lifecycle_events_isolation ON memory_source_lifecycle_events
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # --- memory_source_units triggers ---------------------------------------------------
    # Every function body below is schema-qualified (public.<table>) and pinned to
    # SET search_path = pg_catalog — a trigger function that is NOT itself SECURITY DEFINER
    # executes with the privileges AND search_path context of whoever fired the trigger
    # (mainai_app, for a normal INSERT/UPDATE). Postgres always checks a session's temporary
    # schema first for an unqualified relation name regardless of search_path, and mainai_app
    # can create temp tables by default — an unqualified `documents`/`memory_source_units`/
    # etc. reference here could otherwise be shadowed by a same-named temp table mainai_app
    # creates in its own session, silently defeating the validation these triggers exist for.
    op.execute("""
        CREATE OR REPLACE FUNCTION trg_msu_guard_update() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NEW.source_kind          IS DISTINCT FROM OLD.source_kind
            OR NEW.source_role          IS DISTINCT FROM OLD.source_role
            OR NEW.source_identity_key  IS DISTINCT FROM OLD.source_identity_key
            OR NEW.observed_at          IS DISTINCT FROM OLD.observed_at
            OR NEW.occurred_at          IS DISTINCT FROM OLD.occurred_at
            OR NEW.occurred_at_basis    IS DISTINCT FROM OLD.occurred_at_basis
            OR NEW.owner_id             IS DISTINCT FROM OLD.owner_id
            THEN
                RAISE EXCEPTION 'memory_source_units: identity fields are immutable (id=%)', OLD.id;
            END IF;

            IF (NEW.lifecycle_status, NEW.revoked_at, NEW.revocation_reason, NEW.purged_at, NEW.purge_reason,
                NEW.content_text, NEW.content_hash, NEW.snapshot_status)
               IS DISTINCT FROM
               (OLD.lifecycle_status, OLD.revoked_at, OLD.revocation_reason, OLD.purged_at, OLD.purge_reason,
                OLD.content_text, OLD.content_hash, OLD.snapshot_status)
            THEN
                IF current_setting('memory.transition_active', true) IS DISTINCT FROM 'on' THEN
                    RAISE EXCEPTION 'memory_source_units: lifecycle fields may only change via transition_own_memory_source()/transition_memory_source_admin() (id=%)', OLD.id;
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_msu_update_guard
            BEFORE UPDATE ON memory_source_units
            FOR EACH ROW EXECUTE FUNCTION trg_msu_guard_update();

        CREATE OR REPLACE FUNCTION trg_msu_forbid_delete() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF current_setting('memory.erasure_in_progress', true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'memory_source_units: row deletion is not permitted outside account erasure (id=%)', OLD.id;
            END IF;
            RETURN OLD;
        END;
        $$;

        CREATE TRIGGER trg_msu_no_delete
            BEFORE DELETE ON memory_source_units
            FOR EACH ROW EXECUTE FUNCTION trg_msu_forbid_delete();

        CREATE OR REPLACE FUNCTION trg_msu_check_subtype_exists() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM public.document_source_units WHERE memory_source_id = NEW.id) THEN
                RAISE EXCEPTION 'memory_source_units %: no matching document_source_units row', NEW.id;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE CONSTRAINT TRIGGER trg_msu_subtype_required
            AFTER INSERT OR UPDATE ON memory_source_units
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION trg_msu_check_subtype_exists();
    """)

    # --- document_source_units triggers -------------------------------------------------
    op.execute("""
        CREATE OR REPLACE FUNCTION trg_dsu_validate_fields() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_snap     varchar(16);
            v_identity text;
            v_role     varchar(16);
        BEGIN
            SELECT snapshot_status, source_identity_key, source_role INTO v_snap, v_identity, v_role
            FROM public.memory_source_units WHERE id = NEW.memory_source_id;

            -- Documents are never directly attributable to founder/assistant/external/system
            -- at ingest time -- mainai_app has direct INSERT on both tables, so nothing else
            -- stops a bug or a future code path from inserting a document source with
            -- source_role='founder', which (source_role being immutable once set) would be a
            -- PERMANENT false authority claim. Real, verified attribution is a separate,
            -- later, versioned concern, never a side effect of a document insert.
            IF v_role <> 'unknown' THEN
                RAISE EXCEPTION 'document_source_units: parent source_role must be unknown for document sources (id=%, source_role=%)', NEW.memory_source_id, v_role;
            END IF;

            IF NEW.source_kind = 'document_chunk' THEN
                IF NEW.document_id IS NULL THEN
                    RAISE EXCEPTION 'document_chunk requires document_id (id=%)', NEW.memory_source_id;
                END IF;
                IF TG_OP = 'INSERT' AND NEW.chunk_id IS NULL THEN
                    RAISE EXCEPTION 'document_chunk requires chunk_id at creation (id=%)', NEW.memory_source_id;
                END IF;
            ELSIF NEW.source_kind = 'document_version' THEN
                IF NEW.document_id IS NULL OR NEW.version_id IS NULL OR NEW.chunk_id IS NOT NULL THEN
                    RAISE EXCEPTION 'document_version requires document_id+version_id, chunk_id NULL (id=%)', NEW.memory_source_id;
                END IF;
            ELSIF NEW.source_kind = 'document_record' THEN
                IF NEW.document_id IS NULL OR NEW.version_id IS NOT NULL OR NEW.chunk_id IS NOT NULL OR v_snap = 'exact' THEN
                    RAISE EXCEPTION 'document_record requires only document_id, snapshot_status != exact (id=%)', NEW.memory_source_id;
                END IF;
            ELSE
                RAISE EXCEPTION 'unexpected source_kind % for document_source_units (id=%)', NEW.source_kind, NEW.memory_source_id;
            END IF;

            IF TG_OP = 'INSERT' THEN
                IF NEW.source_kind = 'document_chunk' AND v_identity IS DISTINCT FROM ('document_chunk:' || NEW.chunk_id::text) THEN
                    RAISE EXCEPTION 'source_identity_key does not match chunk_id (id=%)', NEW.memory_source_id;
                ELSIF NEW.source_kind = 'document_version' AND v_identity IS DISTINCT FROM ('document_version:' || NEW.version_id::text) THEN
                    RAISE EXCEPTION 'source_identity_key does not match version_id (id=%)', NEW.memory_source_id;
                ELSIF NEW.source_kind = 'document_record' AND v_identity IS DISTINCT FROM ('document_record:' || NEW.document_id::text) THEN
                    RAISE EXCEPTION 'source_identity_key does not match document_id (id=%)', NEW.memory_source_id;
                END IF;
            END IF;

            IF NOT EXISTS (SELECT 1 FROM public.documents WHERE id = NEW.document_id AND uploaded_by = NEW.owner_id) THEN
                RAISE EXCEPTION 'document_id % does not belong to owner_id %', NEW.document_id, NEW.owner_id;
            END IF;

            IF NEW.version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.knowledge_versions WHERE id = NEW.version_id AND source_id = NEW.document_id AND owner_id = NEW.owner_id
            ) THEN
                RAISE EXCEPTION 'version_id % does not belong to document_id %/owner_id %', NEW.version_id, NEW.document_id, NEW.owner_id;
            END IF;

            IF NEW.chunk_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM public.document_chunks WHERE id = NEW.chunk_id AND document_id = NEW.document_id AND owner_id = NEW.owner_id
            ) THEN
                RAISE EXCEPTION 'chunk_id % does not belong to document_id %/owner_id %', NEW.chunk_id, NEW.document_id, NEW.owner_id;
            END IF;

            RETURN NEW;
        END;
        $$;

        CREATE CONSTRAINT TRIGGER trg_dsu_validate
            AFTER INSERT OR UPDATE ON document_source_units
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION trg_dsu_validate_fields();

        CREATE OR REPLACE FUNCTION trg_dsu_guard_update() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_lifecycle varchar(16);
        BEGIN
            IF NEW.memory_source_id IS DISTINCT FROM OLD.memory_source_id
            OR NEW.owner_id         IS DISTINCT FROM OLD.owner_id
            OR NEW.source_kind      IS DISTINCT FROM OLD.source_kind
            OR NEW.document_id      IS DISTINCT FROM OLD.document_id
            OR NEW.version_id       IS DISTINCT FROM OLD.version_id
            THEN
                RAISE EXCEPTION 'document_source_units: locator fields are immutable (id=%)', OLD.memory_source_id;
            END IF;

            IF NEW.chunk_id IS DISTINCT FROM OLD.chunk_id THEN
                IF NEW.chunk_id IS NOT NULL OR OLD.chunk_id IS NULL THEN
                    RAISE EXCEPTION 'document_source_units: chunk_id can only be cleared, never reassigned (id=%)', OLD.memory_source_id;
                END IF;
                SELECT lifecycle_status INTO v_lifecycle FROM public.memory_source_units WHERE id = OLD.memory_source_id;
                IF v_lifecycle = 'active' THEN
                    RAISE EXCEPTION 'document_source_units: chunk_id cannot be cleared while parent is active (id=%)', OLD.memory_source_id;
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_dsu_update_guard
            BEFORE UPDATE ON document_source_units
            FOR EACH ROW EXECUTE FUNCTION trg_dsu_guard_update();

        CREATE OR REPLACE FUNCTION trg_dsu_forbid_delete() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF current_setting('memory.erasure_in_progress', true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'document_source_units: row deletion is not permitted outside account erasure (id=%)', OLD.memory_source_id;
            END IF;
            RETURN OLD;
        END;
        $$;

        CREATE TRIGGER trg_dsu_no_delete
            BEFORE DELETE ON document_source_units
            FOR EACH ROW EXECUTE FUNCTION trg_dsu_forbid_delete();
    """)

    # --- memory_source_lifecycle_events: append-only ------------------------------------
    # No BEFORE DELETE trigger here deliberately: mainai_app has DELETE revoked (below) and
    # the only path that can ever delete a memory_source_units row (hence CASCADE into this
    # table) is the admin-owned erase_owner_memory()/erase_owner_memory_admin() — an
    # unconditional forbid-delete trigger here would break exactly that legitimate cascade,
    # the same problem an unconditional trigger on memory_source_units/document_source_units
    # would have without their erasure_in_progress carve-out.
    op.execute("""
        CREATE OR REPLACE FUNCTION trg_msle_forbid_update() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            RAISE EXCEPTION 'memory_source_lifecycle_events is append-only (id=%)', OLD.id;
        END;
        $$;

        CREATE TRIGGER trg_msle_no_update
            BEFORE UPDATE ON memory_source_lifecycle_events
            FOR EACH ROW EXECUTE FUNCTION trg_msle_forbid_update();
    """)

    # --- SECURITY DEFINER functions ------------------------------------------------------
    # No `SET row_security = off` here (an earlier draft had it and was wrong): per
    # Postgres's own docs, row_security=off does NOT bypass RLS for a non-exempt role — it
    # only turns a would-be-filtered result into an error instead of silently returning
    # fewer rows. It never grants access RLS would otherwise deny. The two functions below
    # don't need any RLS bypass: they only ever touch the row(s) belonging to
    # current_setting('app.current_user_id'), which is exactly the value RLS's own policy
    # checks, so the policy allows it naturally — the function's explicit ownership check
    # (not RLS) is the real, independent enforcement. See transition_memory_source_admin/
    # erase_owner_memory_admin further below for the two functions that genuinely DO require
    # the admin/migration role to have BYPASSRLS, and why.
    op.execute("""
        CREATE OR REPLACE FUNCTION transition_own_memory_source(
            p_source_id UUID,
            p_target_status VARCHAR(16),
            p_reason TEXT,
            p_actor_kind VARCHAR(16)
        ) RETURNS VOID
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_owner      uuid;
            v_old_status varchar(16);
            v_caller     uuid;
        BEGIN
            v_caller := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_caller IS NULL THEN
                RAISE EXCEPTION 'transition_own_memory_source: no authenticated user context';
            END IF;

            IF p_actor_kind NOT IN ('founder', 'system') THEN
                RAISE EXCEPTION 'transition_own_memory_source: invalid actor_kind %', p_actor_kind;
            END IF;

            IF p_reason IS NULL OR btrim(p_reason) = '' THEN
                RAISE EXCEPTION 'transition_own_memory_source: reason is required';
            END IF;

            SELECT owner_id, lifecycle_status INTO v_owner, v_old_status
            FROM public.memory_source_units WHERE id = p_source_id FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'transition_own_memory_source: source % not found', p_source_id;
            END IF;

            IF v_owner IS DISTINCT FROM v_caller THEN
                RAISE EXCEPTION 'transition_own_memory_source: source % does not belong to caller', p_source_id;
            END IF;

            PERFORM set_config('memory.transition_active', 'on', true);

            IF p_target_status = 'revoked' AND v_old_status = 'active' THEN
                UPDATE public.memory_source_units
                SET lifecycle_status = 'revoked', revoked_at = now(), revocation_reason = p_reason
                WHERE id = p_source_id;
            ELSIF p_target_status = 'active' AND v_old_status = 'revoked' THEN
                UPDATE public.memory_source_units
                SET lifecycle_status = 'active', revoked_at = NULL, revocation_reason = NULL
                WHERE id = p_source_id;
            ELSIF p_target_status = 'purged' AND v_old_status IN ('active', 'revoked') THEN
                UPDATE public.memory_source_units
                SET lifecycle_status = 'purged', purged_at = now(), purge_reason = p_reason,
                    content_text = NULL, content_hash = NULL, content_hash_version = NULL
                WHERE id = p_source_id;
            ELSE
                PERFORM set_config('memory.transition_active', 'off', true);
                RAISE EXCEPTION 'transition_own_memory_source: illegal transition % -> %', v_old_status, p_target_status;
            END IF;

            PERFORM set_config('memory.transition_active', 'off', true);

            INSERT INTO public.memory_source_lifecycle_events
                (owner_id, memory_source_id, from_status, to_status, reason, actor_type, actor_id)
            VALUES (v_owner, p_source_id, v_old_status, p_target_status, p_reason, p_actor_kind, v_caller);
        END;
        $$;

        REVOKE ALL ON FUNCTION transition_own_memory_source(UUID, VARCHAR, TEXT, VARCHAR) FROM PUBLIC;

        -- No `SET row_security = off` here either — it would not do anything a non-exempt
        -- role couldn't already do without it. This function genuinely has no ownership
        -- check (by design — it's the admin/migration escape hatch), so it MUST run as a
        -- function owner that actually has BYPASSRLS (or is superuser); that is a real,
        -- external role attribute, not a per-call SET, and apply_runtime_privileges.py
        -- verifies it on every boot rather than assuming it. mainai_app is never granted
        -- EXECUTE on this function (see REVOKE below and apply_runtime_privileges.py), so
        -- the app role's own lack of BYPASSRLS is irrelevant to this function's safety.
        CREATE OR REPLACE FUNCTION transition_memory_source_admin(
            p_source_id UUID,
            p_target_status VARCHAR(16),
            p_reason TEXT,
            p_actor_type VARCHAR(16),
            p_actor_id UUID
        ) RETURNS VOID
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_owner      uuid;
            v_old_status varchar(16);
        BEGIN
            IF p_actor_type NOT IN ('founder', 'system', 'admin', 'migration') THEN
                RAISE EXCEPTION 'transition_memory_source_admin: invalid actor_type %', p_actor_type;
            END IF;

            IF p_reason IS NULL OR btrim(p_reason) = '' THEN
                RAISE EXCEPTION 'transition_memory_source_admin: reason is required';
            END IF;

            SELECT owner_id, lifecycle_status INTO v_owner, v_old_status
            FROM public.memory_source_units WHERE id = p_source_id FOR UPDATE;

            IF NOT FOUND THEN
                RAISE EXCEPTION 'transition_memory_source_admin: source % not found', p_source_id;
            END IF;

            PERFORM set_config('memory.transition_active', 'on', true);

            IF p_target_status = 'revoked' AND v_old_status = 'active' THEN
                UPDATE public.memory_source_units
                SET lifecycle_status = 'revoked', revoked_at = now(), revocation_reason = p_reason
                WHERE id = p_source_id;
            ELSIF p_target_status = 'active' AND v_old_status = 'revoked' THEN
                UPDATE public.memory_source_units
                SET lifecycle_status = 'active', revoked_at = NULL, revocation_reason = NULL
                WHERE id = p_source_id;
            ELSIF p_target_status = 'purged' AND v_old_status IN ('active', 'revoked') THEN
                UPDATE public.memory_source_units
                SET lifecycle_status = 'purged', purged_at = now(), purge_reason = p_reason,
                    content_text = NULL, content_hash = NULL, content_hash_version = NULL
                WHERE id = p_source_id;
            ELSE
                PERFORM set_config('memory.transition_active', 'off', true);
                RAISE EXCEPTION 'transition_memory_source_admin: illegal transition % -> %', v_old_status, p_target_status;
            END IF;

            PERFORM set_config('memory.transition_active', 'off', true);

            INSERT INTO public.memory_source_lifecycle_events
                (owner_id, memory_source_id, from_status, to_status, reason, actor_type, actor_id)
            VALUES (v_owner, p_source_id, v_old_status, p_target_status, p_reason, p_actor_type, p_actor_id);
        END;
        $$;

        REVOKE ALL ON FUNCTION transition_memory_source_admin(UUID, VARCHAR, TEXT, VARCHAR, UUID) FROM PUBLIC;
        -- mainai_app is never granted EXECUTE here — see GRANT section below.

        -- Owner-scoped, same reasoning as transition_own_memory_source above: no RLS
        -- bypass needed, the explicit v_caller = p_owner_id check is the enforcement, and
        -- RLS's own policy permits it naturally since it's exactly the caller's own row.
        CREATE OR REPLACE FUNCTION erase_owner_memory(p_owner_id UUID) RETURNS VOID
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_caller uuid;
        BEGIN
            v_caller := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
            IF v_caller IS NULL OR v_caller IS DISTINCT FROM p_owner_id THEN
                RAISE EXCEPTION 'erase_owner_memory: caller may only erase their own memory';
            END IF;

            DELETE FROM public.knowledge_claims WHERE owner_id = p_owner_id;

            PERFORM set_config('memory.erasure_in_progress', 'on', true);
            DELETE FROM public.document_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.memory_source_units WHERE owner_id = p_owner_id;
            PERFORM set_config('memory.erasure_in_progress', 'off', true);
        END;
        $$;

        REVOKE ALL ON FUNCTION erase_owner_memory(UUID) FROM PUBLIC;

        -- Admin/migration escape hatch, no ownership check by design — same requirement
        -- as transition_memory_source_admin above: the function-owning role must
        -- genuinely have BYPASSRLS (or be superuser), externally verified by
        -- apply_runtime_privileges.py, not assumed via `row_security = off` (which
        -- would not grant anything RLS itself denies).
        CREATE OR REPLACE FUNCTION erase_owner_memory_admin(p_owner_id UUID) RETURNS VOID
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            DELETE FROM public.knowledge_claims WHERE owner_id = p_owner_id;

            PERFORM set_config('memory.erasure_in_progress', 'on', true);
            DELETE FROM public.document_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.memory_source_units WHERE owner_id = p_owner_id;
            PERFORM set_config('memory.erasure_in_progress', 'off', true);
        END;
        $$;

        REVOKE ALL ON FUNCTION erase_owner_memory_admin(UUID) FROM PUBLIC;
        -- mainai_app is never granted EXECUTE here either.
    """)

    # --- Privilege narrowing for mainai_app: deliberately NOT done here ------------------
    # See the module docstring: a literal REVOKE/GRANT naming mainai_app here would break
    # the "Backend — Alembic migration check" CI job (its database never creates that role).
    # This migration does NOT touch schema-level privileges (REVOKE CREATE ON SCHEMA public
    # FROM PUBLIC) either, for the same reversibility reason: backend/scripts/
    # s1a_privilege_policy.py (applied by ensure_app_role.py and apply_runtime_privileges.py,
    # see backend/docker-entrypoint.sh) is the sole place that global, schema-wide change is
    # made and re-verified — a migration whose downgrade() can't safely undo a global schema
    # privilege change has no business making one in the first place.


def downgrade() -> None:
    conn = op.get_bind()
    checks = [
        ("knowledge_claims", "memory_source_id IS NOT NULL"),
        ("memory_source_lifecycle_events", "TRUE"),
        ("document_source_units", "TRUE"),
        ("memory_source_units", "TRUE"),
    ]
    for table, cond in checks:
        in_use = conn.execute(sa.text(f"SELECT 1 FROM {table} WHERE {cond} LIMIT 1")).first()
        if in_use is not None:
            raise RuntimeError(
                f"0019 downgrade refused: {table} already has data ({cond}). "
                "This migration is reversible only before any real cutover/backfill runs. "
                "Roll forward instead of down."
            )

    op.execute("""
        DROP FUNCTION IF EXISTS erase_owner_memory_admin(UUID);
        DROP FUNCTION IF EXISTS erase_owner_memory(UUID);
        DROP FUNCTION IF EXISTS transition_memory_source_admin(UUID, VARCHAR, TEXT, VARCHAR, UUID);
        DROP FUNCTION IF EXISTS transition_own_memory_source(UUID, VARCHAR, TEXT, VARCHAR);

        ALTER TABLE knowledge_claims DROP COLUMN IF EXISTS memory_source_id;

        DROP POLICY IF EXISTS memory_source_lifecycle_events_isolation ON memory_source_lifecycle_events;
        DROP TRIGGER IF EXISTS trg_msle_no_update ON memory_source_lifecycle_events;
        DROP FUNCTION IF EXISTS trg_msle_forbid_update();
        DROP TABLE IF EXISTS memory_source_lifecycle_events;

        DROP POLICY IF EXISTS document_source_units_isolation ON document_source_units;
        DROP TRIGGER IF EXISTS trg_dsu_no_delete ON document_source_units;
        DROP TRIGGER IF EXISTS trg_dsu_update_guard ON document_source_units;
        DROP TRIGGER IF EXISTS trg_dsu_validate ON document_source_units;
        DROP FUNCTION IF EXISTS trg_dsu_forbid_delete();
        DROP FUNCTION IF EXISTS trg_dsu_guard_update();
        DROP FUNCTION IF EXISTS trg_dsu_validate_fields();
        DROP TABLE IF EXISTS document_source_units;

        DROP POLICY IF EXISTS memory_source_units_isolation ON memory_source_units;
        DROP TRIGGER IF EXISTS trg_msu_subtype_required ON memory_source_units;
        DROP TRIGGER IF EXISTS trg_msu_no_delete ON memory_source_units;
        DROP TRIGGER IF EXISTS trg_msu_update_guard ON memory_source_units;
        DROP FUNCTION IF EXISTS trg_msu_check_subtype_exists();
        DROP FUNCTION IF EXISTS trg_msu_forbid_delete();
        DROP FUNCTION IF EXISTS trg_msu_guard_update();
        DROP TABLE IF EXISTS memory_source_units;
    """)
