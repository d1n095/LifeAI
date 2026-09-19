"""MainAI Cognitive Control Plane -- `app.mainai_vision.founder_truth` -- proves
`founder_program_truth()` composes the REAL `dashboard.founder_executive_dashboard()` with real
completion data, never invents an answer, and never exposes chain-of-thought.

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid

from app.mainai_vision.founder_truth import founder_program_truth
from app.models.user import User


def test_founder_truth_answers_the_named_questions_without_chain_of_thought(superuser_db):
    owner = User(email=f"ft-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    superuser_db.commit()

    truth = founder_program_truth(superuser_db, owner_id=owner.id)
    for key in ("WHERE_ARE_WE", "PERCENT_GENUINELY_COMPLETE", "WHY", "COMPLETION_DEFINITION", "WHAT_IS_STILL_MISSING", "WHAT_SHOULD_HAPPEN_NEXT", "WHAT_WAS_LEARNED"):
        assert key in truth
    assert truth["chain_of_thought_exposed"] is False
    assert truth["not_100_percent_means_nothing_more_can_be_improved"] is False
    assert truth["PERCENT_GENUINELY_COMPLETE"] == 0.0  # no vision nodes yet -- honest zero, not fabricated
    assert "does NOT mean" in truth["COMPLETION_DEFINITION"]
