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
    # S1A universal provenance core (migration 0019) — see docs/MAINAI_PROJECT_UNDERSTANDING_
    # PLAN.md §4.8 and app/models/memory_source_unit.py. Also enabled directly in the
    # migration itself; listed here too so this idempotent reapply path stays the single
    # source of truth for every owner-scoped table, matching the rest of this list.
    "ALTER TABLE memory_source_units ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE memory_source_units FORCE ROW LEVEL SECURITY",
    "ALTER TABLE document_source_units ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE document_source_units FORCE ROW LEVEL SECURITY",
    "ALTER TABLE memory_source_lifecycle_events ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE memory_source_lifecycle_events FORCE ROW LEVEL SECURITY",
    # Durable production backfill-run reporting (migration 0025) — see app/models/
    # memory_source_backfill_run.py and app/rag/backfill/memory_source_run.py. Also enabled
    # directly in the migration itself; listed here too so this idempotent reapply path stays
    # the single source of truth for every owner-scoped table, matching the rest of this list.
    "ALTER TABLE memory_source_backfill_runs ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE memory_source_backfill_runs FORCE ROW LEVEL SECURITY",
    "ALTER TABLE memory_source_backfill_failures ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE memory_source_backfill_failures FORCE ROW LEVEL SECURITY",
    # MainAI Runtime Truthfulness and Durable Job Foundation (migrations 0026/0027, renumbered
    # during integration from the frozen branch's own 0025/0026 — see 0026_mainai_jobs.py's
    # integration note) — see app/models/mainai_job.py.
    "ALTER TABLE mainai_jobs ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_jobs FORCE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_job_events ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_job_events FORCE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_job_proposals ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE mainai_job_proposals FORCE ROW LEVEL SECURITY",
    # Message-level owner isolation (migration 0031) — the one owner-scoped table that had no
    # policy of its own until then, flagged during S1B (docs/BRANCH_REGISTRY.md Pass 42) and
    # deliberately fixed in its own change. Ownership is DERIVED from the conversation rather
    # than stored on the row; see MESSAGES_ISOLATION_EXPR below and migration 0031's docstring
    # for why there is no `messages.owner_id`. Also enabled directly in the migration itself;
    # listed here too so this idempotent reapply path stays the single source of truth for
    # every owner-scoped table, matching the rest of this list.
    "ALTER TABLE messages ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE messages FORCE ROW LEVEL SECURITY",
    # Life Source Foundation Bootstrap (migration 0037; PR #60 provisional proposal).
    # source_import_batches/source_import_batch_failures are ordinary owner-scoped tracking
    # tables; message_source_units is the message-source slice of S1C (docs/
    # MAINAI_PROJECT_UNDERSTANDING_PLAN.md §4.8/§8), the same exclusive-arc pattern as
    # document_source_units, extended to Message. Also enabled directly in the migration
    # itself; listed here too so this idempotent reapply path stays the single source of
    # truth for every owner-scoped table, matching the rest of this list.
    "ALTER TABLE source_import_batches ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE source_import_batches FORCE ROW LEVEL SECURITY",
    "ALTER TABLE source_import_batch_failures ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE source_import_batch_failures FORCE ROW LEVEL SECURITY",
    "ALTER TABLE message_source_units ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE message_source_units FORCE ROW LEVEL SECURITY",
    # Life Intelligence Governance & Meta-Learning (migration 0038): immutable observations
    # linked to the existing MainAI execution runtime; never a parallel job system.
    *[
        statement
        for table in (
            "intelligence_executions", "intelligence_evidence", "intelligence_interpretations",
            "intelligence_ideas", "intelligence_idea_links", "intelligence_idea_lessons",
        )
        for statement in (f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    ],
    *[
        statement
        for table in ("active_context_sets", "active_context_members", "active_context_events",
                      "memory_threads", "memory_thread_members", "memory_thread_relationships", "memory_thread_events",
                      "life_intents", "life_intent_blockers", "life_intent_dependencies", "life_intent_events",
                      "life_problems", "life_problem_approaches", "life_solution_components", "life_component_evaluations",
                      "life_problem_assumptions", "life_problem_decisions", "life_approach_outcomes", "life_problem_lesson_links",
                      "life_solution_selections", "life_solution_component_links", "life_problem_events",
                      "work_strategies", "work_strategy_executions", "work_trace_events", "work_efficiency_observations",
                      "work_strategy_findings", "work_verification_obligations", "work_verification_observations",
                      "work_stopping_decisions", "work_specialist_contributions", "work_strategy_lesson_links")
        for statement in (f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    ],
]

# `messages` is the one table here whose owner is not a column on the row itself: a message
# belongs to whoever owns its conversation, and that fact is stated exactly once, in
# `conversations.user_id`. Storing a second copy on `messages` would create something that can
# drift; deriving it cannot. The uncorrelated `IN (SELECT ...)` form (rather than a correlated
# EXISTS) is a measured choice, not a stylistic one — it plans at the no-RLS baseline on the
# bulk owner-wide scans the backfill and account export perform, where EXISTS costs roughly
# double. Both the full benchmark and the interaction with migration 0030's sequence-number
# trigger are documented in migration 0031's docstring.
#
# Must stay character-identical to `_MESSAGES_ISOLATION_EXPR` in
# alembic/versions/0031_messages_rls.py — the policy is created by that migration and merely
# REPAIRED here, so a drift between the two would mean a rebuilt policy silently enforces a
# different rule than the one a fresh database gets. tests/backend/test_rls_policy_registry.py
# compares every live policy in pg_policies against this list to catch exactly that.
MESSAGES_ISOLATION_EXPR = (
    "conversation_id IN ("
    "SELECT c.id FROM conversations c "
    "WHERE c.user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid"
    ")"
)

# One policy per table: rows are only visible/writable when they belong to the user bound
# to the current request (set via `SET LOCAL app.current_user_id` in app/deps.py). Missing
# `app.current_user_id` (e.g. a raw admin/migration connection) resolves to NULL, which never
# matches — default-deny, not default-allow. Most entries express "belongs to" as a literal
# owner column on the row; `messages` (migration 0031) instead derives it from the owning
# conversation, because that is where the fact is actually recorded — see
# MESSAGES_ISOLATION_EXPR above.
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
        "table": "memory_source_units",
        "name": "memory_source_units_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "document_source_units",
        "name": "document_source_units_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "memory_source_lifecycle_events",
        "name": "memory_source_lifecycle_events_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "memory_source_backfill_runs",
        "name": "memory_source_backfill_runs_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "memory_source_backfill_failures",
        "name": "memory_source_backfill_failures_isolation",
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
    {
        "table": "messages",
        "name": "messages_isolation",
        # The only DERIVED entry in this list — see MESSAGES_ISOLATION_EXPR above.
        "expr": MESSAGES_ISOLATION_EXPR,
    },
    {
        "table": "source_import_batches",
        "name": "source_import_batches_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "source_import_batch_failures",
        "name": "source_import_batch_failures_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    {
        "table": "message_source_units",
        "name": "message_source_units_isolation",
        "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
    },
    *[
        {
            "table": table,
            "name": f"{table}_isolation",
            "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
        }
        for table in (
            "intelligence_executions", "intelligence_evidence", "intelligence_interpretations",
            "intelligence_ideas", "intelligence_idea_links", "intelligence_idea_lessons",
        )
    ],
    *[
        {
            "table": table,
            "name": f"{table}_isolation",
            "expr": "owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid",
        }
        for table in ("active_context_sets", "active_context_members", "active_context_events",
                      "memory_threads", "memory_thread_members", "memory_thread_relationships", "memory_thread_events",
                      "life_intents", "life_intent_blockers", "life_intent_dependencies", "life_intent_events",
                      "life_problems", "life_problem_approaches", "life_solution_components", "life_component_evaluations",
                      "life_problem_assumptions", "life_problem_decisions", "life_approach_outcomes", "life_problem_lesson_links",
                      "life_solution_selections", "life_solution_component_links", "life_problem_events",
                      "work_strategies", "work_strategy_executions", "work_trace_events", "work_efficiency_observations",
                      "work_strategy_findings", "work_verification_obligations", "work_verification_observations",
                      "work_stopping_decisions", "work_specialist_contributions", "work_strategy_lesson_links")
    ],
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


# mainai_job_events/mainai_job_proposals' append-only guarantees (migration 0027, renumbered
# during integration from the frozen branch's own 0026 — see 0027_mainai_job_integrity.py's
# integration note) depend on
# mainai_app NOT holding UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER on them — but
# scripts/security/ensure_app_role.py unconditionally re-runs `GRANT ALL PRIVILEGES ON ALL TABLES IN
# SCHEMA public TO mainai_app` on EVERY container boot, before Alembic even runs (see that
# script's docstring, and the Pass 12 boot-persistence incident in docs/BRANCH_REGISTRY.md
# for the exact same class of bug: a REVOKE applied once, at migration time, is silently
# undone by the next restart). apply_mainai_job_runtime_privileges() below re-asserts the
# lockdown on every boot AND verifies the exact final state — not just "did the REVOKE/GRANT
# statements run without erroring" but "is mainai_app's actual privilege set exactly what it
# should be, and are the SECURITY DEFINER functions exactly what they should be" — because a
# function that can bypass RLS and delete rows (erase_own_mainai_job_children(), see
# migration 0027) is exactly the kind of object where "the statement didn't error" is not the
# same guarantee as "the object is actually safe": a second review round found the FIRST
# version of that function took a caller-supplied owner id with no ownership check at all
# (SECURITY DEFINER meant its DELETEs ran with the function owner's privileges regardless of
# who called it or which owner they claimed) — a real cross-owner deletion vulnerability that
# a purely "did REVOKE/GRANT succeed" check would never have caught, because the bug was in
# the function's own logic and signature, not in a missing grant.
_MAINAI_JOB_EVENT_TABLE_ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT"})
_MAINAI_JOB_PROPOSAL_TABLE_ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT", "UPDATE"})

# All owner-scoped tables this policy manages, for the ownership check below — not just the
# two that get privilege lockdown, since a table silently getting re-owned by anything other
# than the real migration/admin role is itself a drift signal worth catching.
_MAINAI_JOB_TABLES = ("mainai_jobs", "mainai_job_events", "mainai_job_proposals")

# One row per SECURITY DEFINER / trigger function this policy owns. mainai_app_execute=False
# for the two trigger functions is deliberate: firing a trigger never requires the
# DML-issuing role to hold EXECUTE on the trigger function (Postgres invokes it as part of
# the DML statement itself), so mainai_app correctly has no reason to be granted it — if it
# ever were, that would itself be a sign something's wrong (a trigger function is never meant
# to be called directly). `identity_args` is the exact `pg_get_function_identity_arguments()`
# string for each function's signature — all three are zero-argument, so `""` — checked
# instead of (not just alongside) a raw `pronargs` count: `pronargs` alone can't distinguish
# `f()` from some other zero-declared-but-variadic/defaulted overload, and a second overload
# with a different signature would silently coexist rather than get caught.
_MAINAI_JOB_FUNCTION_SPECS = [
    {"name": "erase_own_mainai_job_children", "identity_args": "", "return_type": "void", "mainai_app_execute": True},
    {"name": "mainai_job_events_deny_mutation", "identity_args": "", "return_type": "trigger", "mainai_app_execute": False},
    {"name": "mainai_job_proposals_guard_mutation", "identity_args": "", "return_type": "trigger", "mainai_app_execute": False},
]

# Same lockdown as _MAINAI_JOB_EVENT_TABLE_ALLOWED_PRIVILEGES above, applied to the MainAI
# Execution Loop V0.1 (migration 0032) append-only tables — both are guarded by an identical
# BEFORE UPDATE/DELETE trigger (deny UPDATE unconditionally, deny DELETE unless
# app.mainai_execution_erasure_in_progress is set). `mainai_goals`/`mainai_plans`/
# `mainai_tasks`/`mainai_task_dependencies` are deliberately NOT locked down here — they are
# ordinary mutable tables (guarded by the service layer's own composite-owner-FK/status-machine
# checks, not a DB trigger), so they keep whatever baseline CRUD
# scripts/security/ensure_app_role.py's blanket grant already provides, same as `mainai_jobs`
# itself.
_MAINAI_EXECUTION_TASK_EVENT_TABLE_ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT"})
_MAINAI_EXECUTION_CHECKPOINT_TABLE_ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT"})
# V0.2 hardening-pass finding (P1): migration 0033's own module docstring already claimed
# "SELECT/INSERT on the two append-only-by-convention-or-trigger tables" is "applied and
# verified by app/rls.py's apply_mainai_execution_privileges(), extended to cover these three
# tables" -- but that extension never actually happened; this module was never touched by the
# V0.2 branch at all. mainai_recovery_events (append-only, same DB-trigger-enforced-immutability
# pattern as mainai_task_events/mainai_checkpoints) was left on ensure_app_role.py's blanket
# SELECT/INSERT/UPDATE/DELETE default -- the deny-mutation trigger still functionally blocked
# every UPDATE/DELETE attempt (it does not depend on this policy), so this was never an actual
# hole in what mainai_app could DO, only a real deviation from this codebase's own established
# "narrow every write path to exactly what it needs, never rely on a single layer alone"
# doctrine (see the S1A privilege-policy series) and from what the migration's own docstring
# already promised a reviewer.
_MAINAI_RECOVERY_EVENT_TABLE_ALLOWED_PRIVILEGES = frozenset({"SELECT", "INSERT"})

# All tables migration 0032 created, for the ownership-drift check below — same reasoning as
# _MAINAI_JOB_TABLES. V0.2 (migration 0033) added three more: mainai_task_worktrees/
# mainai_recovery_records are ordinary mutable tables (same "keep the baseline CRUD grant,
# only verify ownership" treatment mainai_goals/mainai_plans/mainai_tasks already get, per the
# comment above); mainai_recovery_events is append-only and privilege-narrowed below exactly
# like mainai_task_events/mainai_checkpoints. V0.3 (migration 0036) added
# mainai_task_waits — another ordinary mutable table (same baseline-CRUD, ownership-only
# treatment as mainai_task_worktrees/mainai_recovery_records; a poll updates its own row's
# status/evidence/next_poll_at in place, so it cannot be append-only like the *_events tables).
_MAINAI_EXECUTION_TABLES = (
    "mainai_goals",
    "mainai_plans",
    "mainai_tasks",
    "mainai_task_dependencies",
    "mainai_task_events",
    "mainai_checkpoints",
    "engineering_lessons",
    "mainai_task_worktrees",
    "mainai_recovery_records",
    "mainai_recovery_events",
    "mainai_task_waits",
    "intelligence_executions",
    "intelligence_evidence",
    "intelligence_interpretations",
    "intelligence_ideas",
    "intelligence_idea_links",
    "intelligence_idea_lessons",
    # Migration 0039 deliberately reuses this already boot-persistent ownership/privilege
    # verifier rather than introducing a second security-policy runner for three context
    # tables linked to the same execution/source domain.
    "active_context_sets",
    "active_context_members",
    "active_context_events",
    "memory_threads",
    "memory_thread_members",
    "memory_thread_relationships",
    "memory_thread_events",
    "life_intents",
    "life_intent_blockers",
    "life_intent_dependencies",
    "life_intent_events",
    "life_problems",
    "life_problem_approaches",
    "life_solution_components",
    "life_component_evaluations",
    "life_problem_assumptions",
    "life_problem_decisions",
    "life_approach_outcomes",
    "life_problem_lesson_links",
    "life_solution_selections",
    "life_solution_component_links",
    "life_problem_events",
    "work_strategies",
    "work_strategy_executions",
    "work_trace_events",
    "work_efficiency_observations",
    "work_strategy_findings",
    "work_verification_obligations",
    "work_verification_observations",
    "work_stopping_decisions",
    "work_specialist_contributions",
    "work_strategy_lesson_links",
)

_MAINAI_EXECUTION_FUNCTION_SPECS = [
    {"name": "erase_own_mainai_execution_children", "identity_args": "", "return_type": "void", "mainai_app_execute": True},
    {"name": "mainai_task_events_deny_mutation", "identity_args": "", "return_type": "trigger", "mainai_app_execute": False},
    {"name": "mainai_checkpoints_deny_mutation", "identity_args": "", "return_type": "trigger", "mainai_app_execute": False},
    {"name": "mainai_recovery_events_deny_mutation", "identity_args": "", "return_type": "trigger", "mainai_app_execute": False},
    {
        "name": "intelligence_governance_deny_mutation",
        "identity_args": "",
        "return_type": "trigger",
        "mainai_app_execute": False,
        "security_definer": False,
    },
    {
        "name": "active_context_events_deny_update",
        "identity_args": "",
        "return_type": "trigger",
        "mainai_app_execute": False,
        "security_definer": False,
    },
    {
        "name": "memory_thread_immutable_deny_update",
        "identity_args": "",
        "return_type": "trigger",
        "mainai_app_execute": False,
        "security_definer": False,
    },
    {
        "name": "life_intent_append_only_deny_update", "identity_args": "", "return_type": "trigger",
        "mainai_app_execute": False, "security_definer": False,
    },
    {
        "name": "life_problem_append_only_deny_update", "identity_args": "", "return_type": "trigger",
        "mainai_app_execute": False, "security_definer": False,
    },
    {
        "name": "work_intelligence_deny_mutation", "identity_args": "", "return_type": "trigger",
        "mainai_app_execute": False, "security_definer": False,
    },
    {
        "name": "work_strategy_execution_counter_only", "identity_args": "", "return_type": "trigger",
        "mainai_app_execute": False, "security_definer": False,
    },
]

# The full table-privilege vocabulary this policy checks — deliberately checked one-by-one via
# has_table_privilege() rather than filtered from information_schema.role_table_grants: the
# information_schema view only lists grants made *directly* to the named grantee (or to
# PUBLIC), so a privilege mainai_app holds *indirectly* — via membership in some other role
# that was separately granted it — would be invisible to a role_table_grants query but would
# still let mainai_app actually perform that DML. has_table_privilege() (and
# has_function_privilege() below) compute Postgres's real, effective privilege check, following
# the role-membership graph exactly the way the database itself does when deciding whether to
# allow a statement — closing that hidden-bypass gap, not just checking the obvious grants.
_ALL_TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _effective_table_privileges(conn, role: str, table: str) -> set[str]:
    return {
        priv
        for priv in _ALL_TABLE_PRIVILEGES
        if conn.execute(
            text("SELECT has_table_privilege(:role, :table, :priv)"),
            {"role": role, "table": f"public.{table}", "priv": priv},
        ).scalar()
    }


def apply_mainai_job_runtime_privileges(engine: Engine, *, require_complete: bool = True) -> None:
    """Applies AND verifies mainai_app's exact runtime privileges on the mainai_job_*
    integrity objects (migration 0027) — call this AFTER apply_rls() on every startup (see
    app/main.py), after both Alembic and scripts/security/ensure_app_role.py have already run (this
    function assumes migration 0027's tables/functions already exist; it does not create
    them — that's the migration's job, and deliberately does not reference the mainai_app
    role at all, so it stays portable to a bare database — see that migration's docstring).

    `engine` is expected to be `app/db.py`'s `migration_engine` — the real superuser/admin
    connection (`DATABASE_URL`, see app/config.py's "two roles by design" comment) — because
    `expected_owner` below is read as `current_user` on that same connection, not hardcoded: a
    correct migration run always creates these tables/functions as whatever role Alembic
    itself connected as, so that connection's own `current_user` is definitionally the right
    answer, on any environment, without this module needing to know the role's literal name.

    Three phases, same transaction (atomic rollback on any failure, including a raised policy
    violation — a partial enforce is never left in place):
    1. Determine `expected_owner` from `current_user` on this connection. Refuses outright if
       that resolves to `mainai_app` itself — the restricted runtime role can never legitimately
       be the connection this function verifies migration output against.
    2. Enforce: idempotently REVOKE the excess table privileges scripts/security/ensure_app_role.py's
       blanket ALL-PRIVILEGES grant left behind, and GRANT EXECUTE on the one function
       mainai_app is actually meant to call.
    3. Verify, against Postgres's own catalogs (not just "did step 2's statements not error"):
       - Each of the three tables (`_MAINAI_JOB_TABLES`) is owned by exactly `expected_owner`,
         and specifically never by `mainai_app` — a SECURITY DEFINER function that deletes
         from these tables is only as trustworthy as the tables' own ownership.
       - mainai_app's *effective* privileges on the two locked-down tables (via
         `has_table_privilege`, catching indirect/role-membership grants, not just direct
         ones) match `_MAINAI_JOB_EVENT_TABLE_ALLOWED_PRIVILEGES`/
         `_MAINAI_JOB_PROPOSAL_TABLE_ALLOWED_PRIVILEGES` exactly.
       - For every function in `_MAINAI_JOB_FUNCTION_SPECS`: exactly one overload exists in the
         public schema (no unexpected second overload with a different signature hiding
         alongside it), its exact argument signature (`pg_get_function_identity_arguments`, not
         just an argument *count*) and return type match, it is `SECURITY DEFINER`, `plpgsql`,
         `search_path=pg_catalog`, its owner is exactly `expected_owner` (never `mainai_app`),
         that owner actually holds `SUPERUSER` or `BYPASSRLS` (a SECURITY DEFINER function
         whose owner *isn't* privileged enough to actually bypass the FORCE RLS it's meant to
         operate under is a sign of ownership drift, not a working privileged function), PUBLIC
         holds no EXECUTE, and mainai_app's *effective* EXECUTE (`has_function_privilege`,
         same indirect-grant coverage as the table check) matches exactly what that function is
         supposed to grant it.

    require_complete=True (the default, and what app/main.py's real startup call uses) raises
    RuntimeError if anything doesn't match — inside the same transaction as the REVOKE/GRANT
    statements above, so a policy violation rolls back any partial change rather than leaving
    the database in a half-applied state. require_complete=False logs a warning instead of
    raising, for a caller that wants a non-fatal diagnostic read of current state (e.g. a test
    asserting on the returned drift) rather than enforcement.

    Founder-reported production incident (VPS hotfix, same class of bug as
    backend/scripts/security/s1a_privilege_policy.py's own `acquire_privilege_boot_lock`, see that
    function's docstring for the full "tuple concurrently updated" mechanism): this function is
    only ever called from `app/main.py`'s FastAPI startup handler, which the durable-worker
    container never triggers (`app/worker.py` never imports `app.main` — see
    `test_worker_module_never_imports_app_main` in tests/backend/test_rls_policy_registry.py),
    so it does not race against the worker today. It COULD still race against ITSELF if two
    backend replicas (or an old+new backend briefly overlapping during a rolling deploy) both
    boot at once — the same `pg_advisory_xact_lock` key `apply_privilege_policy()`'s mutating
    path acquires, so a second concurrent backend simply waits for the first's transaction to
    finish rather than racing it on the same catalog rows. The exact two integers must stay in
    sync with `PRIVILEGE_BOOT_ADVISORY_LOCK_KEY` in backend/scripts/security/s1a_privilege_policy.py —
    duplicated here (not imported) because that script deliberately stays outside the `app`
    package so it can run standalone, pre-boot, before `app` itself is fully importable."""
    with engine.begin() as conn:
        # See docstring above — must stay (72197001, 1), identical to
        # backend/scripts/security/s1a_privilege_policy.py's PRIVILEGE_BOOT_ADVISORY_LOCK_KEY.
        conn.execute(text("SELECT pg_advisory_xact_lock(72197001, 1)"))

        expected_owner = conn.execute(text("SELECT current_user")).scalar()

        errors: list[str] = []

        if expected_owner == "mainai_app":
            errors.append(
                "the connection this policy is verifying migration output against is itself "
                "authenticated as mainai_app — refusing to treat the restricted runtime role "
                "as the trusted migration/admin owner"
            )
            message = "mainai job runtime privilege policy violated:\n" + "\n".join(f"  - {e}" for e in errors)
            if require_complete:
                raise RuntimeError(message)
            logger.warning(message)
            return

        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON mainai_job_events FROM mainai_app"))
        conn.execute(text("REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON mainai_job_proposals FROM mainai_app"))
        conn.execute(text("GRANT EXECUTE ON FUNCTION erase_own_mainai_job_children() TO mainai_app"))

        for table in _MAINAI_JOB_TABLES:
            owner = conn.execute(
                text("SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = :t"),
                {"t": table},
            ).scalar()
            if owner != expected_owner:
                errors.append(f"{table}: owned by '{owner}', expected the migration/admin role '{expected_owner}'")
            if owner == "mainai_app":
                errors.append(f"{table}: must never be owned by mainai_app")

        for table, allowed in (
            ("mainai_job_events", _MAINAI_JOB_EVENT_TABLE_ALLOWED_PRIVILEGES),
            ("mainai_job_proposals", _MAINAI_JOB_PROPOSAL_TABLE_ALLOWED_PRIVILEGES),
        ):
            granted = _effective_table_privileges(conn, "mainai_app", table)
            if granted != allowed:
                errors.append(f"{table}: mainai_app effective privileges are {sorted(granted)}, expected exactly {sorted(allowed)}")

        for spec in _MAINAI_JOB_FUNCTION_SPECS:
            name = spec["name"]
            rows = conn.execute(
                text(
                    "SELECT p.oid, pg_catalog.pg_get_function_identity_arguments(p.oid) AS identity_args, "
                    "       pg_catalog.format_type(p.prorettype, NULL) AS rettype, p.prosecdef, "
                    "       p.proconfig, p.proowner::regrole::text AS owner, l.lanname "
                    "FROM pg_proc p JOIN pg_language l ON p.prolang = l.oid "
                    "WHERE p.proname = :name AND p.pronamespace = 'public'::regnamespace"
                ),
                {"name": name},
            ).all()
            if len(rows) != 1:
                errors.append(f"{name}: expected exactly 1 overload in the public schema, found {len(rows)}")
                continue

            row = rows[0]
            if row.identity_args != spec["identity_args"]:
                errors.append(f"{name}: expected signature '({spec['identity_args']})', found '({row.identity_args})'")
            if row.rettype != spec["return_type"]:
                errors.append(f"{name}: expected return type '{spec['return_type']}', found '{row.rettype}'")
            if not row.prosecdef:
                errors.append(f"{name}: expected SECURITY DEFINER, not set")
            if row.lanname != "plpgsql":
                errors.append(f"{name}: expected language plpgsql, found '{row.lanname}'")
            if not row.proconfig or "search_path=pg_catalog" not in row.proconfig:
                errors.append(f"{name}: expected search_path=pg_catalog, found {row.proconfig}")
            if row.owner != expected_owner:
                errors.append(f"{name}: owned by '{row.owner}', expected the migration/admin role '{expected_owner}'")
            if row.owner == "mainai_app":
                errors.append(f"{name}: must never be owned by mainai_app")

            role_priv = conn.execute(
                text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :owner"),
                {"owner": row.owner},
            ).first()
            if role_priv is None or not (role_priv.rolsuper or role_priv.rolbypassrls):
                errors.append(
                    f"{name}: owner '{row.owner}' has neither SUPERUSER nor BYPASSRLS — a SECURITY "
                    "DEFINER function meant to operate under FORCE RLS must be owned by a role that "
                    "can actually do so"
                )

            public_has_execute = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.routine_privileges "
                    "WHERE routine_schema = 'public' AND routine_name = :name "
                    "AND privilege_type = 'EXECUTE' AND grantee = 'PUBLIC'"
                ),
                {"name": name},
            ).first()
            if public_has_execute:
                errors.append(f"{name}: PUBLIC must never hold EXECUTE")

            mainai_app_has_execute = bool(
                conn.execute(
                    text("SELECT has_function_privilege('mainai_app', :oid, 'EXECUTE')"),
                    {"oid": row.oid},
                ).scalar()
            )
            if mainai_app_has_execute != spec["mainai_app_execute"]:
                errors.append(
                    f"{name}: mainai_app effective EXECUTE is {mainai_app_has_execute}, "
                    f"expected {spec['mainai_app_execute']}"
                )

        if errors:
            message = "mainai job runtime privilege policy violated:\n" + "\n".join(f"  - {e}" for e in errors)
            if require_complete:
                raise RuntimeError(message)
            logger.warning(message)


def apply_mainai_execution_privileges(engine: Engine, *, require_complete: bool = True) -> None:
    """Applies AND verifies mainai_app's exact runtime privileges on the MainAI Execution Loop
    V0.1 objects (migration 0032) — same three-phase enforce-then-verify shape as
    apply_mainai_job_runtime_privileges() above (see that function's docstring for the full
    rationale; not repeated here). Call this AFTER apply_mainai_job_runtime_privileges() on
    every startup (see app/main.py) — same advisory lock key, so the two calls never race each
    other or themselves across concurrent backend replicas.

    Deliberately a SEPARATE function/lock-scoped transaction from
    apply_mainai_job_runtime_privileges() rather than folded into it: these are two genuinely
    different migrations' objects (0027 vs 0032), and keeping them separate means a future
    change to one policy's verification logic can never accidentally widen or narrow the
    other's blast radius."""
    with engine.begin() as conn:
        # Same advisory lock key as apply_mainai_job_runtime_privileges() — see that function's
        # docstring for why a shared key across both is correct (they must never race each
        # other, and Postgres advisory locks are re-entrant within the same session/transaction
        # scope is not required here since these run as two separate calls, never nested).
        conn.execute(text("SELECT pg_advisory_xact_lock(72197001, 1)"))

        expected_owner = conn.execute(text("SELECT current_user")).scalar()

        errors: list[str] = []

        if expected_owner == "mainai_app":
            errors.append(
                "the connection this policy is verifying migration output against is itself "
                "authenticated as mainai_app — refusing to treat the restricted runtime role "
                "as the trusted migration/admin owner"
            )
            message = "mainai execution loop privilege policy violated:\n" + "\n".join(f"  - {e}" for e in errors)
            if require_complete:
                raise RuntimeError(message)
            logger.warning(message)
            return

        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON mainai_task_events FROM mainai_app"))
        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON mainai_checkpoints FROM mainai_app"))
        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON mainai_recovery_events FROM mainai_app"))
        for table in (
            "intelligence_executions", "intelligence_evidence", "intelligence_interpretations",
            "intelligence_ideas", "intelligence_idea_links", "intelligence_idea_lessons",
        ):
            conn.execute(text(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON {table} FROM mainai_app"))
        conn.execute(text("REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON active_context_sets FROM mainai_app"))
        conn.execute(text("REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON active_context_members FROM mainai_app"))
        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON active_context_events FROM mainai_app"))
        conn.execute(text("REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON memory_threads FROM mainai_app"))
        conn.execute(text("REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON memory_thread_members FROM mainai_app"))
        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON memory_thread_relationships FROM mainai_app"))
        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON memory_thread_events FROM mainai_app"))
        conn.execute(text("REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON life_intents FROM mainai_app"))
        conn.execute(text("REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON life_intent_blockers FROM mainai_app"))
        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON life_intent_dependencies FROM mainai_app"))
        conn.execute(text("REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON life_intent_events FROM mainai_app"))
        for table in ("life_problems", "life_problem_approaches", "life_solution_components", "life_problem_assumptions", "life_problem_decisions"):
            conn.execute(text(f"REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON {table} FROM mainai_app"))
        for table in ("life_component_evaluations", "life_approach_outcomes", "life_problem_lesson_links",
                      "life_solution_selections", "life_solution_component_links", "life_problem_events"):
            conn.execute(text(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON {table} FROM mainai_app"))
        conn.execute(text("REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON work_strategy_executions FROM mainai_app"))
        for table in ("work_strategies", "work_trace_events", "work_efficiency_observations", "work_strategy_findings",
                      "work_verification_obligations", "work_verification_observations", "work_stopping_decisions",
                      "work_specialist_contributions", "work_strategy_lesson_links"):
            conn.execute(text(f"REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON {table} FROM mainai_app"))
        conn.execute(text("GRANT EXECUTE ON FUNCTION erase_own_mainai_execution_children() TO mainai_app"))

        for table in _MAINAI_EXECUTION_TABLES:
            owner = conn.execute(
                text("SELECT tableowner FROM pg_tables WHERE schemaname = 'public' AND tablename = :t"),
                {"t": table},
            ).scalar()
            if owner != expected_owner:
                errors.append(f"{table}: owned by '{owner}', expected the migration/admin role '{expected_owner}'")
            if owner == "mainai_app":
                errors.append(f"{table}: must never be owned by mainai_app")

        for table, allowed in (
            ("mainai_task_events", _MAINAI_EXECUTION_TASK_EVENT_TABLE_ALLOWED_PRIVILEGES),
            ("mainai_checkpoints", _MAINAI_EXECUTION_CHECKPOINT_TABLE_ALLOWED_PRIVILEGES),
            ("mainai_recovery_events", _MAINAI_RECOVERY_EVENT_TABLE_ALLOWED_PRIVILEGES),
            *((table, frozenset({"SELECT", "INSERT"})) for table in (
                "intelligence_executions", "intelligence_evidence", "intelligence_interpretations",
                "intelligence_ideas", "intelligence_idea_links", "intelligence_idea_lessons",
            )),
            ("active_context_sets", frozenset({"SELECT", "INSERT", "UPDATE"})),
            ("active_context_members", frozenset({"SELECT", "INSERT", "UPDATE"})),
            ("active_context_events", frozenset({"SELECT", "INSERT"})),
            ("memory_threads", frozenset({"SELECT", "INSERT", "UPDATE"})),
            ("memory_thread_members", frozenset({"SELECT", "INSERT", "UPDATE"})),
            ("memory_thread_relationships", frozenset({"SELECT", "INSERT"})),
            ("memory_thread_events", frozenset({"SELECT", "INSERT"})),
            ("life_intents", frozenset({"SELECT", "INSERT", "UPDATE"})),
            ("life_intent_blockers", frozenset({"SELECT", "INSERT", "UPDATE"})),
            ("life_intent_dependencies", frozenset({"SELECT", "INSERT"})),
            ("life_intent_events", frozenset({"SELECT", "INSERT"})),
            *((table, frozenset({"SELECT", "INSERT", "UPDATE"})) for table in (
                "life_problems", "life_problem_approaches", "life_solution_components", "life_problem_assumptions", "life_problem_decisions")),
            *((table, frozenset({"SELECT", "INSERT"})) for table in (
                "life_component_evaluations", "life_approach_outcomes", "life_problem_lesson_links",
                "life_solution_selections", "life_solution_component_links", "life_problem_events")),
            ("work_strategy_executions", frozenset({"SELECT", "INSERT", "UPDATE"})),
            *((table, frozenset({"SELECT", "INSERT"})) for table in (
                "work_strategies", "work_trace_events", "work_efficiency_observations", "work_strategy_findings",
                "work_verification_obligations", "work_verification_observations", "work_stopping_decisions",
                "work_specialist_contributions", "work_strategy_lesson_links")),
        ):
            granted = _effective_table_privileges(conn, "mainai_app", table)
            if granted != allowed:
                errors.append(f"{table}: mainai_app effective privileges are {sorted(granted)}, expected exactly {sorted(allowed)}")

        for spec in _MAINAI_EXECUTION_FUNCTION_SPECS:
            name = spec["name"]
            rows = conn.execute(
                text(
                    "SELECT p.oid, pg_catalog.pg_get_function_identity_arguments(p.oid) AS identity_args, "
                    "       pg_catalog.format_type(p.prorettype, NULL) AS rettype, p.prosecdef, "
                    "       p.proconfig, p.proowner::regrole::text AS owner, l.lanname "
                    "FROM pg_proc p JOIN pg_language l ON p.prolang = l.oid "
                    "WHERE p.proname = :name AND p.pronamespace = 'public'::regnamespace"
                ),
                {"name": name},
            ).all()
            if len(rows) != 1:
                errors.append(f"{name}: expected exactly 1 overload in the public schema, found {len(rows)}")
                continue

            row = rows[0]
            if row.identity_args != spec["identity_args"]:
                errors.append(f"{name}: expected signature '({spec['identity_args']})', found '({row.identity_args})'")
            if row.rettype != spec["return_type"]:
                errors.append(f"{name}: expected return type '{spec['return_type']}', found '{row.rettype}'")
            expects_security_definer = spec.get("security_definer", True)
            if row.prosecdef != expects_security_definer:
                errors.append(
                    f"{name}: SECURITY DEFINER is {row.prosecdef}, expected {expects_security_definer}"
                )
            if row.lanname != "plpgsql":
                errors.append(f"{name}: expected language plpgsql, found '{row.lanname}'")
            if not row.proconfig or "search_path=pg_catalog" not in row.proconfig:
                errors.append(f"{name}: expected search_path=pg_catalog, found {row.proconfig}")
            if row.owner != expected_owner:
                errors.append(f"{name}: owned by '{row.owner}', expected the migration/admin role '{expected_owner}'")
            if row.owner == "mainai_app":
                errors.append(f"{name}: must never be owned by mainai_app")

            if expects_security_definer:
                role_priv = conn.execute(
                    text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = :owner"),
                    {"owner": row.owner},
                ).first()
                if role_priv is None or not (role_priv.rolsuper or role_priv.rolbypassrls):
                    errors.append(
                        f"{name}: owner '{row.owner}' has neither SUPERUSER nor BYPASSRLS — a SECURITY "
                        "DEFINER function meant to operate under FORCE RLS must be owned by a role that "
                        "can actually do so"
                    )

            public_has_execute = conn.execute(
                text(
                    "SELECT 1 FROM information_schema.routine_privileges "
                    "WHERE routine_schema = 'public' AND routine_name = :name "
                    "AND privilege_type = 'EXECUTE' AND grantee = 'PUBLIC'"
                ),
                {"name": name},
            ).first()
            if public_has_execute:
                errors.append(f"{name}: PUBLIC must never hold EXECUTE")

            mainai_app_has_execute = bool(
                conn.execute(
                    text("SELECT has_function_privilege('mainai_app', :oid, 'EXECUTE')"),
                    {"oid": row.oid},
                ).scalar()
            )
            if mainai_app_has_execute != spec["mainai_app_execute"]:
                errors.append(
                    f"{name}: mainai_app effective EXECUTE is {mainai_app_has_execute}, "
                    f"expected {spec['mainai_app_execute']}"
                )

        if errors:
            message = "mainai execution loop privilege policy violated:\n" + "\n".join(f"  - {e}" for e in errors)
            if require_complete:
                raise RuntimeError(message)
            logger.warning(message)
