"""Reusable adversarial regression suite — Orchestration Truth Ledger, database layer.

Runs as the real runtime role (`mainai_app` via the backend `db_session` fixture, loaded as a pytest
plugin from OUTSIDE the examined worktree). Seeds with `superuser_db`. Each test asserts the secure
property and fails with "P1 OPEN"/"P2 OPEN" on a vulnerable SHA.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

A = "a" * 40


def require(cond, msg):
    if not cond:
        pytest.fail(msg)


def _ctx(db, owner):
    db.execute(sa_text("SELECT set_config('app.current_user_id', :o, true)"), {"o": str(owner)})


def _try(db, sql, params=None):
    try:
        r = db.execute(sa_text(sql), params or {})
        db.flush()
        return True, r.rowcount
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return False, str(exc).splitlines()[0][:120]


@pytest.fixture
def seeded(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    o = str(owner.id)
    ids = {k: str(uuid.uuid4()) for k in ("agent", "slot", "t_run", "t_frozen", "t_other", "claim", "snap")}
    s = superuser_db
    s.execute(sa_text("SELECT set_config('app.current_user_id', :o, true)"), {"o": o})
    s.execute(sa_text("INSERT INTO orchestration_agents (id, owner_id, agent_key, display_name, occupancy_status, max_slots) VALUES (:a,:o,'codex','Codex','running',1)"), {"a": ids["agent"], "o": o})
    s.execute(sa_text("INSERT INTO orchestration_slots (id, owner_id, agent_id, slot_key, status) VALUES (:s,:o,:a,'default','occupied')"), {"s": ids["slot"], "o": o, "a": ids["agent"]})
    s.execute(sa_text("INSERT INTO orchestration_tasks (id, owner_id, agent_id, assigned_slot_id, title, role, status, working_branch, output_sha, test_runs) "
                      "VALUES (:t,:o,:a,:s,'build','builder','running','codex/x',:sha, :tr)"),
              {"t": ids["t_run"], "o": o, "a": ids["agent"], "s": ids["slot"], "sha": A, "tr": '[{"sha":"%s","passed":10,"failed":0,"source":"pytest_execution"}]' % A})
    s.execute(sa_text("INSERT INTO orchestration_tasks (id, owner_id, title, role, status, frozen, protects_sha, protects_branch) VALUES (:t,:o,'frozen','builder','completed',true,:sha,'codex/frozen')"),
              {"t": ids["t_frozen"], "o": o, "sha": A})
    s.execute(sa_text("INSERT INTO orchestration_tasks (id, owner_id, title, role, status) VALUES (:t,:o,'other','builder','queued')"), {"t": ids["t_other"], "o": o})
    s.execute(sa_text("INSERT INTO orchestration_claims (id, owner_id, task_id, agent_key, claim_kind, raw_text) VALUES (:c,:o,:t,'codex','remote_pushed','pushed')"), {"c": ids["claim"], "o": o, "t": ids["t_run"]})
    s.execute(sa_text("INSERT INTO orchestration_github_snapshots (id, owner_id, task_id, branch, exists_remotely, commit_sha) VALUES (:g,:o,:t,'codex/x',true,:sha)"), {"g": ids["snap"], "o": o, "t": ids["t_run"], "sha": A})
    s.commit()
    return owner.id, ids


def _check(db_session, owner, sql, params, *, label, severity="P1"):
    assert db_session.execute(sa_text("select current_user")).scalar_one() == "mainai_app"
    _ctx(db_session, owner)
    ok, info = _try(db_session, sql, params)
    changed = ok and (info or 0) > 0
    db_session.rollback()
    if not ok:
        denial = any(k in str(info) for k in ("InsufficientPrivilege", "permission denied", "RaiseException", "CheckViolation",
                                               "UniqueViolation", "ForeignKeyViolation", "ExclusionViolation", "row-level security"))
        require(denial, f"PROBE ERROR (not a security denial — fix the probe): {info}")
    if ok and not changed:
        pytest.fail(f"PROBE ERROR: statement matched 0 rows, cannot judge '{label}'")
    require(not changed, f"{severity} OPEN: runtime role could {label} -> rows={info}")


# ---------------------------------------------------------------- truth binding at the DB layer
def test_remote_pushed_cannot_be_set_without_a_matching_github_snapshot(db_session, seeded):
    owner, ids = seeded
    _check(db_session, owner, "UPDATE orchestration_tasks SET remote_pushed=true, remote_sha=:x, last_verified_source='github' WHERE id=:t",
           {"x": "f" * 40, "t": ids["t_run"]}, label="set remote_pushed with no GitHub snapshot for that SHA")


def test_historical_test_evidence_is_not_rewritable(db_session, seeded):
    owner, ids = seeded
    _check(db_session, owner, "UPDATE orchestration_tasks SET test_runs=CAST(:j AS jsonb) WHERE id=:t",
           {"j": '[{"sha": "%s", "passed": 999, "failed": 0, "source": "pytest_execution"}]' % ("f" * 40), "t": ids["t_run"]},
           label="rewrite historical pytest evidence (test_runs)", severity="P2")


def test_frozen_candidate_row_cannot_be_unfrozen(db_session, seeded):
    owner, ids = seeded
    _check(db_session, owner, "UPDATE orchestration_tasks SET frozen=false, protects_sha=NULL WHERE id=:t", {"t": ids["t_frozen"]},
           label="unfreeze a frozen candidate row")


# ---------------------------------------------------------------- occupancy / slots / leases
def test_second_running_task_on_occupied_slot_rejected(db_session, seeded):
    owner, ids = seeded
    _check(db_session, owner, "INSERT INTO orchestration_tasks (owner_id, agent_id, assigned_slot_id, title, role, status) VALUES (:o,:a,:s,'dup','builder','running')",
           {"o": str(owner), "a": ids["agent"], "s": ids["slot"]}, label="put a second RUNNING task on an occupied slot (duplicate job)")


def test_slots_cannot_exceed_max_slots(db_session, seeded):
    owner, ids = seeded
    _check(db_session, owner, "INSERT INTO orchestration_slots (owner_id, agent_id, slot_key, status) VALUES (:o,:a,'extra','occupied')",
           {"o": str(owner), "a": ids["agent"]}, label="create an occupied slot beyond max_slots=1")


def test_slot_cannot_be_freed_while_its_task_runs(db_session, seeded):
    owner, ids = seeded
    _check(db_session, owner, "UPDATE orchestration_slots SET status='free', task_id=NULL WHERE id=:s", {"s": ids["slot"]},
           label="free a slot while its task is still RUNNING")


def test_agent_cannot_be_marked_idle_while_slot_occupied(db_session, seeded):
    owner, ids = seeded
    _check(db_session, owner, "UPDATE orchestration_agents SET occupancy_status='idle' WHERE id=:a", {"a": ids["agent"]},
           label="mark agent idle while it holds an occupied slot / running task")


def test_slots_have_lease_expiry_and_session_identity(superuser_db):
    cols = set(superuser_db.execute(sa_text("SELECT column_name FROM information_schema.columns WHERE table_name='orchestration_slots'")).scalars())
    lease_cols = {c for c in cols if any(k in c for k in ("heartbeat", "lease", "expires", "session", "fencing", "generation"))}
    require(lease_cols, f"P1 OPEN: orchestration_slots has no lease/heartbeat/session/fencing columns ({sorted(cols)}); stale or crashed occupancy is unrepresentable across processes")


# ---------------------------------------------------------------- dependencies
def test_dependency_cycle_rejected(db_session, seeded):
    owner, ids = seeded
    _check(db_session, owner, "INSERT INTO orchestration_task_dependencies (owner_id, task_id, depends_on_task_id) VALUES (:o,:a,:b),(:o,:b,:a)",
           {"o": str(owner), "a": ids["t_run"], "b": ids["t_other"]}, label="insert a dependency cycle", severity="P2")


# ---------------------------------------------------------------- immutable evidence (already-held properties)
@pytest.mark.parametrize("sql,label", [
    ("UPDATE orchestration_claims SET raw_text='edited'", "edit an agent claim"),
    ("DELETE FROM orchestration_claims", "delete an agent claim"),
    ("UPDATE orchestration_github_snapshots SET commit_sha='%s'" % ("f" * 40), "edit a GitHub snapshot"),
    ("DELETE FROM orchestration_github_snapshots", "delete a GitHub snapshot"),
    ("UPDATE orchestration_claims SET authoritative=true", "mark a claim authoritative"),
])
def test_evidence_is_append_only(db_session, seeded, sql, label):
    owner, _ = seeded
    _check(db_session, owner, sql, None, label=label, severity="REGRESSION")


# ---------------------------------------------------------------- cross-owner isolation (already-held)
def test_other_owner_sees_nothing_and_cannot_smuggle(db_session, seeded, make_verified_user):
    owner, ids = seeded
    other, _ = make_verified_user()
    _ctx(db_session, other.id)
    for t in ("orchestration_agents", "orchestration_slots", "orchestration_tasks", "orchestration_claims", "orchestration_github_snapshots"):
        require(db_session.execute(sa_text(f"SELECT count(*) FROM {t}")).scalar_one() == 0, f"REGRESSION: cross-owner read of {t}")
    ok, _ = _try(db_session, "INSERT INTO orchestration_tasks (owner_id, agent_id, title, role) VALUES (:o,:a,'steal','builder')", {"o": str(other.id), "a": ids["agent"]})
    db_session.rollback()
    require(not ok, "REGRESSION: other owner's task bound to this owner's agent (composite FK)")
