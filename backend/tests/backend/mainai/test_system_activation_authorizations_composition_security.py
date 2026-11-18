"""Deeper, composition-specific security verification for `system_activation_authorizations`
(PR #408), ordered by the founder's own directive ("CONTINUE ACTIVE ENGINEERING", Priority 1):
runtime-role permission denial, the system-wide (not owner-scoped) authorization boundary,
migration upgrade/downgrade behavior, privilege-policy replay idempotency, and no privilege
escalation introduced by composing #408/#403/#411/#437 together on this branch.

NOT a duplicate of `test_system_activation_authorization.py`'s own coverage (INSERT denial,
the dispatch-bypass proof) -- this file adds exactly what that one does not: UPDATE/DELETE
denial, the structural absence of both an `owner_id` column and any RLS policy (verified, not
assumed), a targeted single-migration round trip within the composed chain, privilege-replay
idempotency, and a cross-table check that composing four independent privilege-narrowing
migrations together never widened a privilege on a table none of them owns.

Compatibility with the existing Codex lineage (#412/#413) is NOT a pytest test here, by the
same reasoning PR #417 already established for the original collision evidence: a test that
shells out to another branch mid-CI-run is fragile and becomes stale the moment that branch
changes. Verified instead by direct inspection, reported in this PR's own description and
`docs/BRANCH_REGISTRY.md`: Codex's `0070`-`0074` files use full descriptive revision strings
(e.g. `"0070_blueprint_drafts"`), never bare `"0070"`/`"0071"`/`"0072"` -- the ONLY actual
string collision across both chains is the single, already-known, already-documented `"0069"`
itself."""

import importlib.util
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.db import migration_engine
from app.models.agent_coordination import SystemActivationAuthorization
from app.rls import apply_mainai_execution_privileges

_APPLY_RUNTIME_PRIVILEGES_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "security" / "apply_runtime_privileges.py"
)

# Privilege sets a few representative, ALREADY-established, privilege-narrowed tables (none
# of them owned by any of #408/#403/#411/#437) are expected to keep completely unchanged --
# the "no privilege escalation introduced by composition" proof. If composing these four
# branches' independent `REVOKE ALL ON TABLE <their own table>` statements ever accidentally
# widened to a blanket schema-level REVOKE/GRANT (which none of them actually issue -- each
# targets only its own table by name, verified by reading every statement), one of these
# would be the first thing to show it.
_UNRELATED_PREEXISTING_PRIVILEGE_FLOORS = {
    "work_candidates": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "document_source_units": frozenset({"SELECT", "INSERT"}),
    "message_source_units": frozenset({"SELECT", "INSERT"}),
}


def _apply_and_verify_runtime_privileges() -> None:
    apply_mainai_execution_privileges(migration_engine)
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.apply_and_verify(get_settings().database_url)


def _mainai_app_privileges(conn, table: str) -> set[str]:
    rows = conn.execute(
        sa_text(
            "SELECT privilege_type FROM information_schema.role_table_grants "
            "WHERE grantee = 'mainai_app' AND table_schema = 'public' AND table_name = :table"
        ),
        {"table": table},
    ).all()
    return {privilege for (privilege,) in rows}


# ============================================================================ runtime role
# permissions: INSERT is already proven denied elsewhere; UPDATE/DELETE are new coverage here.


def test_mainai_app_role_cannot_update_a_system_activation_authorization(superuser_db, db_session):
    """Seeds a row via the superuser connection (the only sanctioned, out-of-band way one is
    ever created) then proves the restricted runtime role -- the one every real request/worker
    actually uses -- cannot UPDATE it, not just cannot INSERT."""
    row = SystemActivationAuthorization(authorized_by="test-admin", reason="seed for update-denial test")
    superuser_db.add(row)
    superuser_db.commit()

    with pytest.raises(Exception):  # noqa: B017 -- exact exception varies by driver, same as every sibling REVOKE-rejection test
        db_session.execute(
            sa_text("UPDATE system_activation_authorizations SET reason = 'tampered' WHERE id = :id"),
            {"id": row.id},
        )
        db_session.commit()
    db_session.rollback()


def test_mainai_app_role_cannot_delete_a_system_activation_authorization(superuser_db, db_session):
    row = SystemActivationAuthorization(authorized_by="test-admin", reason="seed for delete-denial test")
    superuser_db.add(row)
    superuser_db.commit()

    with pytest.raises(Exception):  # noqa: B017
        db_session.execute(sa_text("DELETE FROM system_activation_authorizations WHERE id = :id"), {"id": row.id})
        db_session.commit()
    db_session.rollback()


# ============================================================================ system-wide, not
# owner-scoped: verified structurally (schema), not just by trusting the model/docstring.


def test_system_activation_authorizations_has_no_owner_id_column():
    """The model's own docstring says "deliberately NOT owner-scoped" -- confirmed here against
    the real Postgres catalog, not the ORM class, so a future migration that quietly added an
    owner_id column without updating the docstring would be caught."""
    migration_engine.dispose()
    with migration_engine.connect() as conn:
        columns = {
            name
            for (name,) in conn.execute(
                sa_text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = 'system_activation_authorizations'"
                )
            ).all()
        }
    assert "owner_id" not in columns, "system_activation_authorizations must stay founder-wide, never owner-scoped"


def test_system_activation_authorizations_has_no_row_level_security_at_all():
    """Distinguishes "RLS never enabled" from "RLS enabled with zero policies" -- the same
    rigor test_migration_roundtrip.py's own RLS fingerprint already applies. A table with RLS
    silently enabled-but-policy-less would default-deny every row to every role including
    mainai_app's SELECT, which would look like safety but would actually just be a different,
    undocumented behavior change from what this table's own design intends."""
    migration_engine.dispose()
    with migration_engine.connect() as conn:
        enabled, forced = conn.execute(
            sa_text(
                "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relname = 'system_activation_authorizations'"
            )
        ).one()
        policy_count = conn.execute(
            sa_text(
                "SELECT count(*) FROM pg_policies "
                "WHERE schemaname = 'public' AND tablename = 'system_activation_authorizations'"
            )
        ).scalar_one()
    assert not enabled and not forced and policy_count == 0, (
        f"expected no RLS at all (enabled={enabled}, forced={forced}, policies={policy_count})"
    )


def test_current_system_activation_is_global_regardless_of_app_current_user_id(superuser_db, db_session):
    """The real security property this table relies on instead of RLS: `current_system_
    activation()` must return the SAME row regardless of which owner the calling session's
    `app.current_user_id` is set to -- proving the boundary is genuinely system-wide, not an
    RLS gap that happens to leak a row across tenants. Seeds via the superuser connection,
    reads via the RESTRICTED runtime-role session two different simulated owners would use."""
    from app.agent_coordination.system_activation import current_system_activation

    row = SystemActivationAuthorization(authorized_by="test-admin", reason="global-visibility test")
    superuser_db.add(row)
    superuser_db.commit()

    for simulated_owner in (str(uuid.uuid4()), str(uuid.uuid4())):
        db_session.execute(sa_text("SELECT set_config('app.current_user_id', :v, false)"), {"v": simulated_owner})
        found = current_system_activation(db_session)
        assert found is not None and found.id == row.id, (
            f"system-wide authorization evidence must be visible regardless of app.current_user_id "
            f"(simulated_owner={simulated_owner}); current_system_activation() returned {found!r}"
        )


# ============================================================================ migration
# upgrade/downgrade, targeted at this one revision within the composed chain.


def test_migration_0075_system_activation_authorization_downgrade_and_upgrade_round_trip():
    """A targeted, single-step version of test_migration_roundtrip.py's own whole-chain
    property, specifically for this migration within the 0069-composition chain: downgrading
    past it must actually drop the table (and its privilege grants with it, since REVOKE
    statements live in apply_mainai_execution_privileges(), re-run at every boot -- not in the
    migration itself, per this migration's own docstring on why), and re-upgrading must restore
    it with the exact same column set."""
    import subprocess
    import sys

    from sqlalchemy import inspect

    backend_root = Path(__file__).resolve().parent.parent.parent.parent

    def _run(*args: str) -> None:
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=backend_root, check=True)

    migration_engine.dispose()
    before_columns = {col["name"] for col in inspect(migration_engine).get_columns("system_activation_authorizations")}

    try:
        _run("downgrade", "0068")
        migration_engine.dispose()
        assert "system_activation_authorizations" not in inspect(migration_engine).get_table_names(), (
            "downgrading past migration 0069 must actually drop system_activation_authorizations"
        )

        _run("upgrade", "head")
        migration_engine.dispose()
        after_columns = {col["name"] for col in inspect(migration_engine).get_columns("system_activation_authorizations")}
        assert after_columns == before_columns, "upgrading back to head must restore the exact same column set"

        # The REVOKE lives in apply_mainai_execution_privileges(), applied at boot -- not
        # restored automatically by the migration itself. Re-apply it the same way boot does,
        # then confirm the SELECT-only floor is back exactly as it was before the round trip.
        apply_mainai_execution_privileges(migration_engine)
        with migration_engine.connect() as conn:
            restored_privileges = _mainai_app_privileges(conn, "system_activation_authorizations")
        assert restored_privileges == {"SELECT"}, (
            f"expected mainai_app to hold only SELECT after re-applying the privilege policy "
            f"post-round-trip, found {sorted(restored_privileges)}"
        )
    except Exception:
        # Best-effort recovery, same discipline test_migration_roundtrip.py's own tests use --
        # a failure here must not leave every later test in the session failing on missing
        # tables for an unrelated, confusing reason.
        _run("upgrade", "head")
        apply_mainai_execution_privileges(migration_engine)
        raise


# ============================================================================ privilege-policy
# replay idempotency and no escalation on tables this composition round does not own.


def test_privilege_policy_replay_is_idempotent_no_escalation_across_repeated_applications():
    """Runs the SAME privilege policies every test module in this session already calls once,
    twice in a row -- proving a second application never widens (or narrows) any of the four
    composed tables' grants. A policy that accidentally re-GRANTed something on replay (e.g.
    a missing `REVOKE ALL` before the targeted re-GRANT) would show up as a growing privilege
    set on the second pass; this asserts the two passes are byte-for-byte identical."""
    _apply_and_verify_runtime_privileges()
    with migration_engine.connect() as conn:
        first_pass = {
            table: _mainai_app_privileges(conn, table)
            for table in ("system_activation_authorizations", "github_evidence_candidates", "calendar_events", "repository_file_source_units")
        }

    _apply_and_verify_runtime_privileges()
    with migration_engine.connect() as conn:
        second_pass = {
            table: _mainai_app_privileges(conn, table)
            for table in ("system_activation_authorizations", "github_evidence_candidates", "calendar_events", "repository_file_source_units")
        }

    assert first_pass == second_pass, f"privilege replay must be idempotent: {first_pass} != {second_pass}"


def test_composing_the_four_0069_candidates_does_not_widen_privileges_on_unrelated_tables():
    """None of #408/#403/#411/#437's own privilege-narrowing statements target anything but
    their own single table by name (verified by reading every REVOKE/GRANT in migration 0069
    and app/rls.py directly) -- this is the empirical proof: a few representative, already-
    narrowed, UNRELATED tables must still show exactly their own pre-existing, already-
    established floor after the composed chain's privilege policies have run, not something
    wider (or narrower) picked up as a side effect of composition."""
    _apply_and_verify_runtime_privileges()
    with migration_engine.connect() as conn:
        for table, expected in _UNRELATED_PREEXISTING_PRIVILEGE_FLOORS.items():
            actual = _mainai_app_privileges(conn, table)
            assert actual == set(expected), f"{table}: expected unchanged privileges {sorted(expected)}, found {sorted(actual)}"
