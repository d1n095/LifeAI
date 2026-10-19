"""Exact-set CI and review receipt attacks."""

from __future__ import annotations

import pytest

from first_cycle_acceptance._adapters import PRODUCTION
from first_cycle_acceptance._ci import (
    GITHUB_ACTIONS_APP_ID,
    REQUIRED,
    ReviewReceipt,
    evaluate_exact_set,
    evaluate_review,
    exact_set_runs,
    make_run,
)
from first_cycle_acceptance._matrix import FAIL, PASS
from first_cycle_acceptance.conftest import conclude

pytestmark = pytest.mark.first_cycle_acceptance


@pytest.fixture(autouse=True, scope="module")
def _review_privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id_for_review(db_session, make_verified_user):
    from sqlalchemy import text as sa_text

    from app.request_context import current_user_id as current_user_id_var

    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id

SHA = "b" * 40
OTHER = "c" * 40


def test_exact_approved_12_check_set_passes():
    runs = exact_set_runs(SHA)
    verdict = evaluate_exact_set(runs, sha=SHA)
    conclude(
        "CI-1 exact 12-check set with push and pull_request suites",
        PASS if (verdict.done and verdict.passed and len(REQUIRED) == 12) else FAIL,
        boundary="harness.ci.exact_set",
        detail=f"done={verdict.done} passed={verdict.passed} n_required={len(REQUIRED)} evidence={verdict.evidence}",
    )
    assert verdict.passed
    assert len(REQUIRED) == 12


def test_push_and_pr_suites_both_required():
    runs = exact_set_runs(SHA, suite_ids=(11,))
    verdict = evaluate_exact_set(runs, sha=SHA)
    conclude(
        "CI-2 push/PR suite semantics: one suite is not enough",
        PASS if (not verdict.done and not verdict.passed) else FAIL,
        boundary="harness.ci.suites",
        detail=f"done={verdict.done} passed={verdict.passed} {verdict.evidence['required'][:3]}",
    )
    assert not verdict.done


def test_wrong_app_does_not_satisfy():
    runs = exact_set_runs(SHA)
    for run in runs:
        if run["name"] == REQUIRED[0]["name"]:
            run["app"] = {"slug": "vercel", "id": 1}
    verdict = evaluate_exact_set(runs, sha=SHA)
    conclude(
        "CI-3 wrong app cannot substitute",
        PASS if (not verdict.passed) else FAIL,
        boundary="harness.ci.app_identity",
        detail=f"done={verdict.done} passed={verdict.passed} substitutions={verdict.evidence['rejected_substitutions']}",
    )
    assert not verdict.passed


def test_wrong_sha_is_ignored():
    runs = exact_set_runs(OTHER)
    verdict = evaluate_exact_set(runs, sha=SHA)
    conclude(
        "CI-4 wrong SHA is ignored",
        PASS if (not verdict.done and verdict.evidence["stale_sha_runs_ignored"] == len(runs)) else FAIL,
        boundary="harness.ci.sha",
        detail=f"done={verdict.done} stale={verdict.evidence['stale_sha_runs_ignored']}",
    )
    assert not verdict.done


def test_missing_check_is_pending_not_pass():
    runs = [r for r in exact_set_runs(SHA) if r["name"] != REQUIRED[-1]["name"]]
    verdict = evaluate_exact_set(runs, sha=SHA)
    conclude(
        "CI-5 missing required check is not a pass",
        PASS if (not verdict.done and not verdict.passed) else FAIL,
        boundary="harness.ci.missing",
        detail=f"done={verdict.done} passed={verdict.passed}",
    )
    assert not verdict.done


def test_skipped_and_neutral_do_not_pass():
    skipped = exact_set_runs(SHA, conclusion="skipped")
    neutral = exact_set_runs(SHA, conclusion="neutral")
    v_skip = evaluate_exact_set(skipped, sha=SHA)
    v_neutral = evaluate_exact_set(neutral, sha=SHA)
    oracle_holds = v_skip.done and not v_skip.passed and v_neutral.done and not v_neutral.passed
    conclude(
        "CI-6 skipped/neutral do not satisfy required checks",
        PASS if oracle_holds else FAIL,
        boundary="harness.ci.success_only",
        detail=f"skipped={v_skip.passed} neutral={v_neutral.passed}",
    )


def test_production_evaluate_check_runs_treats_skipped_as_pass():
    skipped = exact_set_runs(SHA, conclusion="skipped")
    prod = PRODUCTION["ci_wait"].symbols.get("evaluate_check_runs")
    if prod is None:
        conclude(
            "CI-6 production evaluate_check_runs success-only",
            FAIL,
            boundary="production.evaluate_check_runs",
            detail="evaluate_check_runs missing",
            production_module="app.mainai_execution.ci_wait",
        )
        return
    done, passed, _ = prod(skipped)
    conclude(
        "CI-6 production evaluate_check_runs success-only",
        FAIL if passed else PASS,
        boundary="production.evaluate_check_runs",
        detail=f"legacy evaluate_check_runs(skipped) -> done={done} passed={passed}",
        production_module="app.mainai_execution.ci_wait",
    )


def test_production_evaluate_required_check_runs_missing():
    exact_prod = PRODUCTION["ci_wait"].symbols.get("evaluate_required_check_runs")
    conclude(
        "CI-6b production evaluate_required_check_runs",
        PASS if exact_prod is not None else FAIL,
        boundary="production.evaluate_required_check_runs",
        detail=(
            "present"
            if exact_prod is not None
            else "symbol missing on this candidate (frozen #276 has legacy evaluate_check_runs only)"
        ),
        production_module="app.mainai_execution.ci_wait",
    )


def test_duplicate_retried_checks_use_latest_per_suite():
    runs = exact_set_runs(SHA)
    # Older failure in suite 11 for the first check, newer success already present.
    runs.append(
        make_run(
            name=REQUIRED[0]["name"],
            sha=SHA,
            conclusion="failure",
            suite_id=11,
            run_id=0,
            started_at="2026-10-17T00:00:00Z",
        )
    )
    verdict = evaluate_exact_set(runs, sha=SHA)
    conclude(
        "CI-7 duplicate/retried checks: latest per suite counts",
        PASS if (verdict.done and verdict.passed) else FAIL,
        boundary="harness.ci.retry",
        detail=f"done={verdict.done} passed={verdict.passed}",
    )
    assert verdict.passed


def test_more_than_thirty_results_still_require_the_set():
    runs = exact_set_runs(SHA)
    extras = [
        make_run(name=f"noise-{i}", sha=SHA, suite_id=99, run_id=1000 + i)
        for i in range(40)
    ]
    verdict = evaluate_exact_set(runs + extras, sha=SHA)
    conclude(
        "CI-8 >30 results: overflow recorded, exact set still required",
        PASS if (verdict.done and verdict.passed and verdict.evidence["overflow"]) else FAIL,
        boundary="harness.ci.overflow",
        detail=f"count={verdict.evidence['check_run_count']} overflow={verdict.evidence['overflow']}",
    )


def test_unexpected_vercel_is_evidence_only():
    runs = exact_set_runs(SHA)
    runs.append(
        make_run(
            name="Vercel Preview",
            sha=SHA,
            app_slug="vercel",
            app_id=1,
            suite_id=77,
            run_id=9999,
            conclusion="success",
        )
    )
    verdict = evaluate_exact_set(runs, sha=SHA)
    extras = verdict.evidence["unexpected_checks_evidence_only"]
    holds = verdict.passed and any(e["name"] == "Vercel Preview" for e in extras)
    conclude(
        "CI-9 unexpected Vercel checks are evidence only",
        PASS if holds else FAIL,
        boundary="harness.ci.vercel",
        detail=f"extras={extras}",
    )


def test_negative_review_stops():
    ok, detail = evaluate_review(
        receipt=ReviewReceipt(
            pr_number=1,
            head_sha=SHA,
            reviewer_identity="founder-reviewer",
            builder_identity="builder-agent",
            state="changes_requested",
        ),
        pr_head_sha=SHA,
        builder_identity="builder-agent",
    )
    conclude(
        "REV-1 negative review is not a GO",
        PASS if not ok else FAIL,
        boundary="harness.review.negative",
        detail=detail,
    )
    assert not ok


def test_changed_pr_head_invalidates_receipt():
    ok, detail = evaluate_review(
        receipt=ReviewReceipt(
            pr_number=1,
            head_sha=SHA,
            reviewer_identity="founder-reviewer",
            builder_identity="builder-agent",
            state="approved",
        ),
        pr_head_sha=OTHER,
        builder_identity="builder-agent",
    )
    conclude(
        "REV-2 changed PR head invalidates review receipt",
        PASS if not ok else FAIL,
        boundary="harness.review.head_sha",
        detail=detail,
    )
    assert not ok


def test_reviewer_builder_identity_separation():
    ok, detail = evaluate_review(
        receipt=ReviewReceipt(
            pr_number=1,
            head_sha=SHA,
            reviewer_identity="builder-agent",
            builder_identity="builder-agent",
            state="approved",
        ),
        pr_head_sha=SHA,
        builder_identity="builder-agent",
    )
    conclude(
        "REV-3 reviewer/builder identity separation",
        PASS if not ok else FAIL,
        boundary="harness.review.identity",
        detail=detail,
    )
    assert not ok


def test_missing_review_receipt_is_stop():
    ok, detail = evaluate_review(receipt=None, pr_head_sha=SHA, builder_identity="builder-agent")
    conclude(
        "REV-4 missing review receipt is STOP",
        PASS if not ok else FAIL,
        boundary="harness.review.missing",
        detail=detail,
    )
    assert not ok


def test_production_review_receipt_evaluator_is_present():
    evaluate = PRODUCTION["review_receipt"].symbols.get("evaluate_first_cycle_review_receipt")
    conclude(
        "REV-5 production evaluate_first_cycle_review_receipt",
        PASS if evaluate is not None else FAIL,
        boundary="production.review_receipt",
        detail="present" if evaluate is not None else "missing",
        production_module="app.mainai_execution.review_receipt",
    )
    assert evaluate is not None


def test_production_missing_review_stops(db_session, owner_id_for_review):
    evaluate = PRODUCTION["review_receipt"].symbols["evaluate_first_cycle_review_receipt"]
    builder = PRODUCTION["review_receipt"].symbols["PATH_A_BUILDER_IDENTITY"]
    review = evaluate(
        db_session,
        repository="d1n095/LifeAI",
        pr_number=1,
        head_sha=SHA,
        approved_base=OTHER,
        builder_identity=builder,
        instruction_hash_value="0" * 64,
        output_hash_value="1" * 64,
        envelope_context={},
        owner_id=owner_id_for_review,
    )
    conclude(
        "REV-6 production missing review is STOP",
        PASS if (not review.ok and review.merged is False) else FAIL,
        boundary="production.review_receipt.missing",
        detail=review.reason,
        production_module="app.mainai_execution.review_receipt",
    )
    assert not review.ok


def test_production_stop_module_never_reports_merged():
    truth_fn = PRODUCTION["review_receipt"].symbols.get("publication_truth")
    if truth_fn is None:
        conclude(
            "REV-7 publication truth never merged",
            FAIL,
            boundary="production.review_receipt.truth",
            detail="publication_truth missing",
            production_module="app.mainai_execution.review_receipt",
        )
        return
    truth = truth_fn(first_cycle_state="stop_before_merge", review_ok=True, ci_passed=True)
    holds = (
        truth["merged"] is False
        and truth["automatic_merge"] is False
        and truth["automatic_deploy"] is False
        and truth["draft_pr_is_not_merged"] is True
        and truth["push_is_not_merged"] is True
        and truth["ci_success_is_not_merged"] is True
        and truth["positive_review_is_not_merged"] is True
        and truth["subsequent_autonomous_job"] is False
    )
    conclude(
        "REV-7 publication truth never merged",
        PASS if holds else FAIL,
        boundary="production.review_receipt.truth",
        detail=str(truth),
        production_module="app.mainai_execution.review_receipt",
    )
    assert holds

