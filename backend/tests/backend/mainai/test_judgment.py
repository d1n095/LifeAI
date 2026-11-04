"""app.mainai_executive.judgment -- pure, deterministic communicative/behavioral judgment.
STAY_QUIET/agreement-is-never-default; a real should_kill signal changes the outcome; the
module structurally cannot mutate (it has no db access at all)."""

from __future__ import annotations

import inspect
import re

import pytest

from app.mainai_executive.judgment import (
    JudgmentAction,
    decide_judgment,
    judgment_as_dict,
)


def test_weak_founder_proposal_no_evidence_no_urgency_is_not_unconditional_speak_agreement():
    """The mandated property: a weak, unevidenced, low-stakes founder proposal must never come
    back as SPEAK (agreement/sycophancy is never the default)."""
    decision = decide_judgment(
        confidence=0.2,
        evidence_strength=0.15,
        founder_originated=True,
        urgency=0.0,
        stakes=0.1,
    )
    assert decision.action != JudgmentAction.SPEAK
    assert decision.action == JudgmentAction.STAY_QUIET
    assert decision.authorized is False


def test_weak_mainai_originated_idea_is_also_not_speak():
    decision = decide_judgment(
        confidence=0.1, evidence_strength=0.1, founder_originated=False, stakes=0.0, urgency=0.0
    )
    assert decision.action == JudgmentAction.STAY_QUIET


def test_founder_originated_weak_but_material_stakes_is_challenge_not_agreement():
    decision = decide_judgment(
        confidence=0.2, evidence_strength=0.2, founder_originated=True, stakes=0.6, urgency=0.0
    )
    assert decision.action == JudgmentAction.CHALLENGE
    assert decision.action != JudgmentAction.SPEAK


def test_strong_confidence_and_evidence_clears_bar_to_speak():
    decision = decide_judgment(
        confidence=0.8, evidence_strength=0.7, founder_originated=True, stakes=0.3, urgency=0.5,
        founder_attention_cost=0.2,
    )
    assert decision.action == JudgmentAction.SPEAK


def test_kill_signal_changes_the_outcome_versus_the_same_call_without_it():
    """Mutation-style: identical call, only kill_signal differs -- the signal must actually
    change the returned action, not just be accepted and ignored."""
    base_kwargs = dict(
        confidence=0.8, evidence_strength=0.7, founder_originated=True, stakes=0.3, urgency=0.5,
        founder_attention_cost=0.2,
    )
    without_kill = decide_judgment(**base_kwargs)
    assert without_kill.action == JudgmentAction.SPEAK

    with_high_confidence_kill = decide_judgment(
        **base_kwargs, kill_signal={"should_kill": True, "confidence": 0.95, "reasons": ["already dead"]}
    )
    assert with_high_confidence_kill.action == JudgmentAction.KILL
    assert with_high_confidence_kill.action != without_kill.action


def test_kill_signal_below_confidence_bar_escalates_instead_of_auto_killing():
    decision = decide_judgment(
        confidence=0.8, evidence_strength=0.7, founder_originated=True,
        kill_signal={"should_kill": True, "confidence": 0.55, "reasons": ["stale"]},
    )
    assert decision.action == JudgmentAction.ESCALATE


def test_kill_signal_present_but_should_kill_false_does_not_trigger_kill_path():
    decision = decide_judgment(
        confidence=0.8, evidence_strength=0.7, founder_originated=True,
        kill_signal={"should_kill": False, "confidence": 0.9, "reasons": ["no kill signal found"]},
    )
    assert decision.action == JudgmentAction.SPEAK


def test_rejected_guard_hit_without_new_evidence_stays_quiet():
    decision = decide_judgment(
        confidence=0.9, evidence_strength=0.9, founder_originated=False,
        rejected_guard_signal={"is_likely_duplicate_of_rejected": True, "reason": "matches a rejected idea"},
        has_new_evidence_since_rejection=False,
    )
    assert decision.action == JudgmentAction.STAY_QUIET


def test_rejected_guard_hit_with_new_evidence_is_not_forced_quiet():
    """New evidence overrides the rejected-idea-guard bias -- proceeds to normal evaluation."""
    decision = decide_judgment(
        confidence=0.9, evidence_strength=0.9, founder_originated=False,
        rejected_guard_signal={"is_likely_duplicate_of_rejected": True, "reason": "matches a rejected idea"},
        has_new_evidence_since_rejection=True,
    )
    assert decision.action != JudgmentAction.STAY_QUIET


def test_wip_over_limit_biases_toward_incubate_over_speak_when_idea_is_strong():
    decision = decide_judgment(
        confidence=0.9, evidence_strength=0.8, founder_originated=False,
        wip_signal=(True, "5 MainAITask row(s) already in flight (limit=5)"),
    )
    assert decision.action == JudgmentAction.INCUBATE
    assert decision.action not in (JudgmentAction.SPEAK, JudgmentAction.PROPOSE_BIGGER)


def test_wip_over_limit_biases_toward_defer_when_idea_is_weak():
    decision = decide_judgment(
        confidence=0.2, evidence_strength=0.2, founder_originated=False,
        wip_signal=(True, "over limit"),
    )
    assert decision.action == JudgmentAction.DEFER


def test_wip_under_limit_does_not_force_defer_or_incubate():
    decision = decide_judgment(
        confidence=0.9, evidence_strength=0.8, founder_originated=True, stakes=0.2,
        founder_attention_cost=0.2, urgency=0.5,
        wip_signal=(False, "capacity available"),
    )
    assert decision.action == JudgmentAction.SPEAK


def test_overlapping_active_item_recommends_combine():
    decision = decide_judgment(
        confidence=0.7, evidence_strength=0.6, founder_originated=False,
        overlaps_existing_active_item=True,
    )
    assert decision.action == JudgmentAction.COMBINE


def test_mainai_originated_high_confidence_high_stakes_proposes_bigger():
    decision = decide_judgment(
        confidence=0.9, evidence_strength=0.85, founder_originated=False, stakes=0.9, urgency=0.5,
        founder_attention_cost=0.3,
    )
    assert decision.action == JudgmentAction.PROPOSE_BIGGER


def test_high_attention_cost_low_urgency_defers_even_when_bar_is_cleared():
    decision = decide_judgment(
        confidence=0.9, evidence_strength=0.8, founder_originated=True, stakes=0.2,
        founder_attention_cost=0.9, urgency=0.1,
    )
    assert decision.action == JudgmentAction.DEFER


@pytest.mark.parametrize("field", ["confidence", "evidence_strength", "urgency", "stakes", "founder_attention_cost"])
def test_out_of_range_fraction_raises(field):
    kwargs = dict(confidence=0.5, evidence_strength=0.5, founder_originated=True, urgency=0.1, stakes=0.1, founder_attention_cost=0.5)
    kwargs[field] = 1.5
    with pytest.raises(ValueError):
        decide_judgment(**kwargs)


def test_every_decision_carries_a_non_opaque_reason_and_signals():
    decision = decide_judgment(confidence=0.9, evidence_strength=0.9, founder_originated=True, stakes=0.1, founder_attention_cost=0.1, urgency=0.5)
    assert decision.reason
    assert isinstance(decision.signals, dict)
    assert decision.signals  # never opaque -- always cites at least one real signal
    as_dict = judgment_as_dict(decision)
    assert as_dict["authorized"] is False
    assert as_dict["action"] == decision.action.value


def test_judgment_module_has_no_db_or_mutating_call_anywhere():
    """Structural: decide_judgment() has no db.add/db.commit/UPDATE, and the module has no
    actual import of sqlalchemy at all -- proof this is a PURE function by construction,
    matching (and exceeding) kill_criteria.py's own 'advisory only, structurally cannot
    mutate' proof. Uses `ast` (not a plain substring search) so the module's OWN prose --
    which legitimately discusses 'db: Session' and 'sqlalchemy' in English -- cannot produce a
    false failure the way a naive text search would."""
    import ast

    import app.mainai_executive.judgment as module

    source = inspect.getsource(module)
    assert not re.search(r"\bdb\.add\(", source)
    assert not re.search(r"\bdb\.commit\(", source)
    assert not re.search(r"\bdb\.flush\(", source)
    assert not re.search(r"\bUPDATE \w", source)

    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module.split(".")[0])
    assert "sqlalchemy" not in imported_modules
    assert "app" not in imported_modules  # no cross-module app.* import at all -- fully self-contained

    sig = inspect.signature(decide_judgment)
    assert "db" not in sig.parameters
