"""Repository File Source Units — S1D: git-tracked repository files as a third
`memory_source_units` subtype, resolving the architecture fork
`docs/LIFE_PROJECT_KNOWLEDGE_INGESTION_DESIGN.md` §4 named but deliberately did not choose.

Founder directive, 2026-11-15 ("CONTINUE MAINAI INTEGRATION"), Priority 2: "Evaluate whether
existing source abstractions can represent repository documents directly or require an
additive, backward-compatible extension... Do not introduce a parallel knowledge database.
Implement only an isolated, reviewable foundation if the architecture evidence supports it."

THE DECISION, with the evidence behind it (not asserted, read): `app.rag.memory_source.
get_or_create_memory_source_unit()` — the only reviewed path to a `MemorySourceUnit` for
document-derived content — requires a real `document_id`, because `DocumentSourceUnit` is
built for content already uploaded through the Library pipeline (`Document`/`DocumentChunk`/
`KnowledgeVersion`). A git-tracked repository file (`docs/*.md`, `CLAUDE.md`, etc.) has never
been uploaded there and has no `document_id` — forcing it through that path would mean
actually running the Library upload flow for ~114 files and reinterpreting `KnowledgeVersion`
(upload-time versioning) to mean "git commit" (a different concept it was never designed to
hold). The SMALLER, additive, backward-compatible answer: `SourceKind` is NOT a native
Postgres enum — migration 0019 declared `source_kind varchar(32)` with a CHECK constraint, and
`MessageSourceUnit` (migration 0037, S1C) already proved this table supports a THIRD-ARM
extension safely once before (document -> +message). This migration adds the fourth arm the
exact same way: one new CHECK value, one new subtype table, one widened `trg_msu_check_
subtype_exists()` branch — never a parallel table, never a new database, never touching
`document_source_units`/`message_source_units` at all.

Identity, per the founder's explicit requirement ("support repository files, commit SHAs,
file paths and source revisions"): `(repo_identity, file_path, commit_sha)` — e.g.
`("d1n095/LifeAI", "docs/AUTH_THREAT_MODEL.md", "<40-hex-char commit sha>")`. `commit_sha`
is DB-CHECK-enforced to be a real, resolved 40-hex-character SHA — never "HEAD", never a
branch name — matching this whole session's own exact-SHA evidence discipline structurally,
not just by convention. A new commit touching the same file is a NEW, separate, immutable
`memory_source_units` row (git's own history already IS the version history this architecture
needs — "preserve previous versions" falls out of this for free, no separate versioning
concept invented). An unchanged file produces the identical `source_identity_key` and is
found, not re-created, by `get_or_create_repository_file_source_unit()`'s SAVEPOINT find-or-
create (mirroring `get_or_create_memory_source_unit()` exactly) — "incremental updates without
duplicating unchanged knowledge" is the existing `uq_msu_owner_identity` constraint doing
exactly what it already does for documents, not a new mechanism.

KNOWN MIGRATION-REVISION COLLISION (restated, not resolved, here): dev's actual head is 0068.
Five branches already independently claim revision "0069" with down_revision "0068"
(#412/#413 `0069_security_remediation`-lineage, #408 `0069_first_cycle_go_gate`, #403
`0069_github_evidence_staging`, #411's own calendar migration) — all already tracked in
`docs/BRANCH_REGISTRY.md`, with the founder's own stated composition order (#412/#413 -> #408
-> #403 -> #411) and explicit "do not merge or renumber in isolation" instruction for each.
THIS migration is a sixth, independently-reviewable candidate for the same slot — exactly the
same resolution this codebase already used once before for an identical collision (see
migration 0037's own docstring: "THIS migration keeps 0037 unchanged... the other branch must
be renumbered when it is next rebased onto mainline"). This branch does not merge, rebase, or
renumber any of the other five; whoever composes that already-tracked queue decides this
migration's final position in it, same as the other five.

Revision ID: 0069
Revises: 0068
Create Date: 2026-11-15
"""

from alembic import op

revision = "0069"
down_revision = "0068"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- extend the parent CHECK additively — every existing row's source_kind remains valid.
    op.execute("""
        ALTER TABLE memory_source_units DROP CONSTRAINT ck_msu_source_kind;
        ALTER TABLE memory_source_units ADD CONSTRAINT ck_msu_source_kind
            CHECK (source_kind IN ('document_chunk', 'document_version', 'document_record', 'message', 'repository_file'));
    """)

    # Widen the exclusive-arc guarantee to a fourth branch — mirrors exactly how migration
    # 0037 widened this same function from one branch to two; the function's own contract
    # (raise unless a matching subtype row exists) is unchanged.
    op.execute("""
        CREATE OR REPLACE FUNCTION trg_msu_check_subtype_exists() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NEW.source_kind IN ('document_chunk', 'document_version', 'document_record') THEN
                IF NOT EXISTS (SELECT 1 FROM public.document_source_units WHERE memory_source_id = NEW.id) THEN
                    RAISE EXCEPTION 'memory_source_units %: no matching document_source_units row', NEW.id;
                END IF;
            ELSIF NEW.source_kind = 'message' THEN
                IF NOT EXISTS (SELECT 1 FROM public.message_source_units WHERE memory_source_id = NEW.id) THEN
                    RAISE EXCEPTION 'memory_source_units %: no matching message_source_units row', NEW.id;
                END IF;
            ELSIF NEW.source_kind = 'repository_file' THEN
                IF NOT EXISTS (SELECT 1 FROM public.repository_file_source_units WHERE memory_source_id = NEW.id) THEN
                    RAISE EXCEPTION 'memory_source_units %: no matching repository_file_source_units row', NEW.id;
                END IF;
            ELSE
                RAISE EXCEPTION 'memory_source_units %: unrecognized source_kind %', NEW.id, NEW.source_kind;
            END IF;
            RETURN NEW;
        END;
        $$;
    """)

    op.execute("""
        CREATE TABLE repository_file_source_units (
            memory_source_id uuid NOT NULL PRIMARY KEY REFERENCES memory_source_units(id),
            owner_id uuid NOT NULL,
            source_kind varchar(32) NOT NULL,

            repo_identity text NOT NULL,
            file_path text NOT NULL,
            commit_sha varchar(40) NOT NULL,

            FOREIGN KEY (memory_source_id, owner_id, source_kind)
                REFERENCES memory_source_units (id, owner_id, source_kind),

            -- Structural, not conventional: a caller declaring "HEAD" or a branch name as a
            -- commit_sha is rejected at the database level, matching this session's own
            -- exact-SHA evidence discipline. A git commit SHA is always exactly 40 lowercase
            -- hex characters (SHA-1); this table holds nothing else.
            CONSTRAINT ck_rfsu_commit_sha_format CHECK (commit_sha ~ '^[0-9a-f]{40}$'),

            -- repo_identity non-empty; file_path relative (never absolute, never a parent-
            -- directory escape) — cheap, structural defense-in-depth for a field that will
            -- later be used to resolve/display provenance, same spirit as every other
            -- immutable identity field in this table family being format-checked rather than
            -- merely trusted.
            CONSTRAINT ck_rfsu_repo_identity_nonempty CHECK (btrim(repo_identity) <> ''),
            CONSTRAINT ck_rfsu_file_path_relative CHECK (
                file_path !~ '^/' AND file_path !~ '(^|/)\\.\\.(/|$)'
            )
        );

        -- Not a UNIQUE index: uq_msu_owner_identity on the PARENT table already enforces
        -- true dedup via source_identity_key (which encodes all three fields). This index
        -- exists only to make "every commit of this file, in order" a cheap query — multiple
        -- legitimate rows (one per commit) are expected and correct, unlike document_source_
        -- units' partial-unique indexes, which exist precisely because at most one row per
        -- locator is ever legitimate there.
        CREATE INDEX ix_rfsu_file ON repository_file_source_units (owner_id, repo_identity, file_path);
    """)

    op.execute("""
        CREATE OR REPLACE FUNCTION trg_rfsu_validate_fields() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        DECLARE
            v_snap          varchar(16);
            v_identity      text;
            v_role          varchar(16);
            v_content       text;
            v_content_hash  varchar(64);
            v_hash_version  varchar(32);
        BEGIN
            SELECT snapshot_status, source_identity_key, source_role, content_text,
                   content_hash, content_hash_version
            INTO v_snap, v_identity, v_role, v_content, v_content_hash, v_hash_version
            FROM public.memory_source_units WHERE id = NEW.memory_source_id;

            IF NEW.source_kind <> 'repository_file' THEN
                RAISE EXCEPTION 'unexpected source_kind % for repository_file_source_units (id=%)', NEW.source_kind, NEW.memory_source_id;
            END IF;

            -- Same reasoning as document_source_units (migration 0019): nothing at ingest
            -- time can vouch for WHO wrote any given line of a repository file (it is
            -- typically many authors' cumulative work) -- real, verified attribution is a
            -- separate, later, versioned concern, never a side effect of recording a source.
            IF v_role <> 'unknown' THEN
                RAISE EXCEPTION 'repository_file_source_units: parent source_role must be unknown (id=%, source_role=%)', NEW.memory_source_id, v_role;
            END IF;

            IF TG_OP = 'INSERT' AND v_identity IS DISTINCT FROM (
                'repository_file:' || NEW.repo_identity || ':' || NEW.file_path || ':' || NEW.commit_sha
            ) THEN
                RAISE EXCEPTION 'source_identity_key does not match repo_identity/file_path/commit_sha (id=%)', NEW.memory_source_id;
            END IF;

            -- An 'exact' snapshot must be self-consistent: content_hash genuinely is
            -- sha256(content_text). Unlike document_chunk (checked against a live
            -- document_chunks row) or message (checked against a live messages row), there is
            -- no separate live table holding "the current text of this repository file at
            -- this commit" to cross-verify against -- content_text itself, captured once at
            -- ingest time from the actual git blob and immutable thereafter (trg_rfsu_guard_
            -- update below), IS the durable record. This check still catches a caller
            -- declaring a mismatched or fabricated hash for text it did supply correctly.
            IF v_snap = 'exact' THEN
                IF v_content IS NULL THEN
                    RAISE EXCEPTION 'repository_file exact snapshot requires content_text (id=%)', NEW.memory_source_id;
                END IF;
                IF v_content_hash IS DISTINCT FROM encode(sha256(convert_to(v_content, 'UTF8')), 'hex') THEN
                    RAISE EXCEPTION 'repository_file exact snapshot content_hash does not match sha256(content_text) (id=%)', NEW.memory_source_id;
                END IF;
                IF v_hash_version IS DISTINCT FROM 'sha256-utf8-v1' THEN
                    RAISE EXCEPTION 'repository_file exact snapshot content_hash_version must be sha256-utf8-v1 (id=%)', NEW.memory_source_id;
                END IF;
            END IF;

            RETURN NEW;
        END;
        $$;

        CREATE CONSTRAINT TRIGGER trg_rfsu_validate
            AFTER INSERT OR UPDATE ON repository_file_source_units
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION trg_rfsu_validate_fields();

        CREATE OR REPLACE FUNCTION trg_rfsu_guard_update() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            -- Every field is permanently immutable -- unlike document_source_units' chunk_id
            -- (which may be cleared, never reassigned, on purge), a repository-file source
            -- row has no analogous "degrade in place" concept: a new commit is always a new,
            -- separate row, never an edit of an existing one.
            IF NEW.memory_source_id IS DISTINCT FROM OLD.memory_source_id
            OR NEW.owner_id         IS DISTINCT FROM OLD.owner_id
            OR NEW.source_kind      IS DISTINCT FROM OLD.source_kind
            OR NEW.repo_identity    IS DISTINCT FROM OLD.repo_identity
            OR NEW.file_path        IS DISTINCT FROM OLD.file_path
            OR NEW.commit_sha       IS DISTINCT FROM OLD.commit_sha
            THEN
                RAISE EXCEPTION 'repository_file_source_units: all fields are immutable (id=%)', OLD.memory_source_id;
            END IF;
            RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_rfsu_update_guard
            BEFORE UPDATE ON repository_file_source_units
            FOR EACH ROW EXECUTE FUNCTION trg_rfsu_guard_update();

        CREATE OR REPLACE FUNCTION trg_rfsu_forbid_delete() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF current_setting('memory.erasure_in_progress', true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'repository_file_source_units: row deletion is not permitted outside account erasure (id=%)', OLD.memory_source_id;
            END IF;
            RETURN OLD;
        END;
        $$;

        CREATE TRIGGER trg_rfsu_no_delete
            BEFORE DELETE ON repository_file_source_units
            FOR EACH ROW EXECUTE FUNCTION trg_rfsu_forbid_delete();
    """)

    op.execute("""
        ALTER TABLE repository_file_source_units ENABLE ROW LEVEL SECURITY;
        ALTER TABLE repository_file_source_units FORCE ROW LEVEL SECURITY;
        CREATE POLICY repository_file_source_units_isolation ON repository_file_source_units
            USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
            WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
    """)

    # erase_owner_memory()/erase_owner_memory_admin() (migration 0019) must cascade into this
    # new subtype table too, under the same erasure_in_progress carve-out — otherwise account
    # erasure would leave repository_file_source_units rows orphaned (their parent
    # memory_source_units row deleted, these left behind with a dangling FK) or, worse, fail
    # outright on the FK constraint. CREATE OR REPLACE, not a new function: same signature,
    # same security/ownership contract, one more DELETE statement in the same transaction.
    op.execute("""
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
            DELETE FROM public.repository_file_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.message_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.document_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.memory_source_units WHERE owner_id = p_owner_id;
            PERFORM set_config('memory.erasure_in_progress', 'off', true);
        END;
        $$;

        CREATE OR REPLACE FUNCTION erase_owner_memory_admin(p_owner_id UUID) RETURNS VOID
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            DELETE FROM public.knowledge_claims WHERE owner_id = p_owner_id;

            PERFORM set_config('memory.erasure_in_progress', 'on', true);
            DELETE FROM public.repository_file_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.message_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.document_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.memory_source_units WHERE owner_id = p_owner_id;
            PERFORM set_config('memory.erasure_in_progress', 'off', true);
        END;
        $$;
    """)

    # --- Privilege narrowing for mainai_app: deliberately NOT done here --------------------
    # Same reasoning as migrations 0019/0037's own docstrings: a literal REVOKE/GRANT naming
    # mainai_app here would break "Backend — Alembic migration check" (its database never
    # creates that role). backend/scripts/security/s1a_privilege_policy.py's _PROTECTED_TABLES
    # list is extended in this same PR to add ("repository_file_source_units", ["SELECT",
    # "INSERT"]) alongside document_source_units/message_source_units — applied and verified
    # at every boot by ensure_app_role.py/apply_runtime_privileges.py exactly like every other
    # S1A-pattern table, never by this migration directly.


def downgrade() -> None:
    op.execute("""
        DROP POLICY IF EXISTS repository_file_source_units_isolation ON repository_file_source_units;
        DROP TRIGGER IF EXISTS trg_rfsu_no_delete ON repository_file_source_units;
        DROP FUNCTION IF EXISTS trg_rfsu_forbid_delete();
        DROP TRIGGER IF EXISTS trg_rfsu_update_guard ON repository_file_source_units;
        DROP FUNCTION IF EXISTS trg_rfsu_guard_update();
        DROP TRIGGER IF EXISTS trg_rfsu_validate ON repository_file_source_units;
        DROP FUNCTION IF EXISTS trg_rfsu_validate_fields();
        DROP TABLE IF EXISTS repository_file_source_units;

        -- Restore the two-arm (document/message) exclusive-arc check exactly as migration
        -- 0037 left it, since the third arm (repository_file) no longer exists after the
        -- DROP TABLE above.
        CREATE OR REPLACE FUNCTION trg_msu_check_subtype_exists() RETURNS TRIGGER
        LANGUAGE plpgsql
        SET search_path = pg_catalog
        AS $$
        BEGIN
            IF NEW.source_kind IN ('document_chunk', 'document_version', 'document_record') THEN
                IF NOT EXISTS (SELECT 1 FROM public.document_source_units WHERE memory_source_id = NEW.id) THEN
                    RAISE EXCEPTION 'memory_source_units %: no matching document_source_units row', NEW.id;
                END IF;
            ELSIF NEW.source_kind = 'message' THEN
                IF NOT EXISTS (SELECT 1 FROM public.message_source_units WHERE memory_source_id = NEW.id) THEN
                    RAISE EXCEPTION 'memory_source_units %: no matching message_source_units row', NEW.id;
                END IF;
            ELSE
                RAISE EXCEPTION 'memory_source_units %: unrecognized source_kind %', NEW.id, NEW.source_kind;
            END IF;
            RETURN NEW;
        END;
        $$;

        ALTER TABLE memory_source_units DROP CONSTRAINT ck_msu_source_kind;
        ALTER TABLE memory_source_units ADD CONSTRAINT ck_msu_source_kind
            CHECK (source_kind IN ('document_chunk', 'document_version', 'document_record', 'message'));

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
            DELETE FROM public.message_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.document_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.memory_source_units WHERE owner_id = p_owner_id;
            PERFORM set_config('memory.erasure_in_progress', 'off', true);
        END;
        $$;

        CREATE OR REPLACE FUNCTION erase_owner_memory_admin(p_owner_id UUID) RETURNS VOID
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog
        AS $$
        BEGIN
            DELETE FROM public.knowledge_claims WHERE owner_id = p_owner_id;

            PERFORM set_config('memory.erasure_in_progress', 'on', true);
            DELETE FROM public.message_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.document_source_units WHERE owner_id = p_owner_id;
            DELETE FROM public.memory_source_units WHERE owner_id = p_owner_id;
            PERFORM set_config('memory.erasure_in_progress', 'off', true);
        END;
        $$;
    """)
