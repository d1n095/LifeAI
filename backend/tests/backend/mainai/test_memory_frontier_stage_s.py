"""Stage S — 1000-tick memory/autonomy soak spec + bounded runner."""

from __future__ import annotations

import uuid

from app.memory_soak import SOAK_SPEC, run_bounded_memory_soak
from app.models.user import User


def test_stage_s_bounded_soak_and_spec(superuser_db):
    owner = User(email=f"s-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    assert SOAK_SPEC["ticks"] == 1000
    report = run_bounded_memory_soak(superuser_db, owner_id=owner.id, ticks=12)
    superuser_db.commit()
    assert report.ticks_run == 12
    assert report.growth["ticks"] == 12
    assert "authority_leakage" in SOAK_SPEC["watch_for"]
