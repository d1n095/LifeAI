"""Teacher performance ledger + consultation / cost-aware selection policy.

No permanent global trust. FREE != TRUSTWORTHY. EXPENSIVE != CORRECT.
Correlated same-model double-checks are not independent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.mainai_school.evidence import EvidenceItem, EvidenceRank, resolve_local_vs_teacher


@dataclass
class TeacherLedgerEntry:
    teacher_id: str
    domain: str
    advice_later_correct: int = 0
    advice_failed: int = 0
    caught_local_mistakes: int = 0
    local_right_teacher_wrong: int = 0
    total_cost: float = 0.0
    total_latency_ms: float = 0.0
    calls: int = 0
    hallucination_flags: int = 0


_LEDGER: dict[str, TeacherLedgerEntry] = {}


def reset_teacher_ledger_for_tests() -> None:
    _LEDGER.clear()


def _key(teacher_id: str, domain: str) -> str:
    return f"{domain}::{teacher_id}"


def record_teacher_outcome(
    *,
    teacher_id: str,
    domain: str,
    later_verified_correct: bool | None,
    caught_local_mistake: bool = False,
    local_was_right_teacher_wrong: bool = False,
    cost: float = 0.0,
    latency_ms: float = 0.0,
    hallucination_flag: bool = False,
) -> TeacherLedgerEntry:
    k = _key(teacher_id, domain)
    e = _LEDGER.setdefault(k, TeacherLedgerEntry(teacher_id=teacher_id, domain=domain))
    e.calls += 1
    e.total_cost += cost
    e.total_latency_ms += latency_ms
    if later_verified_correct is True:
        e.advice_later_correct += 1
    elif later_verified_correct is False:
        e.advice_failed += 1
    if caught_local_mistake:
        e.caught_local_mistakes += 1
    if local_was_right_teacher_wrong:
        e.local_right_teacher_wrong += 1
    if hallucination_flag:
        e.hallucination_flags += 1
    return e


def teacher_domain_score(teacher_id: str, domain: str) -> dict[str, Any]:
    e = _LEDGER.get(_key(teacher_id, domain))
    if e is None:
        return {
            "teacher_id": teacher_id,
            "domain": domain,
            "score": 0.5,
            "evidence": "no_history",
            "global_trust": False,
        }
    denom = max(1, e.advice_later_correct + e.advice_failed)
    correctness = e.advice_later_correct / denom
    return {
        "teacher_id": teacher_id,
        "domain": domain,
        "score": correctness,
        "calls": e.calls,
        "local_right_teacher_wrong": e.local_right_teacher_wrong,
        "cost": e.total_cost,
        "hallucination_flags": e.hallucination_flags,
        "global_trust": False,
        "domain_specific": True,
    }


def teacher_diversity_ok(teacher_ids: list[str]) -> dict[str, Any]:
    """Same model family counted as correlated — not independent."""
    families = []
    for t in teacher_ids:
        fam = t.split("-")[0].split("_")[0].lower() if t else "unknown"
        families.append(fam)
    unique = sorted(set(families))
    return {
        "teachers": list(teacher_ids),
        "families": unique,
        "independent_enough": len(unique) >= 2 if len(teacher_ids) >= 2 else True,
        "same_model_is_not_independent": len(teacher_ids) >= 2 and len(unique) == 1,
    }


@dataclass
class TeacherConsultPlan:
    mode: str  # none | single | multi
    selected: list[str]
    reason: str
    prefer_free_or_cheap: bool
    require_stronger_evidence_if_disagree: bool = True
    notes: dict[str, Any] = field(default_factory=dict)


def plan_teacher_consultation(
    *,
    risk: str,  # low | medium | high
    domain: str,
    uncertain: bool,
    high_value: bool,
    available_teachers: list[dict[str, Any]],
    # each: {id, free_tier, remaining_quota, cost, domain_score, privacy_ok}
) -> TeacherConsultPlan:
    """Select teachers without treating free/cheap/expensive as correctness."""
    usable = [t for t in available_teachers if t.get("privacy_ok", True)]
    if not usable or (not uncertain and not high_value and risk == "low"):
        # Still allow optional single low-cost critique for learning.
        if risk == "low" and not uncertain and not high_value:
            cheap = sorted(usable, key=lambda t: (0 if t.get("free_tier") else 1, float(t.get("cost") or 1)))
            if cheap and domain:  # optional single
                return TeacherConsultPlan(
                    mode="single" if cheap else "none",
                    selected=[cheap[0]["id"]] if cheap else [],
                    reason="low_risk_optional_single_critique",
                    prefer_free_or_cheap=True,
                    notes={"free_is_not_trustworthy": True, "expensive_is_not_correct": True},
                )
        return TeacherConsultPlan(
            mode="none",
            selected=[],
            reason="local_sufficient_or_no_teachers",
            prefer_free_or_cheap=True,
            notes={"free_is_not_trustworthy": True},
        )

    # Rank by domain score then cost; never by price alone.
    ranked = sorted(
        usable,
        key=lambda t: (
            -float(t.get("domain_score") or 0.5),
            0 if t.get("free_tier") and (t.get("remaining_quota") or 0) > 0 else 1,
            float(t.get("cost") or 1.0),
        ),
    )
    if risk == "high" or high_value or (uncertain and risk != "low"):
        picks = []
        families: set[str] = set()
        for t in ranked:
            fam = str(t["id"]).split("-")[0].lower()
            if fam in families and len(picks) >= 1:
                continue  # prefer diversity
            picks.append(t["id"])
            families.add(fam)
            if len(picks) >= 2:
                break
        if len(picks) < 2 and len(ranked) >= 2:
            picks = [ranked[0]["id"], ranked[1]["id"]]
        return TeacherConsultPlan(
            mode="multi",
            selected=picks,
            reason="uncertain_or_high_value_multi_teacher",
            prefer_free_or_cheap=True,
            require_stronger_evidence_if_disagree=True,
            notes={
                "diversity": teacher_diversity_ok(picks),
                "majority_vote_forbidden": True,
                "free_is_not_trustworthy": True,
                "expensive_is_not_correct": True,
            },
        )
    return TeacherConsultPlan(
        mode="single",
        selected=[ranked[0]["id"]],
        reason="medium_single_teacher_critique",
        prefer_free_or_cheap=True,
        notes={"free_is_not_trustworthy": True},
    )


def adjudicate_multi_teacher(
    *,
    local_claim: str,
    teacher_positions: list[dict[str, Any]],
    evidence: list[EvidenceItem],
) -> dict[str, Any]:
    """Compare teachers; never majority-vote as truth."""
    answers = [str(p.get("answer") or "") for p in teacher_positions]
    agree = len(set(answers)) <= 1 and len(answers) >= 2
    # Use strongest teacher-side claim as foil if they agree; else unresolved among teachers.
    teacher_claim = answers[0] if answers else ""
    verdict = resolve_local_vs_teacher(
        local_claim=local_claim,
        teacher_claim=teacher_claim,
        evidence=evidence,
        teachers_agree=agree,
    )
    return {
        "teacher_agreement": agree,
        "majority_vote_used": False,
        "diversity": teacher_diversity_ok([str(p.get("teacher_id") or "") for p in teacher_positions]),
        "verdict": {
            "winner": verdict.winner,
            "reason": verdict.reason,
            "teacher_overruled": verdict.teacher_overruled,
            "local_overruled": verdict.local_overruled,
        },
        "three_models_agree_is_not_automatically_true": True,
    }
