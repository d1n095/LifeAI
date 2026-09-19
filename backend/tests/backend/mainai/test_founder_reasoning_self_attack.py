"""Independent adversarial self-attack pass over the Founder Reasoning + Judgment program
(reconciliation doc §self-attack). Separate from each module's own unit tests (BUILDER != FINAL
EXAMINER) -- targets the failure modes the founder's own directive explicitly named: sycophancy,
single-correction universalization, stale preference resurrection, job spam,
over-parallelization, memory=authority confusion, cross-owner leakage, provider-derived
"founder preference", unbounded heuristic growth, superseded lesson persistence."""

from __future__ import annotations

import ast
import inspect
import uuid

import pytest

from app.founder_memory import record_founder_memory
from app.mainai_executive import (
    architectural_gravity,
    idea_incubation,
    judgment,
    kill_criteria,
    meta_improvement,
    rejected_idea_guard,
    retrieval,
    strategic_compression,
    wip_awareness,
)
from app.mainai_executive.meta_improvement import (
    BEHAVIORAL_COMPONENTS,
    lookup_behavioral_lessons,
    record_behavioral_lesson_from_founder_correction,
)
from app.mainai_executive.rejected_idea_guard import check_recently_rejected
from app.mainai_executive.strategic_compression import StrategicCompressionError, compress_into_program
from app.mainai_executive.wip_awareness import current_wip_load, should_defer_new_work
from app.models.mainai_execution import EngineeringLessonStatus
from app.models.user import User
from app.work_candidates.service import record_work_candidate

from tests.backend.mainai.test_work_candidates import _owner_with_entity

NEW_MODULES = (
    idea_incubation, wip_awareness, kill_criteria, rejected_idea_guard,
    retrieval, judgment, architectural_gravity, strategic_compression, meta_improvement,
)

FORBIDDEN_AUTHORITY_SYMBOLS = (
    "authorize_work_candidate",
    "authorize_execution_scope",
    "grant_task_approval",
)


def _owner(db) -> User:
    user = User(email=f"attack-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


# --- 1. Memory != authority: no new module imports a real authority-granting function. ------


@pytest.mark.parametrize("module", NEW_MODULES, ids=lambda m: m.__name__)
def test_no_new_module_imports_a_real_authority_granting_function(module):
    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported_names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            imported_names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    for forbidden in FORBIDDEN_AUTHORITY_SYMBOLS:
        assert forbidden not in imported_names, f"{module.__name__} imports {forbidden!r} -- memory/judgment must never grant authority"


# --- 2. Every advisory/proposal-shaped return is explicitly non-authoritative. ---------------


def test_kill_criteria_result_is_explicitly_non_authoritative(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="x",
        idempotency_key=f"sa-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()
    result = kill_criteria.evaluate_kill_criteria(superuser_db, owner_id=owner.id, work_candidate_id=candidate.id)
    assert result["authority_impact"] == "NONE"
    assert result["mutates"] is False


def test_architectural_gravity_result_never_claims_auto_created(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    result = architectural_gravity.detect_architectural_gravity(superuser_db, owner_id=owner.id)
    assert result["auto_created"] is False
    assert result["authority_impact"] == "NONE"


def test_strategic_compression_result_never_authorizes(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    a = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="a", idempotency_key=f"sa-{uuid.uuid4()}", classifier_strategy="test")
    b = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="b", idempotency_key=f"sa-{uuid.uuid4()}", classifier_strategy="test")
    superuser_db.commit()
    result = compress_into_program(superuser_db, owner_id=owner.id, idempotency_key=f"sa-prog-{uuid.uuid4()}", work_candidate_ids=[a.id, b.id])
    assert result["authorized"] is False
    assert result["authority_impact"] == "NONE"


def test_judgment_decision_is_never_authorized():
    decision = judgment.decide_judgment(confidence=0.9, evidence_strength=0.9, founder_originated=True, stakes=0.1, urgency=0.5, founder_attention_cost=0.1)
    assert decision.authorized is False


# --- 3. Single-correction universalization: root_cause/general_rule/applies_to are always ----
#         the caller's own explicit judgment -- meta_improvement never derives them from the
#         raw note text, and record_behavioral_lesson_from_founder_correction has NO defaults
#         for them (a TypeError, not a silent auto-fill, if omitted).


def test_meta_improvement_has_no_default_for_root_cause_or_general_rule_or_applies_to():
    sig = inspect.signature(record_behavioral_lesson_from_founder_correction)
    for required in ("root_cause", "affected_component", "general_rule", "applies_to", "fix"):
        assert sig.parameters[required].default is inspect.Parameter.empty, (
            f"{required} must have no default -- a MISS becomes a LESSON only via deliberate, "
            "explicit caller judgment, never auto-derived from raw correction text"
        )


def test_meta_improvement_module_never_derives_general_rule_from_note_content():
    """Structural: the module's own source never reads note.content into anything but the
    upstream problem= field (via record_lesson_from_founder_correction, unchanged) -- it must
    never auto-generate general_rule/root_cause/applies_to from the raw text itself."""
    source = inspect.getsource(meta_improvement)
    assert "note.content" not in source, (
        "meta_improvement.py itself must never read note.content directly -- that derivation "
        "belongs solely to the unchanged upstream record_lesson_from_founder_correction()"
    )


# --- 4. Stale preference resurrection: outside the lookback window, no suppression at all. --


def test_rejection_outside_lookback_window_no_longer_suppresses(superuser_db):
    from datetime import datetime, timedelta

    from app.models.intelligence_governance import IntelligenceIdea
    from app.intelligence_governance.service import record_execution
    from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask

    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    goal = MainAIGoal(owner_id=owner.id, title="p", original_instruction="s", created_by="test")
    superuser_db.add(goal)
    superuser_db.flush()
    plan = MainAIPlan(owner_id=owner.id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    superuser_db.add(plan)
    superuser_db.flush()
    task = MainAITask(owner_id=owner.id, goal_id=goal.id, plan_id=plan.id, description="d", task_type="repo_edit")
    superuser_db.add(task)
    superuser_db.flush()
    execution = record_execution(superuser_db, owner_id=owner.id, task_id=task.id, idempotency_key=f"exec-{uuid.uuid4()}", role="builder")
    # intelligence_ideas is DB-enforced append-only (migration 0038) -- an UPDATE of an
    # already-flushed row's created_at is rejected by Postgres itself, so the backdated
    # timestamp must be set at construction time (a fresh INSERT), not via record_idea() +
    # a later mutation.
    idea = IntelligenceIdea(
        owner_id=owner.id, execution_id=execution.id, idea_kind="idea",
        content="old rejected approach for the parser", disposition="rejected",
        disposition_reason="tried and abandoned", idempotency_key=f"stale-{uuid.uuid4()}",
        created_at=datetime.utcnow() - timedelta(days=200),
    )
    superuser_db.add(idea)
    superuser_db.commit()

    guard = check_recently_rejected(
        superuser_db, owner_id=owner.id, candidate_title="old rejected approach for the parser", lookback_days=90,
    )
    assert guard["is_likely_duplicate_of_rejected"] is False


# --- 5. Job spam: compression refuses to "compress" a single item into a no-op program. -----


def test_compression_refuses_a_program_of_one(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    a = record_work_candidate(superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="a", idempotency_key=f"sa-{uuid.uuid4()}", classifier_strategy="test")
    superuser_db.commit()
    with pytest.raises(StrategicCompressionError):
        compress_into_program(superuser_db, owner_id=owner.id, idempotency_key=f"sa-prog-{uuid.uuid4()}", work_candidate_ids=[a.id])


# --- 6. Over-parallelization: the WIP ceiling fires AT the limit, not only past it. ----------


def test_wip_ceiling_fires_exactly_at_the_limit_not_only_strictly_past_it(superuser_db):
    from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask, MainAITaskStatus

    owner = _owner(superuser_db)
    superuser_db.commit()
    limit = 3
    for _ in range(limit):
        goal = MainAIGoal(owner_id=owner.id, title="p", original_instruction="s", created_by="test")
        superuser_db.add(goal)
        superuser_db.flush()
        plan = MainAIPlan(owner_id=owner.id, goal_id=goal.id, version=1, rationale="test", created_by="test")
        superuser_db.add(plan)
        superuser_db.flush()
        task = MainAITask(owner_id=owner.id, goal_id=goal.id, plan_id=plan.id, description="d", task_type="repo_edit", status=MainAITaskStatus.running)
        superuser_db.add(task)
    superuser_db.commit()

    load = current_wip_load(superuser_db, owner_id=owner.id)
    assert load["tasks_in_flight"] == limit
    should_defer, _reason = should_defer_new_work(superuser_db, owner_id=owner.id, wip_limit=limit)
    assert should_defer is True, "at-the-limit must already defer, not only strictly-over-the-limit"


# --- 7. Cross-owner leakage: owner A's rejection must never suppress owner B's idea. ---------


def test_rejected_idea_guard_never_leaks_across_owners(superuser_db):
    owner_a, entity_a = _owner_with_entity(superuser_db)
    owner_b, entity_b = _owner_with_entity(superuser_db)
    superuser_db.commit()

    from app.intelligence_governance.service import record_execution, record_idea
    from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask

    goal = MainAIGoal(owner_id=owner_a.id, title="p", original_instruction="s", created_by="test")
    superuser_db.add(goal)
    superuser_db.flush()
    plan = MainAIPlan(owner_id=owner_a.id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    superuser_db.add(plan)
    superuser_db.flush()
    task = MainAITask(owner_id=owner_a.id, goal_id=goal.id, plan_id=plan.id, description="d", task_type="repo_edit")
    superuser_db.add(task)
    superuser_db.flush()
    execution = record_execution(superuser_db, owner_id=owner_a.id, task_id=task.id, idempotency_key=f"exec-{uuid.uuid4()}", role="builder")
    record_idea(
        superuser_db, owner_id=owner_a.id, execution_id=execution.id, idea_kind="idea",
        content="shared phrasing for a common feature idea", disposition="rejected",
        disposition_reason="not for owner A", idempotency_key=f"cross-{uuid.uuid4()}",
    )
    superuser_db.commit()

    guard_for_b = check_recently_rejected(
        superuser_db, owner_id=owner_b.id, candidate_title="shared phrasing for a common feature idea",
    )
    assert guard_for_b["is_likely_duplicate_of_rejected"] is False


# --- 8. Provider-derived "founder preference": behavioral lessons still require a REAL, ------
#         founder-attributed correction note -- never inventable from an arbitrary string.


def test_behavioral_lesson_still_requires_a_real_correction_note_object(superuser_db):
    # A bare string (e.g. something an LLM emitted) is not a real FounderMemoryNote --
    # getattr(note, "note_type", None) degrades it to None, which the upstream, unchanged
    # note_type=='correction' guard then fails closed on (not a silent pass-through).
    with pytest.raises(ValueError, match="note_type='correction'"):
        record_behavioral_lesson_from_founder_correction(
            superuser_db, note="just some text a model produced", root_cause="n/a",
            affected_component="judgment_temperament", general_rule="n/a", applies_to=["x"],
            created_by="test", fix="n/a",
        )


# --- 9. Unbounded heuristic growth: the behavioral-component vocabulary is closed and small. -


def test_behavioral_components_vocabulary_is_closed_and_small():
    assert isinstance(BEHAVIORAL_COMPONENTS, frozenset)
    assert 1 <= len(BEHAVIORAL_COMPONENTS) <= 12, "a growing, unbounded vocabulary defeats the whole point of a closed set"


def test_behavioral_components_is_never_mutated_at_runtime_anywhere_in_the_module():
    source = inspect.getsource(meta_improvement)
    assert "BEHAVIORAL_COMPONENTS.add(" not in source
    assert "BEHAVIORAL_COMPONENTS |=" not in source
    assert "BEHAVIORAL_COMPONENTS.update(" not in source


# --- 10. Superseded/disputed lessons never resurface as active guidance. --------------------


def test_disputed_behavioral_lesson_is_excluded_from_lookup(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    note = record_founder_memory(
        superuser_db, owner_id=owner.id, note_type="correction", content="disputed pattern",
        idempotency_key=f"disp-{uuid.uuid4()}", authority="founder", basis="manual",
    )
    superuser_db.commit()
    lesson = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note, root_cause="r", affected_component="wip_default",
        general_rule="g", applies_to=["wip"], created_by="test", fix="f",
    )
    superuser_db.commit()
    lesson.status = EngineeringLessonStatus.disputed
    superuser_db.commit()

    found = lookup_behavioral_lessons(superuser_db, component="wip_default")
    assert lesson.id not in [row.id for row in found]


# --- 11. Kill signal reaching JudgmentAction.KILL causes zero automatic mutation. ------------


def test_kill_recommendation_never_automatically_mutates_the_underlying_row(superuser_db):
    from sqlalchemy import select

    from app.models.work_candidate import WorkCandidate

    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="untouched by kill signal",
        idempotency_key=f"sa-kill-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()
    from app.work_candidates.service import dismiss_work_candidate

    dismiss_work_candidate(superuser_db, owner_id=owner.id, candidate_id=candidate.id, reason="already dead")
    superuser_db.commit()

    kill = kill_criteria.evaluate_kill_criteria(superuser_db, owner_id=owner.id, work_candidate_id=candidate.id)
    assert kill["should_kill"] is True
    decision = judgment.decide_judgment(confidence=0.5, evidence_strength=0.5, founder_originated=False, kill_signal=kill)
    expected = judgment.JudgmentAction.KILL if kill["confidence"] >= 0.8 else judgment.JudgmentAction.ESCALATE
    assert decision.action == expected

    superuser_db.expire_all()
    reloaded = superuser_db.execute(select(WorkCandidate).where(WorkCandidate.id == candidate.id)).scalar_one()
    assert reloaded.status == "dismissed", "status must be exactly what dismiss_work_candidate() set -- the judgment call itself changed nothing"
