import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.models.strategy_synthesis import (
    StrategySynthesisCase,
    StrategySynthesisEvent,
    StrategySynthesisInput,
)
from app.models.user import User
from app.models.work_intelligence import WorkStrategy
from app.strategy_synthesis import (
    StrategySynthesisError,
    add_recipe_component,
    add_synthesis_input,
    create_synthesis_case,
    explain_synthesis,
    materialize_candidate,
    ordered_recipe,
    readiness,
    record_conflict,
    reorder_recipe,
    transition_case,
    transition_conflict,
)
from app.work_intelligence import create_strategy


def _owner(db):
    owner = User(
        email=f"synthesis-{uuid.uuid4()}@example.com",
        password_hash="x",
        email_verified=True,
    )
    db.add(owner)
    db.flush()
    return owner


def _strategy(db, owner, key, version, predecessor=None):
    return create_strategy(
        db,
        owner_id=owner.id,
        strategy_key=key,
        version=version,
        predecessor_id=predecessor.id if predecessor else None,
        ordered_phases=[{"phase": f"source-{version}"}],
        idempotency_key=f"strategy-{key}-{version}",
    )


def _case(db, owner, *, key="synth", version=2, predecessor=None, idem="case"):
    return create_synthesis_case(
        db,
        owner_id=owner.id,
        case_key=key,
        revision=version,
        candidate_strategy_key=key,
        candidate_strategy_version=version,
        predecessor_strategy_id=predecessor.id if predecessor else None,
        purpose="combine evidence-backed methods",
        improvement_dimensions=["quality", "navigation_efficiency"],
        quality_invariants=["focused_tests", "owner_isolation"],
        applicability={"task_type": "repository_hardening"},
        idempotency_key=idem,
    )


def _component(
    db,
    owner,
    case,
    strategy,
    *,
    disposition="included",
    kind="repository_search",
    idem="component",
):
    source = add_synthesis_input(
        db,
        owner_id=owner.id,
        case_id=case.id,
        source_kind="work_strategy",
        source_id=strategy.id,
        disposition=disposition,
        reason="explicit source assessment",
        basis="deterministic",
        idempotency_key=f"{idem}-input",
    )
    component = add_recipe_component(
        db,
        owner_id=owner.id,
        case_id=case.id,
        input_id=source.id,
        component_kind=kind,
        description=f"{kind} method",
        disposition=disposition,
        reason="supported by bounded observations",
        basis="manual",
        method_payload={
            "tool_identity": "rg",
            "stop_after": "definition_callers_tests",
        },
        idempotency_key=idem,
    )
    return source, component


def test_component_level_learning_preserves_losing_and_excluded_sources(superuser_db):
    owner = _owner(superuser_db)
    losing = _strategy(superuser_db, owner, "losing", 1)
    winning = _strategy(superuser_db, owner, "winning", 1)
    case = _case(superuser_db, owner)

    _, useful = _component(superuser_db, owner, case, losing, idem="useful")
    _, excluded = _component(
        superuser_db,
        owner,
        case,
        winning,
        disposition="excluded",
        kind="implementation_sequence",
        idem="excluded",
    )

    assert useful.disposition == "included"
    assert excluded.disposition == "excluded"
    assert losing.ordered_phases == [{"phase": "source-1"}]
    assert winning.ordered_phases == [{"phase": "source-1"}]
    assert readiness(superuser_db, owner_id=owner.id, case_id=case.id)["ready"]


def test_recipe_order_and_replay_are_deterministic(superuser_db):
    owner = _owner(superuser_db)
    source = _strategy(superuser_db, owner, "source-a", 1)
    source_b = _strategy(superuser_db, owner, "source-b", 1)
    case = _case(superuser_db, owner)
    _, first = _component(superuser_db, owner, case, source, idem="first")
    _, second = _component(
        superuser_db,
        owner,
        case,
        source_b,
        kind="verification_obligation",
        idem="second",
    )

    event = reorder_recipe(
        superuser_db,
        owner_id=owner.id,
        case_id=case.id,
        ordered_component_ids=[second.id, first.id],
        idempotency_key="reorder",
    )
    replay = reorder_recipe(
        superuser_db,
        owner_id=owner.id,
        case_id=case.id,
        ordered_component_ids=[second.id, first.id],
        idempotency_key="reorder",
    )
    assert replay.id == event.id
    assert [
        row.id
        for row in ordered_recipe(superuser_db, owner_id=owner.id, case_id=case.id)
    ] == [
        second.id,
        first.id,
    ]
    with pytest.raises(StrategySynthesisError, match="different recipe order"):
        reorder_recipe(
            superuser_db,
            owner_id=owner.id,
            case_id=case.id,
            ordered_component_ids=[first.id, second.id],
            idempotency_key="reorder",
        )


def test_hard_conflict_blocks_then_resolution_allows_materialization(superuser_db):
    owner = _owner(superuser_db)
    baseline = _strategy(superuser_db, owner, "candidate", 1)
    case = _case(superuser_db, owner, key="candidate", predecessor=baseline)
    _, component = _component(superuser_db, owner, case, baseline)
    conflict = record_conflict(
        superuser_db,
        owner_id=owner.id,
        case_id=case.id,
        left_component_id=component.id,
        description="speed shortcut conflicts with owner verification",
        severity="hard",
        idempotency_key="conflict",
    )
    assert readiness(superuser_db, owner_id=owner.id, case_id=case.id)["blockers"] == [
        "unresolved_hard_conflict"
    ]
    with pytest.raises(StrategySynthesisError, match="not ready"):
        transition_case(
            superuser_db,
            owner_id=owner.id,
            case_id=case.id,
            status="ready",
            idempotency_key="ready",
        )

    transition_conflict(
        superuser_db,
        owner_id=owner.id,
        conflict_id=conflict.id,
        status="resolved",
        resolution_reason="retained the mandatory verification step",
        idempotency_key="resolve",
    )
    transition_case(
        superuser_db,
        owner_id=owner.id,
        case_id=case.id,
        status="ready",
        idempotency_key="ready",
    )
    materialization = materialize_candidate(
        superuser_db,
        owner_id=owner.id,
        case_id=case.id,
        idempotency_key="materialize",
    )
    replay = materialize_candidate(
        superuser_db,
        owner_id=owner.id,
        case_id=case.id,
        idempotency_key="materialize",
    )
    candidate = superuser_db.get(WorkStrategy, materialization.strategy_id)
    assert replay.id == materialization.id
    assert candidate.predecessor_id == baseline.id
    assert candidate.work_category == "synthesized_candidate"
    assert not hasattr(candidate, "active") and not hasattr(candidate, "preferred")
    assert candidate.methods["synthesis_case_id"] == str(case.id)


def test_component_support_is_not_candidate_verification(superuser_db):
    owner = _owner(superuser_db)
    baseline = _strategy(superuser_db, owner, "candidate", 1)
    case = _case(superuser_db, owner, key="candidate", predecessor=baseline)
    _component(superuser_db, owner, case, baseline)
    transition_case(
        superuser_db,
        owner_id=owner.id,
        case_id=case.id,
        status="ready",
        idempotency_key="ready",
    )
    materialization = materialize_candidate(
        superuser_db,
        owner_id=owner.id,
        case_id=case.id,
        idempotency_key="materialize",
    )
    assert materialization.strategy_id
    assert (
        superuser_db.execute(
            text("SELECT count(*) FROM work_verification_observations")
        ).scalar_one()
        == 0
    )
    assert (
        superuser_db.execute(
            text("SELECT count(*) FROM strategy_quality_assessments")
        ).scalar_one()
        == 0
    )
    assert (
        superuser_db.execute(
            text("SELECT count(*) FROM strategy_promotion_candidates")
        ).scalar_one()
        == 0
    )


def test_strict_source_owner_and_kind_validation(superuser_db):
    owner = _owner(superuser_db)
    other = _owner(superuser_db)
    foreign = _strategy(superuser_db, other, "foreign", 1)
    case = _case(superuser_db, owner)
    with pytest.raises(StrategySynthesisError, match="another owner"):
        add_synthesis_input(
            superuser_db,
            owner_id=owner.id,
            case_id=case.id,
            source_kind="work_strategy",
            source_id=foreign.id,
            disposition="included",
            reason="must fail",
            idempotency_key="foreign",
        )
    with pytest.raises(StrategySynthesisError, match="unsupported"):
        add_synthesis_input(
            superuser_db,
            owner_id=owner.id,
            case_id=case.id,
            source_kind="arbitrary_table",
            source_id=foreign.id,
            disposition="included",
            reason="must fail",
            idempotency_key="invalid",
        )


def test_database_constraints_and_rls_fail_closed(superuser_db, db_session):
    owner = _owner(superuser_db)
    other = _owner(superuser_db)
    source = _strategy(superuser_db, owner, "source", 1)
    case = _case(superuser_db, owner)
    superuser_db.commit()

    db_session.execute(
        text("SELECT set_config('app.current_user_id', :owner, false)"),
        {"owner": str(other.id)},
    )
    assert db_session.execute(select(StrategySynthesisCase)).scalars().all() == []
    db_session.rollback()

    bad = StrategySynthesisInput(
        owner_id=owner.id,
        case_id=case.id,
        source_kind="work_strategy",
        work_strategy_id=source.id,
        intelligence_execution_id=uuid.uuid4(),
        disposition="included",
        reason="invalid exclusive source arc",
        basis="unknown",
        idempotency_key="bad-arc",
    )
    superuser_db.add(bad)
    with pytest.raises(IntegrityError):
        superuser_db.flush()
    superuser_db.rollback()


def test_revision_history_explainability_and_idempotency_conflict(superuser_db):
    owner = _owner(superuser_db)
    source = _strategy(superuser_db, owner, "evolving", 1)
    first = _case(
        superuser_db, owner, key="evolving", predecessor=source, idem="case-v2"
    )
    _, component = _component(superuser_db, owner, first, source)
    replay = _case(
        superuser_db, owner, key="evolving", predecessor=source, idem="case-v2"
    )
    assert replay.id == first.id
    with pytest.raises(StrategySynthesisError, match="idempotency"):
        create_synthesis_case(
            superuser_db,
            owner_id=owner.id,
            case_key="different",
            revision=2,
            candidate_strategy_key="different",
            candidate_strategy_version=2,
            purpose="conflict",
            quality_invariants=["test"],
            idempotency_key="case-v2",
        )
    second = create_synthesis_case(
        superuser_db,
        owner_id=owner.id,
        case_key="evolving",
        revision=3,
        candidate_strategy_key="evolving",
        candidate_strategy_version=3,
        predecessor_case_id=first.id,
        purpose="revision",
        quality_invariants=["focused_tests"],
        idempotency_key="case-v3",
    )
    explanation = explain_synthesis(
        superuser_db, owner_id=owner.id, case_id=second.id, max_depth=1, max_items=10
    )
    assert [row.id for row in explanation.predecessors] == [first.id]
    assert not explanation.truncated
    assert component.description == "repository_search method"
    assert superuser_db.execute(select(StrategySynthesisEvent)).scalars().all()


def test_concurrent_materialization_is_single_and_provider_independent(
    superuser_db, monkeypatch
):
    from app.db import migration_engine
    from app.providers import registry

    monkeypatch.setattr(
        registry,
        "get_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider resolution is forbidden")
        ),
    )
    owner = _owner(superuser_db)
    baseline = _strategy(superuser_db, owner, "concurrent", 1)
    case = _case(superuser_db, owner, key="concurrent", predecessor=baseline)
    _component(superuser_db, owner, case, baseline)
    transition_case(
        superuser_db,
        owner_id=owner.id,
        case_id=case.id,
        status="ready",
        idempotency_key="ready",
    )
    owner_id, case_id = owner.id, case.id
    superuser_db.commit()

    def materialize():
        session = sessionmaker(bind=migration_engine)()
        try:
            row = materialize_candidate(
                session,
                owner_id=owner_id,
                case_id=case_id,
                idempotency_key="concurrent-materialize",
            )
            result = row.id
            session.commit()
            return result
        finally:
            session.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: materialize(), range(2)))
    assert len(set(results)) == 1
    assert (
        superuser_db.execute(
            text("SELECT count(*) FROM strategy_synthesis_materializations")
        ).scalar_one()
        == 1
    )
    assert (
        superuser_db.execute(
            text(
                "SELECT count(*) FROM work_strategies WHERE strategy_key='concurrent' AND version=2"
            )
        ).scalar_one()
        == 1
    )


def test_runtime_privileges_include_synthesis_tables(superuser_db):
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges

    apply_mainai_execution_privileges(migration_engine, require_complete=True)
