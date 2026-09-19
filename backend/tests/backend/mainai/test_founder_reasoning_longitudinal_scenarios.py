"""Independent adversarial pass over the whole "Founder Reasoning + Judgment + Strategic
Initiative" program (docs/mainai_v2/MAINAI_FOUNDER_REASONING_JUDGMENT_RECONCILIATION.md).

BUILDER != FINAL EXAMINER: idea_incubation/wip_awareness/kill_criteria/rejected_idea_guard/
retrieval/why_graph (Part 1) and judgment/architectural_gravity/strategic_compression/
meta_improvement (Part 2) were each built and unit-tested by the agent that wrote them. This
file is a SEPARATE pass, composing the real modules END-TO-END against real Postgres rows,
covering the founder's own directive's ten lettered longitudinal scenarios (A-J). It
deliberately does not re-prove what each module's own test file already proves in isolation
(e.g. kill_criteria's own never-mutates proof) -- it proves the modules combine correctly."""

from __future__ import annotations

import uuid

from app.founder_memory import record_founder_memory
from app.intelligence_governance.service import record_execution, record_idea
from app.mainai_execution.lesson_conflicts import find_conflict_candidate_pairs
from app.mainai_executive.architectural_gravity import detect_architectural_gravity
from app.mainai_executive.idea_incubation import transition_idea_disposition
from app.mainai_executive.judgment import JudgmentAction, decide_judgment
from app.mainai_executive.kill_criteria import evaluate_kill_criteria
from app.mainai_executive.meta_improvement import (
    lookup_behavioral_lessons,
    record_behavioral_lesson_from_founder_correction,
)
from app.mainai_executive.rejected_idea_guard import check_recently_rejected
from app.mainai_executive.retrieval import StrengthWeights, rank_by_strength
from app.mainai_executive.strategic_compression import compress_into_program
from app.mainai_executive.why_graph import list_triggered_decision_debt
from app.mainai_executive.wip_awareness import default_wip_limit, should_defer_new_work
from app.memory_threads.service import add_member, create_thread
from app.models.mainai_execution import MainAIGoal, MainAIPlan, MainAITask, MainAITaskStatus
from app.models.user import User
from app.work_candidates.service import record_work_candidate

from tests.backend.mainai.test_work_candidates import _owner_with_entity


def _owner(db) -> User:
    user = User(email=f"scenario-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(user)
    db.flush()
    return user


def _in_flight_task(db, owner_id):
    goal = MainAIGoal(owner_id=owner_id, title="p", original_instruction="s", created_by="test")
    db.add(goal)
    db.flush()
    plan = MainAIPlan(owner_id=owner_id, goal_id=goal.id, version=1, rationale="test", created_by="test")
    db.add(plan)
    db.flush()
    task = MainAITask(
        owner_id=owner_id, goal_id=goal.id, plan_id=plan.id, description="d", task_type="repo_edit",
        status=MainAITaskStatus.running,
    )
    db.add(task)
    db.flush()
    return task


def _idea(db, owner_id, *, disposition="unknown", content="an idea"):
    task = _in_flight_task(db, owner_id)
    execution = record_execution(db, owner_id=owner_id, task_id=task.id, idempotency_key=f"exec-{uuid.uuid4()}", role="builder")
    return record_idea(
        db, owner_id=owner_id, execution_id=execution.id, idea_kind="idea", content=content,
        disposition=disposition,
        disposition_reason="seed" if disposition in {"accepted", "rejected"} else None,
        idempotency_key=f"idea-{uuid.uuid4()}",
    )


def _correction(db, owner_id, content):
    return record_founder_memory(
        db, owner_id=owner_id, note_type="correction", content=content,
        idempotency_key=f"corr-{uuid.uuid4()}", authority="founder", basis="manual",
    )


# --- A. Repeated correction -> WIP-aware incubation, not over-parallelization. --------------


def test_scenario_a_wip_over_limit_biases_incubate_using_real_in_flight_tasks(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    for _ in range(default_wip_limit()):
        _in_flight_task(superuser_db, owner.id)
    superuser_db.commit()

    should_defer, reason = should_defer_new_work(superuser_db, owner_id=owner.id)
    assert should_defer is True

    decision = decide_judgment(
        confidence=0.9, evidence_strength=0.8, founder_originated=False,
        wip_signal=(should_defer, reason),
    )
    assert decision.action == JudgmentAction.INCUBATE
    assert decision.action not in (JudgmentAction.SPEAK, JudgmentAction.PROPOSE_BIGGER)


# --- B. MainAI proposes bigger architecture unprompted, high confidence + stakes. -----------


def test_scenario_b_propose_bigger_and_the_pattern_is_durably_recorded(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    decision = decide_judgment(
        confidence=0.9, evidence_strength=0.85, founder_originated=False, stakes=0.9, urgency=0.5,
        founder_attention_cost=0.3,
    )
    assert decision.action == JudgmentAction.PROPOSE_BIGGER

    # The founder's own repeated "think bigger" correction is recorded as a durable behavioral
    # lesson -- decide_judgment() itself stays pure (never reads it), but the pattern is
    # inspectable/retrievable for a caller to have already tuned PROPOSE_BIGGER_STAKES_BAR from.
    note = _correction(superuser_db, owner.id, "MainAI undersold a big opportunity as a small item -- think bigger.")
    superuser_db.commit()
    lesson = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note, root_cause="PROPOSE_BIGGER_STAKES_BAR was too high.",
        affected_component="judgment_temperament",
        general_rule="Propose the bigger initiative more readily when confidence and stakes are both high.",
        applies_to=["judgment"], created_by="test", fix="Lower PROPOSE_BIGGER_STAKES_BAR.",
    )
    superuser_db.commit()
    found = lookup_behavioral_lessons(superuser_db, component="judgment_temperament")
    assert lesson.id in [row.id for row in found]


# --- C. Twice-rejected idea does not get re-raised absent new evidence. ---------------------


def test_scenario_c_rejected_idea_stays_quiet_then_speaks_again_with_new_evidence(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    rejected = _idea(superuser_db, owner.id, disposition="unknown", content="use approach Q for the ingest pipeline")
    superuser_db.commit()
    transition_idea_disposition(
        superuser_db, owner_id=owner.id, idea_id=rejected.id, disposition="rejected",
        idempotency_key=f"rej-{uuid.uuid4()}", reason="approach Q was tried and abandoned",
    )
    superuser_db.commit()

    guard = check_recently_rejected(superuser_db, owner_id=owner.id, candidate_title="use approach Q for the ingest pipeline")
    assert guard["is_likely_duplicate_of_rejected"] is True

    quiet = decide_judgment(
        confidence=0.9, evidence_strength=0.9, founder_originated=False,
        rejected_guard_signal=guard, has_new_evidence_since_rejection=False,
    )
    assert quiet.action == JudgmentAction.STAY_QUIET

    proceeds = decide_judgment(
        confidence=0.9, evidence_strength=0.9, founder_originated=False,
        rejected_guard_signal=guard, has_new_evidence_since_rejection=True,
    )
    assert proceeds.action != JudgmentAction.STAY_QUIET


# --- D. Currentness -- honest scope check, not the literal "newer auto-wins" claim. ---------


def test_scenario_d_retrieval_favors_newer_but_conflicting_lessons_are_never_auto_resolved(superuser_db):
    from datetime import datetime, timedelta

    owner = _owner(superuser_db)
    superuser_db.commit()

    # Retrieval layer: a newer item of equal confidence outranks an older one -- this IS how
    # "the founder's newer statement surfaces first" is actually implemented.
    old_item = {"object_type": "founder_memory_note", "object_ref": "old", "state": "active", "last_activated_at": datetime.utcnow() - timedelta(days=60)}
    new_item = {"object_type": "founder_memory_note", "object_ref": "new", "state": "active", "last_activated_at": datetime.utcnow()}
    ranked = rank_by_strength(
        [old_item, new_item],
        confidence_by_key={("founder_memory_note", "old"): 0.6, ("founder_memory_note", "new"): 0.6},
        weights=StrengthWeights(confidence=0.5, recency=0.5, contradiction=0.0),
    )
    assert ranked[0].object_ref == "new"

    # But two genuinely CONTRADICTING behavioral lessons are never auto-resolved by recency --
    # both must be independently discoverable as a real conflict candidate pair, exactly like
    # any other engineering lesson. This is a deliberate, safer scope decision worth flagging:
    # the founder's literal "newer wins" wording is implemented at the RETRIEVAL/ranking layer
    # only, never as an automatic override of an existing behavioral lesson's own status.
    note_a = _correction(superuser_db, owner.id, "Always defer to CHALLENGE when unsure.")
    note_b = _correction(superuser_db, owner.id, "Never defer to CHALLENGE when unsure.")
    superuser_db.commit()
    lesson_a = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note_a, root_cause="r1", affected_component="challenge_threshold",
        general_rule="Always challenge when uncertain.", applies_to=["judgment"], created_by="test", fix="f1",
    )
    lesson_b = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note_b, root_cause="r2", affected_component="challenge_threshold",
        general_rule="Never challenge when uncertain.", applies_to=["judgment"], created_by="test", fix="f2",
    )
    superuser_db.commit()
    pairs = find_conflict_candidate_pairs(superuser_db, lessons=[lesson_a, lesson_b])
    assert len(pairs) == 1
    # Both lessons are STILL 'active' at this point -- only the (async, provider-backed)
    # detect_conflict()/mark_conflict() step (deliberately not exercised in this hermetic test,
    # same as it already exists and is proven elsewhere) would move them to disputed, and even
    # then it marks BOTH disputed, never picks a "newer" winner automatically.
    still_active = lookup_behavioral_lessons(superuser_db, component="challenge_threshold")
    assert {lesson_a.id, lesson_b.id} <= {row.id for row in still_active}


# --- E. Respectful challenge of a weak founder idea, full waterfall reaches rule 5. ----------


def test_scenario_e_challenges_weak_founder_idea_when_not_preempted_by_kill_or_rejection(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    candidate = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="a fresh, untested idea",
        idempotency_key=f"e-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()

    kill = evaluate_kill_criteria(superuser_db, owner_id=owner.id, work_candidate_id=candidate.id)
    assert kill["should_kill"] is False
    guard = check_recently_rejected(superuser_db, owner_id=owner.id, candidate_title=candidate.title)
    assert guard["is_likely_duplicate_of_rejected"] is False

    decision = decide_judgment(
        confidence=0.25, evidence_strength=0.2, founder_originated=True, stakes=0.6,
        kill_signal=kill, rejected_guard_signal=guard,
    )
    assert decision.action == JudgmentAction.CHALLENGE


# --- F. Ten related ideas while builders are busy -> one compressed program, no job spam. ---


def test_scenario_f_ten_related_candidates_while_over_wip_compress_to_one_program(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    for _ in range(default_wip_limit()):
        _in_flight_task(superuser_db, owner.id)
    candidate_ids = []
    for i in range(10):
        c = record_work_candidate(
            superuser_db, owner_id=owner.id, source_entity_id=entity.id, title=f"related item {i}",
            idempotency_key=f"f-{uuid.uuid4()}", classifier_strategy="test",
        )
        candidate_ids.append(c.id)
    superuser_db.commit()

    should_defer, _reason = should_defer_new_work(superuser_db, owner_id=owner.id)
    assert should_defer is True

    result = compress_into_program(
        superuser_db, owner_id=owner.id, idempotency_key=f"prog-{uuid.uuid4()}", work_candidate_ids=candidate_ids,
    )
    assert result["proposal_count"] == 1
    assert result["member_count"] == 10
    assert result["authorized"] is False


# --- G. Architectural gravity across independent threads -> feeds into compression. ---------


def test_scenario_g_gravity_detected_pattern_feeds_into_compression(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    shared_tag = f"gravity-{uuid.uuid4().hex[:8]}"
    candidate_ids = []
    for _ in range(3):
        candidate = record_work_candidate(
            superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="local design choice",
            idempotency_key=f"g-wc-{uuid.uuid4()}", classifier_strategy="test",
            provenance={"tags": [shared_tag]},
        )
        candidate_ids.append(candidate.id)
        thread = create_thread(superuser_db, owner_id=owner.id, idempotency_key=f"g-thread-{uuid.uuid4()}")
        add_member(
            superuser_db, owner_id=owner.id, thread_id=thread.id, member_kind="work_candidate",
            member_ref_id=candidate.id, actor_type="system",
        )
    superuser_db.commit()

    gravity = detect_architectural_gravity(superuser_db, owner_id=owner.id, min_independent_threads=3)
    assert gravity["gravity_detected"] is True
    assert gravity["auto_created"] is False

    # Following the gravity module's OWN recommendation: compress the underlying candidates.
    compressed = compress_into_program(
        superuser_db, owner_id=owner.id, idempotency_key=f"g-prog-{uuid.uuid4()}", work_candidate_ids=candidate_ids,
    )
    assert compressed["proposal_count"] == 1
    assert compressed["member_count"] == 3


# --- H. Successful pattern strengthens; unrelated lessons don't interfere. ------------------


def test_scenario_h_successful_pattern_lesson_persists_and_is_isolated_by_component(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    note = _correction(superuser_db, owner.id, "SPEAK worked well when confidence was high -- keep doing that.")
    superuser_db.commit()
    good_lesson = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note, root_cause="n/a -- confirms existing behavior is correct",
        affected_component="judgment_temperament",
        general_rule="Speaking on high-confidence, high-evidence items works well -- keep the bar as-is.",
        applies_to=["judgment"], created_by="test", fix="No change needed.",
    )
    superuser_db.commit()

    other_note = _correction(superuser_db, owner.id, "WIP limit of 5 has been fine.")
    superuser_db.commit()
    record_behavioral_lesson_from_founder_correction(
        superuser_db, note=other_note, root_cause="n/a", affected_component="wip_default",
        general_rule="Default WIP limit of 5 remains appropriate.", applies_to=["wip"], created_by="test", fix="No change.",
    )
    superuser_db.commit()

    judgment_lessons = lookup_behavioral_lessons(superuser_db, component="judgment_temperament")
    assert [row.id for row in judgment_lessons] == [good_lesson.id]


# --- I. Failed pattern repeats -> a real conflict candidate is discoverable. -----------------


def test_scenario_i_repeated_failure_produces_a_discoverable_conflict_candidate(superuser_db):
    owner = _owner(superuser_db)
    superuser_db.commit()
    note_a = _correction(superuser_db, owner.id, "kill_criteria fired on a candidate that was still genuinely alive.")
    superuser_db.commit()
    lesson_a = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note_a, root_cause="stale_age_days was too aggressive.",
        affected_component="kill_criteria_threshold",
        general_rule="Raise the stale-age threshold before recommending a kill.",
        applies_to=["kill_criteria"], created_by="test", fix="Raise DEFAULT_STALE_AGE_DAYS.",
    )
    superuser_db.commit()

    # A later, contradicting correction about the SAME component.
    note_b = _correction(superuser_db, owner.id, "kill_criteria is now too slow to flag genuinely dead work.")
    superuser_db.commit()
    lesson_b = record_behavioral_lesson_from_founder_correction(
        superuser_db, note=note_b, root_cause="stale_age_days was raised too far.",
        affected_component="kill_criteria_threshold",
        general_rule="Lower the stale-age threshold to flag dead work sooner.",
        applies_to=["kill_criteria"], created_by="test", fix="Lower DEFAULT_STALE_AGE_DAYS.",
    )
    superuser_db.commit()

    pairs = find_conflict_candidate_pairs(superuser_db, lessons=[lesson_a, lesson_b])
    assert len(pairs) == 1, "a repeated, contradicting correction about the same behavioral default must be discoverable"


# --- J. Founder returns after being offline -> a compressed brief, not activity spam. -------


def test_scenario_j_offline_return_gets_a_bounded_brief_not_a_flood(superuser_db):
    owner, entity = _owner_with_entity(superuser_db)
    superuser_db.commit()
    # Simulate a busy offline period: several unreviewed candidates, one dependency now ready.
    blocker = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="blocker",
        idempotency_key=f"j-blocker-{uuid.uuid4()}", classifier_strategy="test",
    )
    superuser_db.commit()
    from app.work_candidates.service import authorize_work_candidate

    authorize_work_candidate(superuser_db, owner_id=owner.id, candidate_id=blocker.id, authorized_by="founder")
    superuser_db.commit()
    waiting = record_work_candidate(
        superuser_db, owner_id=owner.id, source_entity_id=entity.id, title="waiting on blocker",
        idempotency_key=f"j-waiting-{uuid.uuid4()}", classifier_strategy="test",
        dependencies=[str(blocker.id)],
    )
    for i in range(25):
        record_work_candidate(
            superuser_db, owner_id=owner.id, source_entity_id=entity.id, title=f"unrelated noise {i}",
            idempotency_key=f"j-noise-{uuid.uuid4()}", classifier_strategy="test",
        )
    superuser_db.commit()

    debt = list_triggered_decision_debt(superuser_db, owner_id=owner.id, limit=10)
    assert len(debt["items"]) <= 10, "the brief must stay bounded even with a large backlog"
    triggers = {item["trigger"] for item in debt["items"]}
    assert "dependency_ready" in triggers
    dep_ready_ids = {item["id"] for item in debt["items"] if item["trigger"] == "dependency_ready"}
    assert str(waiting.id) in dep_ready_ids
