"""Examiner-only security examination of the EXISTING coordination store on base 2fbe20a.

DB-level tests attack as the real runtime role (`mainai_app`, backend `db_session` fixture) with raw SQL and
report whether the DATABASE ITSELF prevents each violation. SVC tests exercise `app.agent_coordination.service`
to show which properties are service-only, including a real two-session concurrency race.
Each test prints a matrix line: `MATRIX|<id>|<layer>|PASS/FAIL|<evidence>` and never raises on FAIL
(so the whole matrix is always produced). Probe-integrity: unexpected SQL errors are reported as PROBE_ERROR.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

DENIALS = ("InsufficientPrivilege", "permission denied", "RaiseException", "CheckViolation", "UniqueViolation",
           "ForeignKeyViolation", "ExclusionViolation", "row-level security", "append-only", "cannot create")


def row(pid, layer, ok, evidence):
    print(f"\nMATRIX|{pid}|{layer}|{'PASS' if ok else 'FAIL'}|{evidence}")


def _ctx(db, owner):
    db.execute(sa_text("SELECT set_config('app.current_user_id', :o, true)"), {"o": str(owner)})


def attempt(db, owner, sql, params=None):
    """Run as runtime role in owner context. Returns ('DENIED'|'ALLOWED'|'PROBE_ERROR', detail)."""
    _ctx(db, owner)
    try:
        r = db.execute(sa_text(sql), params or {})
        db.flush()
        out = ("ALLOWED", f"rows={r.rowcount}") if (r.rowcount or 0) > 0 else ("PROBE_ERROR", "0 rows matched")
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).splitlines()[0][:110]
        out = ("DENIED", msg) if any(d in msg for d in DENIALS) else ("PROBE_ERROR", msg)
    db.rollback()
    return out


# ------------------------------------------------------------------------------------------ seeding
@pytest.fixture
def world(superuser_db, make_verified_user):
    owner, _ = make_verified_user()
    o = str(owner.id)
    s = superuser_db
    ids = {k: str(uuid.uuid4()) for k in ("goal", "plan", "task1", "task2", "agentA", "agentB", "asgA", "asgB", "asgC", "leaseA", "job")}
    now = datetime.utcnow()
    s.execute(sa_text("SELECT set_config('app.current_user_id', :o, true)"), {"o": o})
    s.execute(sa_text("INSERT INTO mainai_goals (id, owner_id, title, original_instruction, created_at, created_by) VALUES (:g,:o,'g','g',:n,'probe')"), {"g": ids["goal"], "o": o, "n": now})
    s.execute(sa_text("INSERT INTO mainai_plans (id, goal_id, owner_id, version, rationale, created_at, created_by) VALUES (:p,:g,:o,1,'r',:n,'probe')"), {"p": ids["plan"], "g": ids["goal"], "o": o, "n": now})
    for t in ("task1", "task2"):
        s.execute(sa_text("INSERT INTO mainai_tasks (id, goal_id, plan_id, owner_id, description, task_type, created_at) VALUES (:t,:g,:p,:o,'d','code_change',:n)"),
                  {"t": ids[t], "g": ids["goal"], "p": ids["plan"], "o": o, "n": now})
    for a, key in (("agentA", "probe-a-"), ("agentB", "probe-b-")):
        s.execute(sa_text("INSERT INTO coordination_agents (id, agent_key, display_name, adapter_kind, supports_write, concurrency_limit) VALUES (:i,:k,'x','cli',true,1)"),
                  {"i": ids[a], "k": key + uuid.uuid4().hex[:8]})

    def asg(i, agent, task, status="running", rw="read_write", role="builder"):
        s.execute(sa_text("INSERT INTO agent_work_assignments (id, owner_id, goal_id, task_id, agent_id, role, read_write_mode, repository_identity, "
                          "allowed_paths, status, canonical_work_key, requested_by, branch, worktree_path) VALUES "
                          "(:i,:o,:g,:t,:a,:role,:rw,'d1n095/LifeAI','[\"backend/app\"]',:st,:k,'probe',:b,:w)"),
                  {"i": i, "o": o, "g": ids["goal"], "t": task, "a": agent, "role": role, "rw": rw, "st": status, "k": f"k-{i}",
                   "b": f"br-{i[:6]}", "w": f"/wt/{i[:6]}"})
    asg(ids["asgA"], ids["agentA"], ids["task1"])
    asg(ids["asgB"], ids["agentB"], ids["task2"], status="planned")
    asg(ids["asgC"], ids["agentB"], ids["task2"], status="planned")
    s.execute(sa_text("INSERT INTO agent_scope_leases (id, owner_id, assignment_id, agent_id, repository_identity, branch, worktree_path, allowed_paths, mode, lease_generation, status) "
                      "VALUES (:l,:o,:a,:ag,'d1n095/LifeAI','feature/x','/worktrees/shared','[\"backend/app\"]','read_write',2,'active')"),
              {"l": ids["leaseA"], "o": o, "a": ids["asgA"], "ag": ids["agentA"]})
    s.execute(sa_text("INSERT INTO mainai_jobs (id, owner_id, job_type, created_at, created_by, status, started_at, lease_generation, lease_expires_at) "
                      "VALUES (:j,:o,'corpus_review',:n,'probe','running',:n,2,:e)"), {"j": ids["job"], "o": o, "n": now, "e": now + timedelta(minutes=5)})
    s.commit()
    ids["owner"] = owner.id
    return ids


def lease_sql(asg, agent, wt="/worktrees/shared", branch="feature/y", repo="d1n095/LifeAI", paths='["docs"]'):
    return ("INSERT INTO agent_scope_leases (owner_id, assignment_id, agent_id, repository_identity, branch, worktree_path, allowed_paths, mode, status) "
            f"VALUES (:o, '{asg}', '{agent}', '{repo}', '{branch}', '{wt}', '{paths}', 'read_write', 'active')")


# ------------------------------------------------------------------------------------------ 1/2 worktree
@pytest.mark.parametrize("pid,wt,repo,label", [
    ("1", "/worktrees/shared", "d1n095/LifeAI", "same canonical worktree, other agent"),
    ("2a", "/worktrees/shared/", "d1n095/LifeAI", "trailing-slash alias"),
    ("2b", "/worktrees//shared", "d1n095/LifeAI", "double-slash alias"),
    ("2c", "/worktrees/x/../shared", "d1n095/LifeAI", "dot-dot alias"),
    ("2d", "/worktrees/shared/backend", "d1n095/LifeAI", "nested directory inside owned worktree"),
    ("2e", "/worktrees/shared", "https://github.com/d1n095/LifeAI", "same worktree under repo-identity alias"),
])
def test_db_worktree_exclusivity(db_session, world, pid, wt, repo, label):
    st, d = attempt(db_session, world["owner"], lease_sql(world["asgB"], world["agentB"], wt=wt, repo=repo), {"o": str(world["owner"])})
    row(pid, "DB", st == "DENIED", f"{label}: second active read_write lease -> {st} {d}")


def test_db_task_worktree_table_path_uniqueness(db_session, world, superuser_db):
    o = str(world["owner"])
    jobs = []
    for _ in range(2):
        j = str(uuid.uuid4())
        superuser_db.execute(sa_text("INSERT INTO mainai_jobs (id, owner_id, job_type, created_at, created_by) VALUES (:j,:o,'corpus_review',now(),'probe')"), {"j": j, "o": o})
        jobs.append(j)
    superuser_db.commit()
    sql = ("INSERT INTO mainai_task_worktrees (id, task_id, owner_id, job_id, lease_generation, executor_id, repo, base_sha, branch, path, marker_token, created_at) "
           "VALUES (gen_random_uuid(), :t, :o, :j, 1, 'e', 'd1n095/LifeAI', :sha, :b, '/worktrees/same', 'm', now())")
    st1, _ = attempt(db_session, world["owner"], sql, {"t": world["task1"], "o": o, "j": jobs[0], "sha": "a" * 40, "b": "claude/mainai-task-1"})
    # second insert in a fresh attempt needs the first committed: do both in one transaction
    _ctx(db_session, world["owner"])
    try:
        db_session.execute(sa_text(sql), {"t": world["task1"], "o": o, "j": jobs[0], "sha": "a" * 40, "b": "claude/mainai-task-1"})
        db_session.execute(sa_text(sql), {"t": world["task2"], "o": o, "j": jobs[1], "sha": "a" * 40, "b": "claude/mainai-task-2"})
        db_session.flush()
        st, d = "ALLOWED", "two ACTIVE mainai_task_worktrees rows with identical path"
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).splitlines()[0][:100]
        st, d = ("DENIED" if any(x in msg for x in DENIALS) else "PROBE_ERROR"), msg
    db_session.rollback()
    row("1b", "DB", st == "DENIED", f"mainai_task_worktrees (0033): {d} (first insert alone: {st1})")


# ------------------------------------------------------------------------------------------ 3/4 assignments
def test_db_duplicate_active_assignment_per_task(db_session, world):
    st, d = attempt(db_session, world["owner"], "UPDATE agent_work_assignments SET status='running' WHERE id IN (:b,:c)", {"b": world["asgB"], "c": world["asgC"]})
    row("3", "DB", st == "DENIED", f"two RUNNING assignments for the same task -> {st} {d}")


def test_db_agent_concurrency_limit(db_session, world):
    st, d = attempt(db_session, world["owner"], "UPDATE agent_work_assignments SET status='running', agent_id=:a WHERE id=:b", {"a": world["agentA"], "b": world["asgB"]})
    row("4", "DB", st == "DENIED", f"agent with concurrency_limit=1 gets a 2nd RUNNING assignment -> {st} {d}")


# ------------------------------------------------------------------------------------------ 5 fencing
def test_db_stale_generation_cannot_release_or_regress_lease(db_session, world):
    st, d = attempt(db_session, world["owner"], "UPDATE agent_scope_leases SET status='released', released_at=now() WHERE id=:l", {"l": world["leaseA"]})
    row("5a", "DB", st == "DENIED", f"raw release of a generation-2 lease without presenting the generation -> {st} {d}")
    st, d = attempt(db_session, world["owner"], "UPDATE agent_scope_leases SET lease_generation=1 WHERE id=:l", {"l": world["leaseA"]})
    row("5b", "DB", st == "DENIED", f"lease_generation regressed 2->1 -> {st} {d}")


def test_db_stale_job_generation_cannot_finalize(db_session, world):
    st, d = attempt(db_session, world["owner"], "UPDATE mainai_jobs SET status='completed', completed_at=now() WHERE id=:j", {"j": world["job"]})
    row("5c", "DB", st == "DENIED", f"mainai_jobs finalized by a writer that never presented lease_generation -> {st} {d}")
    st, d = attempt(db_session, world["owner"], "UPDATE mainai_jobs SET lease_generation=1 WHERE id=:j", {"j": world["job"]})
    row("5d", "DB", st == "DENIED", f"mainai_jobs lease_generation regressed 2->1 -> {st} {d}")


# ------------------------------------------------------------------------------------------ 6 dependencies
def test_db_dependency_rules(db_session, world, superuser_db):
    o = str(world["owner"])
    superuser_db.execute(sa_text("UPDATE agent_work_assignments SET status='failed', completed_at=now() WHERE id=:a"), {"a": world["asgA"]})
    superuser_db.execute(sa_text("INSERT INTO agent_work_assignment_dependencies (assignment_id, depends_on_assignment_id, owner_id) VALUES (:b,:a,:o)"),
                         {"b": world["asgB"], "a": world["asgA"], "o": o})
    superuser_db.commit()
    st, d = attempt(db_session, world["owner"], "UPDATE agent_work_assignments SET status='running' WHERE id=:b", {"b": world["asgB"]})
    row("6a", "DB", st == "DENIED", f"assignment started while its dependency FAILED -> {st} {d}")
    st, d = attempt(db_session, world["owner"], "INSERT INTO agent_work_assignment_dependencies (assignment_id, depends_on_assignment_id, owner_id) VALUES (:a,:b,:o)",
                    {"a": world["asgA"], "b": world["asgB"], "o": o})
    row("6b", "DB", st == "DENIED", f"dependency cycle A->B->A inserted -> {st} {d}")
    st, d = attempt(db_session, world["owner"], "INSERT INTO agent_work_assignment_dependencies (assignment_id, depends_on_assignment_id, owner_id) VALUES (:a,:a,:o)",
                    {"a": world["asgC"], "o": o})
    row("6c", "DB", st == "DENIED", f"self-dependency -> {st} {d}")


# ------------------------------------------------------------------------------------------ 7 examined / frozen
def test_db_builder_on_examined_candidate(db_session, world, superuser_db):
    o = str(world["owner"])
    superuser_db.execute(sa_text("UPDATE agent_work_assignments SET role='reviewer', read_write_mode='read_only', status='reviewing', head_sha=:s, branch='feature/x' WHERE id=:a"),
                         {"a": world["asgA"], "s": "e" * 40})
    superuser_db.execute(sa_text("UPDATE agent_scope_leases SET mode='read_only' WHERE id=:l"), {"l": world["leaseA"]})
    superuser_db.commit()
    st, d = attempt(db_session, world["owner"], lease_sql(world["asgB"], world["agentB"], wt="/worktrees/builder", branch="feature/x", paths='["backend/app"]'), {"o": o})
    row("7", "DB", st == "DENIED", f"read_write builder lease on branch currently under read_only examination -> {st} {d}")
    st, d = attempt(db_session, world["owner"], "UPDATE agent_work_assignments SET read_write_mode='read_write' WHERE id=:a", {"a": world["asgA"]})
    row("7b", "DB", st == "DENIED", f"reviewer assignment flipped to read_write -> {st} {d}")


# ------------------------------------------------------------------------------------------ 8 verdicts
def test_db_verification_registry(db_session, world, superuser_db):
    o = str(world["owner"])
    ins = ("INSERT INTO mainai_verification_records (owner_id, component_id, candidate_id, candidate_sha, builder_identity, examiner_identity, review_result, evidence_summary) "
           "VALUES (:o,'grandmaster','c1',:s,:b,:e,'PASS','x')")
    st, d = attempt(db_session, world["owner"], ins, {"o": o, "s": "e" * 40, "b": "codex", "e": "claude"})
    row("8a", "DB", st == "DENIED", f"runtime role inserts a PASS verdict -> {st} {d}")
    superuser_db.execute(sa_text(ins), {"o": o, "s": "e" * 40, "b": "codex", "e": "claude"})
    superuser_db.commit()
    for pid, sql in (("8b", "UPDATE mainai_verification_records SET review_result='FAIL' WHERE owner_id=:o"),
                     ("8c", "DELETE FROM mainai_verification_records WHERE owner_id=:o")):
        st, d = attempt(db_session, world["owner"], sql, {"o": o})
        row(pid, "DB", st == "DENIED", f"runtime role {'rewrites' if pid == '8b' else 'deletes'} a verdict -> {st} {d}")
    for pid, b, e in (("8d", "codex", "codex"), ("8e", "codex", "Codex"), ("8f", "codex", "codex "), ("8g", "codex", "CODEX")):
        try:
            superuser_db.execute(sa_text(ins), {"o": o, "s": "f" * 40, "b": b, "e": e})
            superuser_db.flush()
            st, d = "ALLOWED", "privileged writer recorded it"
        except Exception as exc:  # noqa: BLE001
            st, d = "DENIED", str(exc).splitlines()[0][:80]
        superuser_db.rollback()
        row(pid, "DB", st == "DENIED", f"self-certification builder={b!r} examiner={e!r} (privileged writer path) -> {st} {d}")
    g = superuser_db.execute(sa_text("SELECT string_agg(privilege_type, ',' ORDER BY privilege_type) FROM information_schema.role_table_grants "
                                     "WHERE grantee='mainai_app' AND table_name='mainai_verification_records'")).scalar()
    row("8h", "DB", g == "SELECT", f"runtime-role grants on registry = {g} (designed: SELECT only)")


# ------------------------------------------------------------------------------------------ SERVICE layer
def _load(s, model, i):
    return s.get(model, uuid.UUID(i))


def test_svc_sequential_conflict_and_alias(world):
    from app.agent_coordination.service import acquire_lease
    from app.db import SessionLocal
    from app.models.agent_coordination import AgentWorkAssignment
    s = SessionLocal()
    try:
        _ctx(s, world["owner"])
        r = acquire_lease(s, assignment=_load(s, AgentWorkAssignment, world["asgB"]), agent_id=uuid.UUID(world["agentB"]), branch="feature/z",
                          worktree_path="/worktrees/shared", allowed_paths=["docs"], mode="read_write")
        row("S1", "SVC", r.outcome != "ACQUIRED", f"service, same worktree sequentially -> {r.outcome}")
        s.rollback()
        _ctx(s, world["owner"])
        r = acquire_lease(s, assignment=_load(s, AgentWorkAssignment, world["asgB"]), agent_id=uuid.UUID(world["agentB"]), branch="feature/z",
                          worktree_path="/worktrees/shared/", allowed_paths=["docs"], mode="read_write")
        row("S2", "SVC", r.outcome != "ACQUIRED", f"service, trailing-slash alias of an owned worktree -> {r.outcome}")
        s.rollback()
    finally:
        s.close()


def test_svc_stale_generation_release(world):
    from app.agent_coordination.service import LeaseFencingError, release_lease
    from app.db import SessionLocal
    s = SessionLocal()
    try:
        _ctx(s, world["owner"])
        try:
            release_lease(s, lease_id=uuid.UUID(world["leaseA"]), owner_id=world["owner"], agent_id=uuid.UUID(world["agentA"]), lease_generation=1)
            ok = False
        except LeaseFencingError:
            ok = True
        row("S3", "SVC", ok, f"service release with stale generation 1 (current 2) -> {'LeaseFencingError' if ok else 'RELEASED'}")
        s.rollback()
    finally:
        s.close()


def test_svc_concurrent_phantom_race(world, superuser_db):
    """No active lease exists for a fresh repository: two processes acquire the same worktree at once."""
    from app.agent_coordination.service import acquire_lease
    from app.db import SessionLocal
    from app.models.agent_coordination import AgentWorkAssignment
    repo = f"race/{uuid.uuid4().hex[:6]}"
    superuser_db.execute(sa_text("UPDATE agent_work_assignments SET repository_identity=:r WHERE id IN (:b,:c)"), {"r": repo, "b": world["asgB"], "c": world["asgC"]})
    superuser_db.commit()
    results = []
    for trial in range(5):
        barrier, out = threading.Barrier(2), []

        def go(asg, path):
            s = SessionLocal()
            try:
                _ctx(s, world["owner"])
                a = _load(s, AgentWorkAssignment, asg)
                barrier.wait()
                r = acquire_lease(s, assignment=a, agent_id=uuid.UUID(world["agentB"]), branch=f"race-{asg[:4]}-{trial}",
                                  worktree_path=f"/worktrees/race{trial}", allowed_paths=[path], mode="read_write")
                s.commit()
                out.append(r.outcome)
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                out.append(type(exc).__name__)
            finally:
                s.close()
        ts = [threading.Thread(target=go, args=(world["asgB"], "backend/app")), threading.Thread(target=go, args=(world["asgC"], "backend/app/x"))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        results.append(tuple(out))
        superuser_db.execute(sa_text("UPDATE agent_scope_leases SET status='released', released_at=now() WHERE assignment_id IN (:b,:c) AND status='active'"),
                             {"b": world["asgB"], "c": world["asgC"]})
        superuser_db.commit()
    both = sum(1 for r in results if r.count("ACQUIRED") == 2)
    row("S4", "SVC", both == 0, f"concurrent acquire, same worktree + overlapping paths, no pre-existing lease: both ACQUIRED in {both}/5 trials {results}")


def test_svc_race_control_sequential(world, superuser_db):
    """Control for S4: the same two acquisitions run sequentially are refused, so S4 is a genuine race."""
    from app.agent_coordination.service import acquire_lease
    from app.db import SessionLocal
    from app.models.agent_coordination import AgentWorkAssignment
    repo = f"race/{uuid.uuid4().hex[:6]}"
    superuser_db.execute(sa_text("UPDATE agent_work_assignments SET repository_identity=:r WHERE id IN (:b,:c)"), {"r": repo, "b": world["asgB"], "c": world["asgC"]})
    superuser_db.commit()
    outs = []
    for asg, path in ((world["asgB"], "backend/app"), (world["asgC"], "backend/app/x")):
        s = SessionLocal()
        try:
            _ctx(s, world["owner"])
            r = acquire_lease(s, assignment=_load(s, AgentWorkAssignment, asg), agent_id=uuid.UUID(world["agentB"]), branch=f"seq-{asg[:4]}",
                              worktree_path="/worktrees/seq", allowed_paths=[path], mode="read_write")
            s.commit()
            outs.append(r.outcome)
        finally:
            s.close()
    row("S4c", "SVC", outs[1] != "ACQUIRED", f"control: same pair sequentially -> {outs}")


def test_registry_trigger_bypass_surface(superuser_db):
    q = superuser_db.execute(sa_text(
        "SELECT (SELECT string_agg(b.rolname, ',') FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member JOIN pg_roles b ON b.oid=m.roleid WHERE r.rolname='mainai_app'),"
        " (SELECT pg_get_userbyid(relowner) FROM pg_class WHERE relname='mainai_verification_records'),"
        " (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname='mainai_app')")).one()
    row("8i", "DB", not q[0] and q[1] != "mainai_app" and not q[2],
        f"mainai_app memberships={q[0]} table_owner={q[1]} super_or_bypassrls={q[2]} (trigger keyed on session_user; cannot be dropped/disabled by runtime)")
