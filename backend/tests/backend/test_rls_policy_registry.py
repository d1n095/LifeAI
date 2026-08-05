"""Drift-prevention for app/rls.py's self-heal loop (founder review, round 2 — MEDIUM 4).

apply_rls() re-applies RLS_STATEMENTS (ENABLE/FORCE) AND (re)creates any POLICY_DEFINITIONS
entry missing from pg_policies on every boot. If a table is ENABLE-listed but has no matching
POLICY_DEFINITIONS entry, a lost/dropped policy is never repaired — the table stays FORCE-RLS
with no policy, which is a silent default-deny (not a security hole, but a real availability
gap that this test exists to catch before it ships again for a new table).
"""

import re

from app.rls import POLICY_DEFINITIONS, RLS_STATEMENTS

_ENABLE_RE = re.compile(r"^ALTER TABLE (\w+) ENABLE ROW LEVEL SECURITY$")


def test_policy_definitions_cover_every_rls_enabled_table():
    enabled_tables = {
        match.group(1) for stmt in RLS_STATEMENTS if (match := _ENABLE_RE.match(stmt.strip())) is not None
    }
    assert enabled_tables, "sanity check: RLS_STATEMENTS should not be empty"

    policy_tables = {policy["table"] for policy in POLICY_DEFINITIONS}

    missing = enabled_tables - policy_tables
    assert not missing, (
        f"tables have RLS ENABLEd in RLS_STATEMENTS but no matching POLICY_DEFINITIONS entry: "
        f"{sorted(missing)} — apply_rls()'s self-heal loop can never repair a lost policy for "
        f"these tables (see app/rls.py's module docstring)"
    )


def test_policy_definitions_has_no_orphan_entries():
    """The reverse gap: a POLICY_DEFINITIONS entry for a table RLS_STATEMENTS never enables
    would try to create a policy on a table without RLS enabled at all — not harmful, but a
    sign the two lists have drifted apart and one of them is wrong."""
    enabled_tables = {
        match.group(1) for stmt in RLS_STATEMENTS if (match := _ENABLE_RE.match(stmt.strip())) is not None
    }
    policy_tables = {policy["table"] for policy in POLICY_DEFINITIONS}

    orphans = policy_tables - enabled_tables
    assert not orphans, f"POLICY_DEFINITIONS references tables never ENABLEd in RLS_STATEMENTS: {sorted(orphans)}"
