"""0069-composition integration branch (`claude/migration-0069-composition-integration`) —
a composed-chain proof for the four independently-developed candidates that each
independently claimed Alembic revision "0069" with down_revision "0068" from the same dev
tip: PR #408 (`system_activation_authorizations`), PR #403 (`github_evidence_candidates`),
PR #411 (`calendar_events`), PR #437 (`repository_file_source_units`). PR #417 already proved
their Postgres objects are fully disjoint, so composing them (renumbered to a single linear
0069->0072 chain on THIS branch only — see each file's own docstring, and
docs/BRANCH_REGISTRY.md, for the composition record) is a pure rename, never a content
reconciliation.

NOT a duplicate of each source PR's own test suite (all four are merged in, unmodified, and
still run their own RLS cross-owner isolation / privilege-denial proofs in their own modules).
This file adds the one thing nothing else checks: that the composed `app/rls.py` (all four
PRs' own additions auto-merged into one shared module — verified by direct inspection, not
assumed, since a silent loss during the merge would be exactly the kind of integration defect
a "smallest set of changes" review is supposed to catch) and
`scripts/security/s1a_privilege_policy.py` produce every one of the four tables' RLS/privilege
state correctly, side by side, in one place — a single point of truth for a reviewer instead of
four separate modules to cross-reference.

Upgrade/downgrade/rollback reversibility for this whole composed chain — downgrading all the
way to base and re-upgrading to head must reproduce byte-for-byte the same schema, covering
every one of these four migrations' upgrade() AND downgrade() at once, not just the newest —
is already covered, migration-agnostically, by
tests/backend/test_migration_roundtrip.py's test_full_migration_chain_downgrades_to_base_and_
back_to_head. Not repeated here, since that test is deliberately migration-agnostic and needs
no changes to exercise this composed chain too.
"""

import importlib.util
from pathlib import Path

from sqlalchemy import text as sa_text

from app.config import get_settings
from app.db import migration_engine
from app.rls import apply_mainai_execution_privileges

_APPLY_RUNTIME_PRIVILEGES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "scripts" / "security" / "apply_runtime_privileges.py"
)

# Expected mainai_app grant set per table, per each source PR's own stated intent:
#   - system_activation_authorizations (PR #408): SELECT only -- no authorize_*/revoke_*
#     function exists anywhere in application code, on purpose; mainai_app can only read.
#   - github_evidence_candidates (PR #403): SELECT/INSERT/UPDATE -- DELETE/TRUNCATE/
#     REFERENCES/TRIGGER revoked; deletion only through the owner-erasure SECURITY DEFINER path.
#   - calendar_events (PR #411): the ordinary SELECT/INSERT/UPDATE/DELETE floor every table
#     gets by default (db-init/01-app-role.sh) -- deliberately NOT narrowed, since a calendar
#     event is ordinary Founder data the Founder must be able to edit or delete outright.
#   - repository_file_source_units (PR #437): SELECT/INSERT only, the same floor
#     document_source_units/message_source_units already use -- every field immutable, delete
#     forbidden outside account erasure.
_EXPECTED_MAINAI_APP_PRIVILEGES = {
    "system_activation_authorizations": frozenset({"SELECT"}),
    "github_evidence_candidates": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "calendar_events": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "repository_file_source_units": frozenset({"SELECT", "INSERT"}),
}


def _apply_both_privilege_policies() -> None:
    # Two separate policy modules, each owning a disjoint subset of these four tables
    # (confirmed by reading both, not assumed): apply_mainai_execution_privileges() (app/rls.py)
    # covers system_activation_authorizations and github_evidence_candidates;
    # s1a_privilege_policy.py's _PROTECTED_TABLES (applied via apply_runtime_privileges.py's
    # apply_and_verify(), the same entry point tests/backend/rag/test_bootstrap_hardening.py's
    # own module-level fixture uses) covers repository_file_source_units. calendar_events needs
    # neither -- it keeps the plain default-ACL floor conftest.py's `_test_database` fixture
    # already grants, on purpose (see _EXPECTED_MAINAI_APP_PRIVILEGES above).
    apply_mainai_execution_privileges(migration_engine)
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.apply_and_verify(get_settings().database_url)


def test_all_four_0069_candidate_tables_exist_with_rls_enabled_and_forced():
    migration_engine.dispose()  # drop pooled connections so the inspector sees the current schema
    with migration_engine.connect() as conn:
        rows = conn.execute(
            sa_text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = ANY(:tables)"
            ),
            {"tables": list(_EXPECTED_MAINAI_APP_PRIVILEGES)},
        ).all()
    found = {name: (enabled, forced) for name, enabled, forced in rows}
    assert set(found) == set(_EXPECTED_MAINAI_APP_PRIVILEGES), (
        "expected all four composed 0069-candidate tables to exist after `alembic upgrade head` "
        f"on this branch; found instead: {sorted(found)}"
    )
    for table, (enabled, forced) in found.items():
        assert enabled and forced, f"{table}: RLS must be ENABLED and FORCED, found enabled={enabled} forced={forced}"


def test_mainai_app_privileges_match_each_source_prs_own_intent_simultaneously():
    """Re-runs the same privilege policies every one of the four source PRs' own test modules
    already calls in isolation -- what's new here is doing so with all four tables present at
    once, proving composition didn't silently widen or narrow any one PR's own intended grant
    set (e.g. one module's REVOKE accidentally re-GRANTing a sibling table, or vice versa)."""
    _apply_both_privilege_policies()
    with migration_engine.connect() as conn:
        rows = conn.execute(
            sa_text(
                "SELECT table_name, privilege_type FROM information_schema.role_table_grants "
                "WHERE grantee = 'mainai_app' AND table_schema = 'public' AND table_name = ANY(:tables)"
            ),
            {"tables": list(_EXPECTED_MAINAI_APP_PRIVILEGES)},
        ).all()
    actual: dict[str, set[str]] = {}
    for table_name, privilege in rows:
        actual.setdefault(table_name, set()).add(privilege)
    for table, expected in _EXPECTED_MAINAI_APP_PRIVILEGES.items():
        assert actual.get(table, set()) == set(expected), (
            f"{table}: expected mainai_app privileges {sorted(expected)}, "
            f"found {sorted(actual.get(table, set()))}"
        )
