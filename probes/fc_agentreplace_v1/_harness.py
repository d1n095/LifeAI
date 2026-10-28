"""Shared helpers for Priority 10 — Multi-Agent Coordination Under Concurrent Execution.

Target: #314 production lineage, product SHA 2308b697c6ffd4e792ffee5acccc4f4659d86389
(`app.agent_coordination` durable coordination implementation).

This suite is distinct from the single-service lifecycle / basic-restart tests: it drives TWO
independent coordination actors against the SAME durable state and measures the multi-instance /
multi-process invariants. Two independent "service instances" are modelled as two independent
SQLAlchemy Sessions on the same engine (separate connections, separate transactions) and/or two
independent `DurableCoordinationService` objects (each its own per-process in-memory state) — the
faithful shape of two MainAI processes sharing one Postgres.

What the durable layer actually is (established by reading the code on this SHA):
  * `agent_work_assignments` / `agent_scope_leases` are REAL shared relational rows. Leases are
    DB-fenced (acquire/renew/take_over use `SELECT ... FOR UPDATE`, a per-assignment partial
    unique index `uq_agent_scope_leases_one_active_per_assignment`, generation + staleness
    checks). Assignment *creation*'s duplicate-work check is a check-then-insert with NO unique
    index backing it (migration 0046 indexes `(owner_id, canonical_work_key)` NON-uniquely).
  * Obligations / handoffs / provider-event dedup live in a per-process in-memory
    `DurableCoordinationService` (`_obligations`, `_by_canonical`, `_seen_event_ids`,
    `_provider_ingestion["processed_event_ids"]`), persisted as ONE wholesale JSON blob into
    `AgentWorkAssignment.provenance["durable_coordination"]` by `persist_owner_ledger` with no
    version / optimistic-concurrency guard.

Read-only examiner. No paid calls; loopback only; target worktree unchanged. first_cycle_go=false.
"""
from __future__ import annotations

import uuid

from sqlalchemy import text


def new_session():
    """An independent coordination 'service instance': its own Session + connection on the
    shared engine. Coordination tables have no RLS, so no GUC context is required."""
    from app.db import migration_engine
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=migration_engine, expire_on_commit=False)()


def make_owner(make_verified_user):
    user, _ = make_verified_user()
    return user.id


def make_goal(db, owner_id, *, title=None):
    from app.mainai_execution import planner

    goal = planner.create_goal(
        db, owner_id=owner_id, title=title or f"coord probe {uuid.uuid4().hex[:8]}",
        original_instruction="Multi-agent coordination examination substrate.", created_by="founder")
    db.flush()
    return goal


def register_coord_agent(db, *, agent_key, write=True):
    from app.agent_coordination.service import register_agent

    return register_agent(db, agent_key=agent_key, display_name=agent_key, adapter_kind="internal",
                          supports_write=write)


def create_assignment(db, *, owner_id, goal_id, agent_id, role="builder", mode="read_write",
                      repo="lifeai", paths=("backend/app/x/**",), task_id=None, group=None):
    from app.agent_coordination.service import create_work_assignment

    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal_id, task_id=task_id, agent_id=agent_id, role=role,
        read_write_mode=mode, repository_identity=repo, allowed_paths=list(paths),
        requested_by="founder", parallel_exploration_group_id=group)


def nonterminal_count_for_key(db, *, owner_id, canonical_work_key):
    from app.agent_coordination.service import TERMINAL_WORK_ASSIGNMENT_STATUSES

    terminal = tuple(s.value for s in TERMINAL_WORK_ASSIGNMENT_STATUSES)
    placeholders = ",".join(f"'{t}'" for t in terminal)
    return db.execute(text(
        f"SELECT count(*) FROM agent_work_assignments WHERE owner_id=:o AND canonical_work_key=:k "
        f"AND status NOT IN ({placeholders})"), {"o": str(owner_id), "k": canonical_work_key}).scalar_one()


def canonical_key(*, goal_id, task_id=None, role="builder", repo="lifeai", paths=("backend/app/x/**",)):
    from app.agent_coordination.service import _canonical_work_key

    return _canonical_work_key(goal_id=goal_id, task_id=task_id, role=role, repository_identity=repo,
                               allowed_paths=list(paths))


def terminal_event_count(db, *, assignment_id, to_status):
    return db.execute(text(
        "SELECT count(*) FROM agent_work_assignment_events WHERE assignment_id=:a "
        "AND event_type='status_changed' AND detail->>'to'=:s"),
        {"a": str(assignment_id), "s": to_status}).scalar_one()


def advance(db, assignment, *statuses):
    """Drive a chain of transitions on one session (e.g. planned->ready->running)."""
    from app.agent_coordination.service import transition_status

    for s in statuses:
        transition_status(db, assignment=assignment, new_status=s)
    db.flush()


def run_barriered(fn_a, fn_b, *, timeout=30):
    """Run two callables as two concurrent 'service instances', each with its OWN session,
    aligned at a 2-party barrier. Each fn(session, barrier) -> value. Returns {name: (ok|err, v)}."""
    import threading

    barrier = threading.Barrier(2)
    results: dict = {}

    def wrap(name, fn):
        s = new_session()
        try:
            results[name] = ("ok", fn(s, barrier))
        except Exception as exc:  # noqa: BLE001 -- record, don't raise across threads
            results[name] = ("err", repr(exc))
        finally:
            s.close()

    ta = threading.Thread(target=wrap, args=("a", fn_a))
    tb = threading.Thread(target=wrap, args=("b", fn_b))
    ta.start(); tb.start(); ta.join(timeout); tb.join(timeout)
    return results


# --------------------------------------------------------------- Priority 11 extras
def create_assignment_with_authority(db, *, owner_id, goal, agent_id, paths=("backend/app/x/**",)):
    """Create an assignment whose provenance pins the goal's instruction authority -- the same
    shape evaluate_assignment_readiness checks (authorized_instruction_sha256)."""
    import hashlib
    from app.agent_coordination.service import create_work_assignment

    sha = hashlib.sha256(goal.original_instruction.encode()).hexdigest()
    return create_work_assignment(
        db, owner_id=owner_id, goal_id=goal.id, task_id=None, agent_id=agent_id, role="builder",
        read_write_mode="read_write", repository_identity="lifeai", allowed_paths=list(paths),
        requested_by="founder", provenance={"authorized_instruction_sha256": sha})


def readiness(db, assignment, *, current_base_sha=None):
    from app.agent_coordination.service import evaluate_assignment_readiness

    return evaluate_assignment_readiness(db, assignment=assignment, current_base_sha=current_base_sha)


def lease_event_details(db, *, assignment_id, event_type):
    from sqlalchemy import text as _t

    rows = db.execute(_t(
        "SELECT detail FROM agent_work_assignment_events WHERE assignment_id=:a AND event_type=:e "
        "ORDER BY created_at"), {"a": str(assignment_id), "e": event_type}).scalars().all()
    return [dict(r) for r in rows]


def assignment_status(db, assignment_id):
    from sqlalchemy import text as _t

    return db.execute(_t("SELECT status FROM agent_work_assignments WHERE id=:i"),
                      {"i": str(assignment_id)}).scalar_one()
