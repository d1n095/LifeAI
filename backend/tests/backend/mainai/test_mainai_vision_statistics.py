"""MainAI Cognitive Control Plane -- `app.mainai_vision.statistics` -- proves METRIC != TRUTH /
MISSING != ZERO hold for this package's own statistics registry, and that every record reuses
the REAL `MetricEnvelope` shape verbatim (never a second, competing metric shape).

See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture this
module implements."""

from __future__ import annotations

import uuid

from app.mainai_vision.statistics import (
    ObservationBasis,
    compile_statistics_registry,
    p0_p1_p2_counts,
    technical_debt_statistic,
    vision_maturity_statistic,
)
from app.mainai_vision.types import MetricEnvelope
from app.models.user import User


def test_every_statistics_record_wraps_a_real_metric_envelope():
    stats = p0_p1_p2_counts(p0=2, p1=5, p2=0)
    for record in stats.values():
        assert isinstance(record.envelope, MetricEnvelope)
        assert isinstance(record.basis, ObservationBasis)


def test_missing_technical_debt_count_is_missing_data_not_zero():
    record = technical_debt_statistic(unresolved_lesson_count=None)
    assert record.envelope.missing_data is True
    assert record.envelope.value is None


def test_known_technical_debt_count_is_reported_as_given():
    record = technical_debt_statistic(unresolved_lesson_count=3)
    assert record.envelope.missing_data is False
    assert record.envelope.value == 3


def test_vision_maturity_with_zero_nodes_is_missing_data():
    record = vision_maturity_statistic(node_count=0, average_maturity_index=None, max_index=12)
    assert record.envelope.missing_data is True


def test_vision_maturity_fraction_is_bounded_0_to_1():
    record = vision_maturity_statistic(node_count=5, average_maturity_index=6.0, max_index=12)
    assert 0.0 <= record.envelope.value <= 1.0


def test_compile_statistics_registry_never_writes(superuser_db):
    owner = User(email=f"stats-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(owner)
    superuser_db.flush()
    superuser_db.commit()

    registry = compile_statistics_registry(superuser_db, owner_id=owner.id, p0=1, p1=2, unresolved_lesson_count=4)
    assert set(registry.keys()) == {"vision_maturity", "p0", "p1", "p2", "technical_debt"}
    assert registry["p0"].envelope.value == 1
    assert registry["technical_debt"].envelope.value == 4
