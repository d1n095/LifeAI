"""Stage O — memory health / repack checks."""

from __future__ import annotations

import uuid

from app.inspectable_memory import founder_add_memory_note
from app.memory_health import run_memory_health_checks
from app.models.user import User


def test_stage_o_health_checks(superuser_db):
    owner = User(email=f"o-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    founder_add_memory_note(
        superuser_db, owner_id=owner.id, content="orphan health probe", note_type="preference", idempotency_key="o1"
    )
    superuser_db.commit()
    report = run_memory_health_checks(superuser_db, owner_id=owner.id)
    assert report.ok_to_repack is True
    assert all(f.changes_canonical_meaning is False for f in report.findings)
