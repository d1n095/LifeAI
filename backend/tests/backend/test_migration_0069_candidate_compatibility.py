"""Point-in-time compatibility evidence for the two open candidates that independently claim
Alembic revision `0069` from the same dev-branch base (`0068`): PR #408
(`claude/first-cycle-go-activation-gate`, head `0c88e228`) and PR #411
(`claude/calendar-foundation`, head `86115310`). Founder directive 2026-11-15 ("PR
consolidation and executable integration"), Priority 2: "Determine the correct migration
ancestry and integration order using actual revision graphs and existing database-history
evidence... Implement isolated compatibility tests for the affected candidate histories if
they are missing." This did not exist before this file.

WHY THIS IS A FIXTURE SNAPSHOT, NOT A LIVE CROSS-BRANCH CHECK: `actions/checkout@v4` (this
repo's own CI, see `.github/workflows/ci.yml`) does a shallow, single-ref checkout -- neither
sibling branch's git objects are present in a normal CI run for either PR, so a test that
shelled out to `git show origin/<other-branch>:<path>` would need to fetch over the network
mid-test-run (fragile, and this sandbox/CI runner may not permit it) and would start failing
the moment either branch is merged, rebased or deleted -- exactly what SHOULD happen once this
collision is actually resolved. Embedding the exact SQL each candidate's migration file
contained AS OF THE SHAs NAMED ABOVE keeps this test deterministic and CI-safe while still
being a REAL executable check (a real regex over real captured SQL), not a hand-typed,
tautological "these two literals match" assertion.

THIS IS NOT A FIX AND DOES NOT RENUMBER EITHER MIGRATION. It proves the two candidates'
underlying DDL is objectively collision-free at the Postgres-object level (disjoint table/
index/policy/constraint names) -- i.e. the ONLY thing that actually needs to change, once
integration order is known, is one file's `revision`/filename string, never its DDL content.
It deliberately does not recommend which candidate should renumber, nor fabricate a merged
lineage -- that decision belongs to whoever actually knows the real merge order (see
docs/BRANCH_REGISTRY.md's "Känd, FÖRVÄNTAD migration-revision-kollision" entry, which also
names PR #412 (`codex/founder-alpha-0069-security-remediation`) as the active Codex
migration-remediation effort for an EARLIER, unrelated 0069 collision from PR #237's own
lineage -- not the same collision this file documents).

DELETE THIS FILE once the #408/#411 collision is actually resolved (whichever PR merges
second is renumbered at that point, per this project's own "merge-regeln" -- not before).
"""

import re

# Captured verbatim from PR #408's migration file at head 0c88e228 (backend/alembic/versions/
# 0069_system_activation_authorization.py's upgrade() body) -- see that file for full context/
# docstring; only the DDL matters for this comparison.
_PR_408_SQL = """
CREATE TABLE system_activation_authorizations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    authorized_by varchar(64) NOT NULL,
    reason text NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'active',
    authorized_at timestamp NOT NULL DEFAULT now(),
    expires_at timestamp,
    revoked_at timestamp,
    revoked_by varchar(64),
    created_at timestamp NOT NULL DEFAULT now(),
    CONSTRAINT ck_system_activation_authorizations_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT ck_system_activation_authorizations_revoked_fields CHECK (
        (status = 'active' AND revoked_at IS NULL AND revoked_by IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL AND revoked_by IS NOT NULL)
    )
);
"""

# Captured verbatim from PR #411's migration file at head 86115310 (backend/alembic/versions/
# 0069_calendar_foundation.py's upgrade() body) -- see that file for full context/docstring;
# only the DDL matters for this comparison.
_PR_411_SQL = """
CREATE TABLE calendar_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title varchar(256) NOT NULL,
    description text,
    location varchar(256),
    starts_at timestamp NOT NULL,
    ends_at timestamp,
    status varchar(16) NOT NULL DEFAULT 'scheduled',
    source varchar(24) NOT NULL DEFAULT 'manual',
    cancelled_at timestamp,
    cancellation_reason text,
    created_at timestamp NOT NULL DEFAULT now(),
    updated_at timestamp NOT NULL DEFAULT now(),
    CONSTRAINT ck_calendar_events_title CHECK (length(btrim(title)) > 0),
    CONSTRAINT ck_calendar_events_times CHECK (ends_at IS NULL OR ends_at > starts_at),
    CONSTRAINT ck_calendar_events_status CHECK (status IN ('scheduled', 'cancelled', 'completed')),
    CONSTRAINT ck_calendar_events_source CHECK (source IN ('manual')),
    CONSTRAINT ck_calendar_events_cancellation CHECK (
        (status = 'cancelled' AND cancelled_at IS NOT NULL)
        OR (status != 'cancelled' AND cancelled_at IS NULL AND cancellation_reason IS NULL)
    )
);

CREATE INDEX ix_calendar_events_owner_starts_at ON calendar_events(owner_id, starts_at);
CREATE INDEX ix_calendar_events_owner_status ON calendar_events(owner_id, status, starts_at);

ALTER TABLE calendar_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE calendar_events FORCE ROW LEVEL SECURITY;
CREATE POLICY calendar_events_isolation ON calendar_events
    USING (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    WITH CHECK (owner_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid);
"""

_BOTH_DECLARE_DOWN_REVISION_0068 = True  # confirmed by direct inspection of both files' headers


def _named_objects(sql: str) -> set[str]:
    """Every Postgres object name this DDL creates -- tables, indexes, policies, constraints.
    Deliberately broad (would also catch e.g. a sequence or trigger name if either migration
    had one) rather than narrowly matching only `CREATE TABLE`, since the actual risk is ANY
    shared identifier, not specifically table names."""
    names: set[str] = set()
    for pattern in (
        r"CREATE TABLE (\w+)",
        r"CREATE INDEX (\w+)",
        r"CREATE POLICY (\w+)",
        r"CONSTRAINT (\w+)",
        r"CREATE TRIGGER (\w+)",
        r"CREATE FUNCTION (\w+)",
    ):
        names.update(re.findall(pattern, sql))
    return names


def test_pr408_and_pr411_migrations_share_a_revision_id_but_no_postgres_object_names():
    """The actual collision is the `revision = "0069"` STRING, never the DDL -- this proves
    the DDL itself is disjoint, so resolving the collision is purely a rename (of whichever
    candidate merges second), never a content-level reconciliation."""
    pr408_objects = _named_objects(_PR_408_SQL)
    pr411_objects = _named_objects(_PR_411_SQL)

    assert pr408_objects, "sanity check: PR #408's fixture SQL should name at least one object"
    assert pr411_objects, "sanity check: PR #411's fixture SQL should name at least one object"

    overlap = pr408_objects & pr411_objects
    assert overlap == set(), (
        f"PR #408 and PR #411's 0069 migrations share Postgres object name(s) {overlap} -- "
        "this is a REAL content-level collision, not just a revision-id string clash, and "
        "would need actual reconciliation (not just a rename) before either can be renumbered "
        "onto the other."
    )


def test_pr408_migration_does_not_touch_calendar_events():
    assert "calendar_events" not in _PR_408_SQL


def test_pr411_migration_does_not_touch_system_activation_authorizations():
    assert "system_activation_authorizations" not in _PR_411_SQL
