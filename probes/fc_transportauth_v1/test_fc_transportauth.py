"""Transport Database Authority — independent adversarial examination.

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298. DB-backed via the target's own
tests.conftest, so every guard/constraint/index/RLS/consume path under test is the REAL
production boundary. Grants are built through the product's own authorize->preflight->grant
chain (see _harness.build_authority_graph), then attacked at the DB level.

Scope discipline: does NOT repeat the completed GitHub publication tests
(test_founder_review_publication / test_publication_readiness) nor the builder's own
authority-state-machine suite (test_activation_authority_substrate, which covers the service
consume/expiry/base-sha paths and the single "terminal push grant state is immutable"
assertion). It targets the unexamined DB trigger / CHECK / unique-index / append-only / RLS
surface for transport effects. (Verified: the push/draft guard messages exercised here, apart
from the one noted above, are asserted nowhere in tests/.)

  TR1  push grant cannot be deleted (runtime privilege-denied AND trigger-blocked)
  TR2  push grant transition must leave 'active' monotonically (guard)
  TR3  push grant authority bindings are immutable (guard, privileged writer)
  TR4  consumption integrity: status='consumed' requires consumed_at + consumed_by_job_id (CHECK)
  TR5  single-use under concurrency: two racing consumes -> exactly one wins (service+DB atomic)
  TR6  at most one ACTIVE grant per task is a DB invariant (partial unique index)
  TR7  draft-PR grant cannot be deleted and its bindings are immutable (dual layer + guard)
  TR8  committed authorization -> remote effect is an append-only, one-way-reconcilable receipt
  TR9  RLS owner isolation: a forged owner context cannot reach another owner's push grant

Read-only examiner. No paid calls, no production mutation, loopback-only. first_cycle_go=false.
"""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import sessionmaker

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    DRAFT_TABLE,
    LOCAL,
    PUSH_TABLE,
    REMOTE_TABLE,
    build_authority_graph,
    make_draft_pr_grant,
    make_push_grant,
    push_status,
    set_runtime_owner,
)


@pytest.fixture
def graph(superuser_db, make_verified_user):
    return build_authority_graph(superuser_db, make_verified_user)


# ---------------------------------------------------------------------------
# TR1 — push grants are auditable: neither the runtime nor a privileged writer may delete one.
# ---------------------------------------------------------------------------
def test_TR1_push_grant_cannot_be_deleted(db_session, superuser_db, graph):
    push = make_push_grant(superuser_db, graph)

    set_runtime_owner(db_session, graph["user"].id)
    with pytest.raises(DBAPIError):                      # DELETE privilege revoked from runtime
        db_session.execute(text(f"DELETE FROM {PUSH_TABLE} WHERE id=:id"), {"id": push.id})
    db_session.rollback()

    with pytest.raises(DBAPIError, match="push grants are auditable and cannot be deleted"):
        superuser_db.execute(text(f"DELETE FROM {PUSH_TABLE} WHERE id=:id"), {"id": push.id})
    superuser_db.rollback()

    assert push_status(superuser_db, push.id) == "active"


# ---------------------------------------------------------------------------
# TR2 — a push grant may only leave 'active' into a terminal state, never stay/return 'active'.
# ---------------------------------------------------------------------------
def test_TR2_push_grant_must_leave_active_monotonically(superuser_db, graph):
    push = make_push_grant(superuser_db, graph)
    # The effective guard (migration 0095 redefinition) phrases this as "invalid push grant
    # transition"; migration 0090/0092 phrased it "must leave active state monotonically".
    # Both reject keeping/returning to 'active'.
    with pytest.raises(DBAPIError, match="invalid push grant transition|must leave active state monotonically"):
        superuser_db.execute(
            text(f"UPDATE {PUSH_TABLE} SET status='active', consumed_at=now() WHERE id=:id"),
            {"id": push.id},
        )
    superuser_db.rollback()
    assert push_status(superuser_db, push.id) == "active"


# ---------------------------------------------------------------------------
# TR3 — the authority bindings of a push grant are immutable even for a privileged writer;
#        only (status, consumed_at, consumed_by_job_id) may change.
# ---------------------------------------------------------------------------
def test_TR3_push_grant_bindings_are_immutable(superuser_db, graph):
    push = make_push_grant(superuser_db, graph)
    with pytest.raises(DBAPIError, match="push grant authority bindings are immutable"):
        superuser_db.execute(
            text(f"UPDATE {PUSH_TABLE} SET status='consumed', consumed_at=now(), "
                 "consumed_by_job_id=:job, branch='refs/heads/attacker' WHERE id=:id"),
            {"job": graph["job"].id, "id": push.id},
        )
    superuser_db.rollback()
    assert push_status(superuser_db, push.id) == "active"


# ---------------------------------------------------------------------------
# TR4 — consumption integrity: 'consumed' requires BOTH consumed_at and consumed_by_job_id
#        (CHECK ck_push_grant_consumption); a legitimate consume with both set succeeds.
# ---------------------------------------------------------------------------
def test_TR4_consumption_requires_job_and_timestamp(superuser_db, graph):
    push = make_push_grant(superuser_db, graph)

    # consumed without the job binding -> CHECK violation
    with pytest.raises(IntegrityError, match="ck_push_grant_consumption"):
        superuser_db.execute(
            text(f"UPDATE {PUSH_TABLE} SET status='consumed', consumed_at=now() WHERE id=:id"),
            {"id": push.id},
        )
    superuser_db.rollback()

    # consumed with both set -> accepted (legitimate terminal transition)
    superuser_db.execute(
        text(f"UPDATE {PUSH_TABLE} SET status='consumed', consumed_at=now(), "
             "consumed_by_job_id=:job WHERE id=:id"),
        {"job": graph["job"].id, "id": push.id},
    )
    superuser_db.commit()
    assert push_status(superuser_db, push.id) == "consumed"


# ---------------------------------------------------------------------------
# TR5 — single-use under concurrency: two racing consume_push_grant calls on the same active
#        grant -> exactly one consumes it, the other fails closed (lost the atomic race).
# ---------------------------------------------------------------------------
def test_TR5_concurrent_consume_is_single_use(superuser_db, graph):
    from app.db import migration_engine
    from app.mainai_execution.activation_security import PushGrantConsumed

    push = make_push_grant(superuser_db, graph)
    grant_id = push.id
    owner_id = graph["user"].id
    job_id = graph["job"].id
    Session = sessionmaker(bind=migration_engine)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def consume_once():
        from app.mainai_execution.activation_security import consume_push_grant
        barrier.wait()
        db = Session()
        try:
            consume_push_grant(db, owner_id=owner_id, grant_id=grant_id, job_id=job_id,
                               local_commit_sha=LOCAL, observed_base_sha="a" * 40,
                               authority_version=7)
            db.commit()
            with lock:
                outcomes.append("consumed")
        except PushGrantConsumed:
            db.rollback()
            with lock:
                outcomes.append("already")
        except Exception as exc:  # noqa: BLE001
            db.rollback()
            with lock:
                outcomes.append(f"other:{exc!r}")
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        for f in [pool.submit(consume_once) for _ in range(2)]:
            f.result(timeout=30)

    assert sorted(outcomes) == ["already", "consumed"], outcomes
    assert push_status(superuser_db, grant_id) == "consumed"


# ---------------------------------------------------------------------------
# TR6 — at most one ACTIVE push grant per task is a DB invariant (partial unique index); a
#        second active grant for the same (owner, task) is rejected by the database.
# ---------------------------------------------------------------------------
def test_TR6_one_active_push_grant_per_task(superuser_db, graph):
    push = make_push_grant(superuser_db, graph)
    with pytest.raises(IntegrityError) as excinfo:
        superuser_db.execute(
            text(
                f"INSERT INTO {PUSH_TABLE} "
                "(id, owner_id, goal_id, task_id, job_id, worktree_id, execution_envelope_id, "
                " preflight_receipt_id, repository_identity, branch, base_branch, base_sha, "
                " local_commit_sha, authority_version, authorized_by, authorized_at, expires_at, "
                " status, idempotency_key, provenance, created_at) "
                f"SELECT gen_random_uuid(), owner_id, goal_id, task_id, job_id, worktree_id, "
                " execution_envelope_id, preflight_receipt_id, repository_identity, branch, "
                " base_branch, base_sha, local_commit_sha, authority_version, authorized_by, "
                " authorized_at, expires_at, 'active', 'dup-'||id, provenance, created_at "
                f"FROM {PUSH_TABLE} WHERE id=:id"
            ),
            {"id": push.id},
        )
    superuser_db.rollback()
    msg = str(excinfo.value)
    assert ("one_active_task" in msg) or ("preflight_once" in msg) or ("duplicate key" in msg), msg


# ---------------------------------------------------------------------------
# TR7 — draft-PR grants are auditable and their bindings immutable (dual layer + guard).
# ---------------------------------------------------------------------------
def test_TR7_draft_pr_grant_delete_and_bindings(db_session, superuser_db, graph):
    draft = make_draft_pr_grant(superuser_db, graph)

    set_runtime_owner(db_session, graph["user"].id)
    with pytest.raises(DBAPIError):                      # runtime cannot DELETE
        db_session.execute(text(f"DELETE FROM {DRAFT_TABLE} WHERE id=:id"), {"id": draft.id})
    db_session.rollback()

    with pytest.raises(DBAPIError, match="draft PR grants cannot be deleted"):
        superuser_db.execute(text(f"DELETE FROM {DRAFT_TABLE} WHERE id=:id"), {"id": draft.id})
    superuser_db.rollback()

    with pytest.raises(DBAPIError, match="draft PR grant bindings are immutable"):
        superuser_db.execute(
            text(f"UPDATE {DRAFT_TABLE} SET status='consumed', consumed_at=now(), "
                 "head_branch='refs/heads/attacker' WHERE id=:id"),
            {"id": draft.id},
        )
    superuser_db.rollback()

    assert superuser_db.execute(
        text(f"SELECT status FROM {DRAFT_TABLE} WHERE id=:id"), {"id": draft.id},
    ).scalar_one() == "active"


# ---------------------------------------------------------------------------
# TR8 — committed authorization -> remote effect is recorded as an APPEND-ONLY receipt:
#        it cannot be deleted, its bindings are immutable, and it reconciles ONE WAY from
#        'claimed' to a terminal observed outcome (after which it is frozen).
# ---------------------------------------------------------------------------
def test_TR8_remote_effect_receipt_is_append_only_one_way(superuser_db, graph):
    push = make_push_grant(superuser_db, graph)
    superuser_db.execute(
        text(
            f"INSERT INTO {REMOTE_TABLE} "
            "(owner_id, grant_id, effect_kind, repository_identity, branch, expected_prior_sha, "
            " exact_commit_sha, worker_id, lease_generation, outcome) "
            "VALUES (:owner, :grant, 'push_branch', :repo, :branch, :prior, :exact, 'worker-1', 1, 'claimed')"
        ),
        {"owner": graph["user"].id, "grant": push.id, "repo": "d1n095/LifeAI",
         "branch": graph["worktree"].branch, "prior": "a" * 40, "exact": LOCAL},
    )
    superuser_db.commit()

    # cannot be deleted
    with pytest.raises(DBAPIError, match="remote effect receipts cannot be deleted"):
        superuser_db.execute(text(f"DELETE FROM {REMOTE_TABLE} WHERE grant_id=:g"), {"g": push.id})
    superuser_db.rollback()

    # bindings immutable (changing the branch is not a reconciliation field)
    with pytest.raises(DBAPIError, match="remote effect bindings are immutable"):
        superuser_db.execute(
            text(f"UPDATE {REMOTE_TABLE} SET branch='refs/heads/x' WHERE grant_id=:g"),
            {"g": push.id},
        )
    superuser_db.rollback()

    # one-way reconciliation from 'claimed' to a terminal observed outcome is allowed
    superuser_db.execute(
        text(f"UPDATE {REMOTE_TABLE} SET outcome='observed_succeeded', observed_sha=:sha "
             "WHERE grant_id=:g"),
        {"sha": LOCAL, "g": push.id},
    )
    superuser_db.commit()
    assert superuser_db.execute(
        text(f"SELECT outcome FROM {REMOTE_TABLE} WHERE grant_id=:g"), {"g": push.id},
    ).scalar_one() == "observed_succeeded"

    # the terminal receipt is now frozen
    with pytest.raises(DBAPIError, match="terminal remote effect receipt is immutable"):
        superuser_db.execute(
            text(f"UPDATE {REMOTE_TABLE} SET outcome='conflict' WHERE grant_id=:g"),
            {"g": push.id},
        )
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# TR9 — RLS owner isolation: a runtime authenticated as owner A cannot reach owner B's push
#        grant (the UPDATE matches zero rows; B's grant is unchanged).
# ---------------------------------------------------------------------------
def test_TR9_forged_owner_context_cannot_cross_owners(db_session, superuser_db, make_verified_user):
    graph_a = build_authority_graph(superuser_db, make_verified_user)
    graph_b = build_authority_graph(superuser_db, make_verified_user)
    push_b = make_push_grant(superuser_db, graph_b)

    set_runtime_owner(db_session, graph_a["user"].id)        # attacker authenticates as A
    result = db_session.execute(
        text(f"UPDATE {PUSH_TABLE} SET status='cancelled' WHERE id=:id"),
        {"id": push_b.id},
    )
    assert result.rowcount == 0                              # RLS hid B's grant from A
    db_session.commit()
    assert push_status(superuser_db, push_b.id) == "active"
