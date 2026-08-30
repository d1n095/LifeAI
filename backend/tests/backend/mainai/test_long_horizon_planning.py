"""Stage I — NOW/NEAR/MID/LONG planning; future plan != future authority."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text as sa_text

from app.long_horizon import HorizonBucket, build_horizon_plan, classify_horizon, reevaluate_horizon_item
from app.request_context import current_user_id as current_user_id_var


@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine)


def test_classify_horizon_buckets():
    assert classify_horizon("do this now") == HorizonBucket.NOW
    assert classify_horizon("nästa vecka ship recap") == HorizonBucket.NEAR
    assert classify_horizon("kvartalsvis archive") == HorizonBucket.MID
    assert classify_horizon("långsiktig multi-region someday") == HorizonBucket.LONG


def test_horizon_plan_never_grants_authority(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    plan = build_horizon_plan(
        db_session,
        owner_id=user.id,
        items=[
            {"title": "Ship Stage I horizon now", "idempotency_key": f"i-now-{uuid.uuid4()}", "bucket": "now"},
            {
                "title": "Multi-region active-active someday",
                "idempotency_key": f"i-long-{uuid.uuid4()}",
                "bucket": "long",
                "dependencies": ["stable-single-region"],
                "blockers": ["no-cross-region-lease"],
            },
        ],
    )
    db_session.commit()
    assert all(i.authority_granted is False for i in plan.items)
    assert all(i.needs_reevaluation is True for i in plan.items)
    by = plan.by_bucket()
    assert by["now"] and by["long"]
    long_item = by["long"][0]
    assert "no-cross-region-lease" in long_item.blockers
    reevaluated = reevaluate_horizon_item(long_item, reality_changed=True)
    assert reevaluated.authority_granted is False
    assert reevaluated.needs_reevaluation is True
