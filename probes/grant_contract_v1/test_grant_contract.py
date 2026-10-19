"""Grant / remote-effect CONTRACT tests (GC-1..GC-8). Candidate-independent: every API call goes through
grant_adapter.discover(). Assertions are fixed; only the adapter learns new APIs.

  GC-1 one use: a consumed push grant can never authorize a second consume (also across sessions,
       i.e. after a worker crash/restart).
  GC-2 concurrency: two simultaneous consumers -> exactly one succeeds.
  GC-3 expiry: an expired grant is denied.
  GC-4 cancellation: a cancelled grant is denied.
  GC-5 exact binding: job, local commit, observed base SHA and authority version must all match.
  GC-6 distinct effects: a draft-PR effect needs its own grant; a push grant never authorizes it.
  GC-7 no silent re-issue: after consumption a new grant needs a FRESH preflight (spec §4).
  GC-8 typed denial: denials are ExternalBoundaryError subclasses (GrantDeniedError) so the #288+
       execution boundary fails closed without paid retry.
CapabilityMissing -> test FAILS with "BLOCKED:"; AdapterNeeded -> FAILS with "ADAPTER-NEEDED:". Never skipped.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta

import pytest

from app.db import SessionLocal

import grant_adapter as ga


@pytest.fixture
def adapter():
    try:
        return ga.discover()
    except ga.CapabilityMissing as exc:
        pytest.fail(f"BLOCKED: {exc}")
    except ga.AdapterNeeded as exc:
        pytest.fail(f"ADAPTER-NEEDED: {exc}")


@pytest.fixture
def ctx(adapter, db_session, make_verified_user):
    u, _ = make_verified_user()
    return adapter.build_context(db_session, u.id)


def _rls(db, owner_id):
    from sqlalchemy import text

    db.execute(text("SET LOCAL app.current_user_id = :u"), {"u": str(owner_id)})


def _denied(adapter, fn):
    try:
        fn()
    except adapter.denial_types():
        return True
    return False


def test_GC1_one_use_across_sessions(adapter, ctx, db_session):
    gid = adapter.mint(db_session, ctx)
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    adapter.consume(db_session, ctx, gid)
    db_session.commit()
    with SessionLocal() as restarted:  # a fresh worker after a crash
        _rls(restarted, ctx.owner_id)
        assert _denied(adapter, lambda: adapter.consume(restarted, ctx, gid)), "GC-1 consumed grant authorized a second consume"
        restarted.rollback()


def test_GC2_concurrent_consumers_exactly_one_wins(adapter, ctx, db_session):
    gid = adapter.mint(db_session, ctx)
    db_session.commit()
    results = []
    barrier = threading.Barrier(2)

    def worker():
        with SessionLocal() as s:
            _rls(s, ctx.owner_id)
            barrier.wait()
            try:
                adapter.consume(s, ctx, gid)
                s.commit()
                results.append("ok")
            except adapter.denial_types():
                s.rollback()
                results.append("denied")
            except Exception as exc:  # noqa: BLE001
                s.rollback()
                results.append(f"error:{type(exc).__name__}")

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(30) for t in ts]
    assert sorted(results) == ["denied", "ok"], f"GC-2 {results}"


def test_GC3_expired_grant_denied(adapter, ctx, db_session):
    gid = adapter.mint(db_session, ctx, expires_in=timedelta(seconds=30))
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    assert _denied(adapter, lambda: adapter.consume(db_session, ctx, gid, now=datetime.utcnow() + timedelta(minutes=2))), "GC-3"
    db_session.rollback()


def test_GC4_cancelled_grant_denied(adapter, ctx, db_session):
    gid = adapter.mint(db_session, ctx)
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    adapter.cancel(db_session, ctx, gid)
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    assert _denied(adapter, lambda: adapter.consume(db_session, ctx, gid)), "GC-4"
    db_session.rollback()


@pytest.mark.parametrize("field,value", [("job_id", uuid.uuid4()), ("local_commit_sha", "d" * 40), ("observed_base_sha", "e" * 40), ("authority_version", 8)])
def test_GC5_exact_binding(adapter, ctx, db_session, field, value):
    gid = adapter.mint(db_session, ctx)
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    assert _denied(adapter, lambda: adapter.consume(db_session, ctx, gid, **{field: value})), f"GC-5 {field} mismatch authorized"
    db_session.rollback()


def test_GC6_push_grant_never_authorizes_draft_pr(adapter, ctx, db_session):
    gid = adapter.mint(db_session, ctx)
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    try:
        authorized = not _denied(adapter, lambda: adapter.consume(db_session, ctx, gid, effect_kind=ga.EFFECT_OPEN_PR))
    except ga.CapabilityMissing as exc:
        pytest.fail(f"BLOCKED: {exc} (spec: open_pr is a DISTINCT_ONE_SHOT_GRANT)")
    assert not authorized, "GC-6 a push grant authorized a draft-PR effect"


def test_GC7_no_reissue_from_a_consumed_preflight(adapter, ctx, db_session):
    gid = adapter.mint(db_session, ctx)
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    adapter.consume(db_session, ctx, gid)
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    reissued = not _denied(adapter, lambda: adapter.mint(db_session, ctx))
    db_session.rollback()
    assert not reissued, "GC-7 a second push grant was minted from the same (already used) preflight receipt without a fresh preflight"


def test_GC8_denials_are_typed_external_boundary_errors(adapter, ctx, db_session):
    try:
        from app.mainai_execution.publication_errors import ExternalBoundaryError
    except ImportError:
        pytest.fail("BLOCKED: candidate has no typed publication boundary (publication_errors); compose on #288+")
    gid = adapter.mint(db_session, ctx)
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    adapter.cancel(db_session, ctx, gid)
    db_session.commit()
    _rls(db_session, ctx.owner_id)
    with pytest.raises(Exception) as caught:
        adapter.consume(db_session, ctx, gid)
    db_session.rollback()
    assert isinstance(caught.value, ExternalBoundaryError), (
        f"GC-8 grant denial is {type(caught.value).__name__} (MRO {[c.__name__ for c in type(caught.value).__mro__[:4]]}), "
        "not an ExternalBoundaryError/GrantDeniedError -> the execution boundary would map it via PublicationUnknownError at best"
    )
