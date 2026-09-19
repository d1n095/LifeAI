"""Statistics Command Center -- one coherent metric registry, never a competing metric truth.
See docs/mainai_v2/MAINAI_COGNITIVE_CONTROL_PLANE_RECONCILIATION.md for the architecture
decision.

METRIC != TRUTH. METRIC IMPROVEMENT != SYSTEM IMPROVEMENT. MISSING != ZERO.

Every metric here IS an `app.resource_intelligence.types.MetricEnvelope` -- this module never
invents a second shape (`resource_intelligence.types.MetricEnvelope` already carries value/unit/
definition/denominator/time_window/population/sample_size/source/method/missing_data/
uncertainty/last_updated/trend). `ObservationBasis` below is this package's own ADDITIVE
classification (observed/derived/estimated) layered on top via `StatisticsRecord`, a thin
wrapper -- never a modification of `MetricEnvelope` itself, which `resource_intelligence`'s own
tests already depend on keeping its exact current shape."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.mainai_vision.types import MetricEnvelope, unknown_metric


class ObservationBasis(str, enum.Enum):
    OBSERVED = "observed"
    DERIVED = "derived"
    ESTIMATED = "estimated"


@dataclass(frozen=True)
class StatisticsRecord:
    """`MetricEnvelope` plus this package's own `basis` and `exclusions` -- the two fields the
    founder's own spec named that `MetricEnvelope` does not carry. A thin wrapper, never a
    parallel envelope shape: `envelope` is always the real, unmodified `MetricEnvelope`."""

    name: str
    envelope: MetricEnvelope
    basis: ObservationBasis
    exclusions: tuple[str, ...] = ()


def _wrap(name: str, envelope: MetricEnvelope, *, basis: ObservationBasis, exclusions: tuple[str, ...] = ()) -> StatisticsRecord:
    return StatisticsRecord(name=name, envelope=envelope, basis=basis, exclusions=exclusions)


def vision_maturity_statistic(*, node_count: int, average_maturity_index: float | None, max_index: int) -> StatisticsRecord:
    if node_count == 0:
        envelope = unknown_metric(unit="fraction", definition="average maturity index / max index across current vision nodes", source="mainai_vision.completion", method="zero vision nodes")
        return _wrap("vision_maturity", envelope, basis=ObservationBasis.DERIVED)
    from datetime import datetime, timezone

    value = (average_maturity_index / max_index) if max_index > 0 else 0.0
    envelope = MetricEnvelope(
        value=value, unit="fraction", definition="average maturity index / max index across current vision nodes",
        denominator="max_maturity_index", time_window=None, population=f"node_count={node_count}", sample_size=node_count,
        source="mainai_vision.completion", method="mean(node maturity index) / max index", missing_data=False,
        uncertainty=None, last_updated=datetime.now(timezone.utc), trend=None,
    )
    return _wrap("vision_maturity", envelope, basis=ObservationBasis.DERIVED)


def p0_p1_p2_counts(*, p0: int, p1: int, p2: int) -> dict[str, StatisticsRecord]:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    def _count(name: str, value: int) -> StatisticsRecord:
        envelope = MetricEnvelope(
            value=value, unit="count", definition=f"open {name.upper()} findings", denominator=None,
            time_window=None, population=None, sample_size=None, source="caller_supplied",
            method="caller-reported count", missing_data=False, uncertainty=None, last_updated=now, trend=None,
        )
        return _wrap(name, envelope, basis=ObservationBasis.OBSERVED)

    return {"p0": _count("p0", p0), "p1": _count("p1", p1), "p2": _count("p2", p2)}


def technical_debt_statistic(*, unresolved_lesson_count: int | None) -> StatisticsRecord:
    """MISSING != ZERO: `unresolved_lesson_count=None` (caller genuinely does not know) is
    `missing_data=True`, never silently reported as zero debt."""

    from datetime import datetime, timezone

    if unresolved_lesson_count is None:
        envelope = unknown_metric(unit="count", definition="unresolved active EngineeringLesson count", source="mainai_executive.meta_improvement", method="caller did not supply a count")
        return _wrap("technical_debt", envelope, basis=ObservationBasis.OBSERVED)
    envelope = MetricEnvelope(
        value=unresolved_lesson_count, unit="count", definition="unresolved active EngineeringLesson count",
        denominator=None, time_window=None, population=None, sample_size=None,
        source="mainai_executive.meta_improvement", method="lookup_behavioral_lessons() + caller's own broader lesson count",
        missing_data=False, uncertainty=None, last_updated=datetime.now(timezone.utc), trend=None,
    )
    return _wrap("technical_debt", envelope, basis=ObservationBasis.OBSERVED)


def compile_statistics_registry(
    db: Session,
    *,
    owner_id: uuid.UUID,
    p0: int = 0,
    p1: int = 0,
    p2: int = 0,
    unresolved_lesson_count: int | None = None,
) -> dict[str, StatisticsRecord]:
    """Composed, DB-touching convenience wrapper -- pulls real completion data from
    `completion.assess_program_completion()` (never re-derives it), never writes anything."""

    from app.mainai_vision.completion import MATURITY_INDEX, assess_program_completion

    report = assess_program_completion(db, owner_id=owner_id)
    max_index = max(MATURITY_INDEX.values()) if MATURITY_INDEX else 0
    average_index = None
    if report.node_maturity:
        from app.mainai_vision.types import MaturityState

        average_index = sum(MATURITY_INDEX[MaturityState(v)] for v in report.node_maturity.values()) / len(report.node_maturity)

    registry: dict[str, StatisticsRecord] = {}
    registry["vision_maturity"] = vision_maturity_statistic(node_count=report.node_count, average_maturity_index=average_index, max_index=max_index)
    registry.update(p0_p1_p2_counts(p0=p0, p1=p1, p2=p2))
    registry["technical_debt"] = technical_debt_statistic(unresolved_lesson_count=unresolved_lesson_count)
    return registry
