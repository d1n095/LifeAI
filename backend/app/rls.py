import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger("mainai.rls")

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
    # MainAI Runtime Truthfulness and Durable Job Foundation (migration 0025) — see
    # app/models/mainai_job.py.
    "ALTER TABLE mainai_jobs ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_jobs FORCE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_job_events ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_job_events FORCE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_job_proposals ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_job_proposals FORCE ROW LEVEL SECURITY",
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
    {
        "table": "mainai_jobs",
        "name": "mainai_jobs_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "mainai_job_events",
        "name": "mainai_job_events_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "mainai_job_proposals",
        "name": "mainai_job_proposals_isolation",
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


# mainai_job_events/mainai_job_proposals' append-only guarantees (migration 0026) depend on
# mainai_app NOT holding UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER on them — but
# scripts/ensure_app_role.py unconditionally re-runs `GRANT ALL PRIVILEGES ON ALL TABLES IN
# SCHEMA public TO mainai_app` on EVERY container boot, before Alembic even runs (see that
# script's docstring, and the Pass 12 boot-persistence incident in docs/BRANCH_REGISTRY.md
# for the exact same class of bug: a REVOKE applied once, at migration time, is silently
# undone by the next restart). apply_mainai_job_runtime_privileges() below re-asserts the
# lockdown on every boot AND verifies the exact final state — not just "did the REVOKE/GRANT
# statements run without erroring" but "is mainai_app's actual privilege set exactly what it
# should be, and are the SECURITY DEFINER functions exactly what they should be" — because a
# function that can bypass RLS and delete rows (erase_own_mainai_job_children(), see
# migration 0026) is exactly the kind of object where "the statement didn't error" is not the
# same guarantee as "the object is actually safe": a second review round found the FIRST
# version of that function took a caller-supplied owner id with no ownership check at all
# (SECURITY DEFINER meant its DELETEs ran with the function owner's privileges regardless of
# who called it or which owner they claimed) — a real cross-owner deletion vulnerability that
# a purely "did REVOKE/GRANT succeed" check would never have caught, because the bug was in
# the function's own logic and signature, not in a missing grant.
_MAINAI_JOB_EVENT_TABLE_ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT"})
_MAINAI_JOB_PROPOSAL_TABLE_ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE"})

# One row per SECURITY DEFINER / trigger function this policy owns. mainai_app_execute=False
# for the two trigger functions is deliberate: firing a trigger never requires the
# DML-issuing role to hold EXECUTE on the trigger function (Postgres invokes it as part of
# the DML statement itself), so mainai_app correctly has no reason to be granted it — if it
# ever were, that would itself be a sign something's wrong (a trigger function is never meant
# to be called directly).
_MAINAI_JOB_FUNCTION_SPECS = [
    {"name": "erase_own_mainai_job_children", "nargs": 0, "return_type": "void", "mainai_app_execute": True},
    {"name": "mainai_job_events_deny_mutation", "nargs": 0, "return_type": "trigger", "mainai_app_execute": False},
    {"name": "mainai_job_proposals_guard_mutation", "nargs": 0, "return_type": "trigger", "mainai_app_execute": False},
]


def apply_mainai_job_runtime_privileges(engine: Engine, *, require_complete: bool = True) -> None:
    """Applies AND verifies mainai_app's exact runtime privileges on the mainai_job_*
    integrity objects (migration 0026) — call this AFTER apply_rls() on every startup (see
    app/main.py), after both Alembic and scripts/ensure_app_role.py have already run (this
    function assumes migration 0026's tables/functions already exist; it does not create
    them — that's the migration's job, and deliberately does not reference the mainai_app
    role at all, so it stays portable to a bare database — see that migration's docstring).

    Two phases, same transaction:
    1. Enforce: idempotently REVOKE the excess table privileges scripts/ensure_app_role.py's
       blanket ALL-PRIVILEGES grant left behind, and GRANT EXECUTE on the one function
       mainai_app is actually meant to call.
    2. Verify: re-read the actual resulting state from Postgres's own catalogs (not just
       trust that step 1's statements succeeded) — table grants via
       information_schema.role_table_grants, and for every function in
       _MAINAI_JOB_FUNCTION_SPECS: exactly one overload exists (pg_proc), argument count,
       return type, SECURITY DEFINER, search_path=pg_catalog, plpgsql language, owner is
       never mainai_app, PUBLIC holds no EXECUTE, and mainai_app's EXECUTE matches exactly
       what that function is supposed to grant it (see _MAINAI_JOB_FUNCTION_SPECS above).

    require_complete=True (the default, and what app/main.py's real startup call uses) raises
    RuntimeError — inside the same transaction as the REVOKE/GRANT statements above, so a
    policy violation rolls back any partial change rather than leaving the database in a
    half-applied state — if anything doesn't match. require_complete=False logs a warning
    instead of raising, for a caller that wants a non-fatal diagnostic read of current state
    (e.g. a test asserting on the returned drift) rather than enforcement."""
    with engine.begin() as conn:
        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON mainai_job_events FROM mainai_app"))
        conn.execute(text("REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON mainai_job_proposals FROM mainai_app"))
        conn.execute(text("GRANT EXECUTE ON FUNCTION erase_own_mainai_job_children() TO mainai_app"))

        errors: list[str] = []

        for table, allowed in (
            ("mainai_job_events", _MAINAI_JOB_EVENT_TABLE_ALLOWED_PRIVILEGES),
            ("mainai_job_proposals", _MAINAI_JOB_PROPOSAL_TABLE_ALLOWED_PRIVILEGES),
        ):
            granted = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT privilege_type FROM information_schema.role_table_grants "
                        "WHERE table_schema = 'public' AND table_name = :t AND grantee = 'mainai_app'"
                    ),
                    {"t": table},
                ).all()
            }
            if granted != allowed:
                errors.append(f"{table}: mainai_app privileges are {sorted(granted)}, expected exactly {sorted(allowed)}")

        for spec in _MAINAI_JOB_FUNCTION_SPECS:
            name = spec["name"]
            overload_count = conn.execute(
                text("SELECT count(*) FROM pg_proc WHERE proname = :name AND pronamespace = 'public'::regnamespace"),
                {"name": name},
            ).scalar()
            if overload_count != 1:
                errors.append(f"{name}: expected exactly 1 overload in the public schema, found {overload_count}")
                continue

            nargs, rettype, prosecdef, proconfig, owner, lang = conn.execute(
                text(
                    "SELECT p.pronargs, pg_catalog.format_type(p.prorettype, NULL), p.prosecdef, "
                    "       p.proconfig, p.proowner::regrole::text, l.lanname "
                    "FROM pg_proc p JOIN pg_language l ON p.prolang = l.oid "
                    "WHERE p.proname = :name AND p.pronamespace = 'public'::regnamespace"
                ),
                {"name": name},
            ).first()
            if nargs != spec["nargs"]:
                errors.append(f"{name}: expected {spec['nargs']} argument(s), found {nargs}")
            if rettype != spec["return_type"]:
                errors.append(f"{name}: expected return type '{spec['return_type']}', found '{rettype}'")
            if not prosecdef:
                errors.append(f"{name}: expected SECURITY DEFINER, not set")
            if lang != "plpgsql":
                errors.append(f"{name}: expected language plpgsql, found '{lang}'")
            if not proconfig or "search_path=pg_catalog" not in proconfig:
                errors.append(f"{name}: expected search_path=pg_catalog, found {proconfig}")
            if owner == "mainai_app":
                errors.append(f"{name}: must never be owned by mainai_app")

            grantees = {
                row[0]
                for row in conn.execute(
                    text(
                        "SELECT grantee FROM information_schema.routine_privileges "
                        "WHERE routine_schema = 'public' AND routine_name = :name AND privilege_type = 'EXECUTE'"
                    ),
                    {"name": name},
                ).all()
            }
            if "PUBLIC" in grantees:
                errors.append(f"{name}: PUBLIC must never hold EXECUTE")
            mainai_app_has_execute = "mainai_app" in grantees
            if mainai_app_has_execute != spec["mainai_app_execute"]:
                errors.append(f"{name}: mainai_app EXECUTE is {mainai_app_has_execute}, expected {spec['mainai_app_execute']}")

        if errors:
            message = "mainai job runtime privilege policy violated:\n" + "\n".join(f"  - {e}" for e in errors)
            if require_complete:
                raise RuntimeError(message)
            logger.warning(message)
