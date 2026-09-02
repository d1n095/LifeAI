"""Memory→work replay contract — canonical ids retained on NOOP_SAME."""

from __future__ import annotations

import uuid

from app.inspectable_memory import founder_add_memory_note
from app.memory_work_linkage import apply_memory_work_linkage
from app.memory_work_linkage.types import LinkageAction
from tests.backend.mainai.test_memory_work_linkage import _promote_entity, _set_rls_user


def test_memory_work_replay_keeps_canonical_ids(db_session, make_verified_user):
    user, _ = make_verified_user()
    _set_rls_user(db_session, user.id)
    _promote_entity(
        db_session, owner_id=user.id, title="Short founder answers preference", key="replay-ent"
    )
    note, _ = founder_add_memory_note(
        db_session,
        owner_id=user.id,
        content="I want short founder answers",
        note_type="preference",
        idempotency_key=f"replay-a-{uuid.uuid4()}",
        link_to_work=False,
    )
    first = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id)
    db_session.commit()
    assert first.created_candidate_ids, "expected parked candidate"
    canonical = list(first.canonical_candidate_ids)
    assert canonical == list(first.created_candidate_ids) == list(first.created_now_ids)
    assert first.replayed is False
    assert first.operation_receipt_id

    second = apply_memory_work_linkage(db_session, owner_id=user.id, note_id=note.id)
    db_session.commit()
    assert second.created_now_ids == []
    # Self-replay still surfaces the candidate id (#238); created_now stays empty (#237).
    assert second.created_candidate_ids == canonical
    assert second.canonical_candidate_ids == canonical
    assert second.replayed is True
    assert LinkageAction.CANDIDATE_RECORDED in second.actions
    assert second.operation_receipt_id
