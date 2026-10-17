"""Exact-set CI wait: MainAI waits only for the immutable required-check contract stored on the
wait, bound to exact SHA and exact GitHub App identity.

A. evaluate_required_check_runs(): pure adversarial matrix (missing, in-progress, skipped,
   neutral, wrong-app substitution, app-id mismatch, stale SHA, Vercel/unexpected extras,
   re-run supersession within a suite, failure in a second suite, empty contract).
B. Contract storage: start_ci_wait() stores the set + digest; a tampered digest fails closed;
   a legacy wait with an empty condition is evaluated against the repository default contract.
C. Pagination: poll_ci_wait() through the real GitHubClient sees all 37+ runs across pages, so
   a failing required run on a later page is never missed.
"""

from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.integrations.github_client import GitHubClient
from app.mainai_execution import planner
from app.mainai_execution.ci_wait import evaluate_required_check_runs, poll_ci_wait, start_ci_wait
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.required_checks import (
    ACTIVATION_REQUIRED_CHECKS,
    GITHUB_ACTIONS_APP_ID,
    GITHUB_ACTIONS_APP_SLUG,
    RequiredCheck,
    RequiredCheckContractError,
    normalize_required_checks,
    required_checks_payload,
)
from app.models.mainai_execution import MainAITask
from app.models.mainai_wait import MainAITaskWaitStatus
from app.request_context import current_user_id as current_user_id_var

SHA = "a" * 40
OTHER_SHA = "b" * 40
NAMES = [c.name for c in ACTIVATION_REQUIRED_CHECKS]


def _run(name, *, sha=SHA, conclusion="success", status="completed", slug=GITHUB_ACTIONS_APP_SLUG, app_id=GITHUB_ACTIONS_APP_ID, suite=1, run_id=None):
    _run.counter += 1
    return {
        "id": run_id if run_id is not None else _run.counter,
        "name": name,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "app": {"slug": slug, "id": app_id},
        "check_suite": {"id": suite},
        "started_at": "2026-10-17T00:00:00Z",
    }


_run.counter = 0


def _all_green(suites=(1, 2)):
    return [_run(n, suite=s) for s in suites for n in NAMES]


def _eval(runs, required=ACTIVATION_REQUIRED_CHECKS, sha=SHA):
    return evaluate_required_check_runs(runs, sha=sha, required_checks=required)


def _state(evidence, name):
    return next(r["state"] for r in evidence["required"] if r["name"] == name)


# ---------------------------------------------------------------- A. pure matrix
def test_all_required_success_in_both_suites_passes():
    done, passed, ev = _eval(_all_green())
    assert (done, passed) == (True, True)
    assert all(r["state"] == "passed" for r in ev["required"])


def test_missing_required_check_is_pending_never_pass():
    runs = [r for r in _all_green() if r["name"] != "Backend — Alembic migration check"]
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert _state(ev, "Backend — Alembic migration check") == "missing"


def test_no_runs_at_all_is_pending():
    assert _eval([])[:2] == (False, False)


def test_in_progress_required_check_is_pending():
    runs = _all_green()
    runs[0] = _run(runs[0]["name"], status="in_progress", suite=runs[0]["check_suite"]["id"], run_id=10_000)
    assert _eval(runs)[:2] == (False, False)


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "failure", "cancelled", "timed_out", "action_required", "stale", "some_future_value"])
def test_non_success_conclusion_on_a_required_check_fails(conclusion):
    runs = _all_green(suites=(1,))
    runs[3] = _run(runs[3]["name"], conclusion=conclusion, suite=1, run_id=20_000)
    done, passed, ev = _eval(runs)
    assert (done, passed) == (True, False)
    assert _state(ev, runs[3]["name"]) == "failed"


def test_contract_may_explicitly_allow_skipped():
    required = (RequiredCheck(name="Optional job", allowed_conclusions=("success", "skipped")),)
    assert _eval([_run("Optional job", conclusion="skipped")], required=required)[:2] == (True, True)


def test_same_name_from_wrong_app_never_substitutes():
    runs = [r for r in _all_green() if r["name"] != "Backend — unit/integration tests"]
    runs.append(_run("Backend — unit/integration tests", slug="vercel", app_id=8329))
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert _state(ev, "Backend — unit/integration tests") == "missing"
    assert {"name": "Backend — unit/integration tests", "app_slug": "vercel", "app_id": 8329} in ev["rejected_substitutions"]


def test_same_slug_but_wrong_app_id_never_substitutes():
    runs = [r for r in _all_green() if r["name"] != "Frontend — npm audit"]
    runs.append(_run("Frontend — npm audit", app_id=999_999))
    assert _eval(runs)[:2] == (False, False)


def test_wrong_app_failure_does_not_fail_a_green_required_check():
    runs = _all_green() + [_run("Backend — unit/integration tests", slug="vercel", app_id=8329, conclusion="failure")]
    assert _eval(runs)[:2] == (True, True)


def test_runs_for_another_sha_are_ignored():
    runs = [_run(n, sha=OTHER_SHA) for n in NAMES]
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert ev["stale_sha_runs_ignored"] == len(NAMES)


def test_vercel_and_unexpected_checks_are_evidence_only():
    runs = _all_green() + [
        _run("Vercel Preview Comments", slug="vercel", app_id=8329, conclusion="failure"),
        _run("VPS bootstrap scripts — shellcheck + dry-run", conclusion="skipped"),
        _run("Some new job", status="in_progress"),
    ]
    done, passed, ev = _eval(runs)
    assert (done, passed) == (True, True)
    assert {e["name"] for e in ev["unexpected_checks_evidence_only"]} == {
        "Vercel Preview Comments", "VPS bootstrap scripts — shellcheck + dry-run", "Some new job",
    }


def test_rerun_supersedes_earlier_attempt_in_the_same_suite():
    runs = _all_green(suites=(1,))
    name = runs[0]["name"]
    runs.append(_run(name, conclusion="failure", suite=1, run_id=1))  # older attempt
    assert _eval(runs)[:2] == (True, True)


def test_failure_in_a_second_suite_fails_even_if_the_first_suite_passed():
    runs = _all_green(suites=(1,)) + [_run(NAMES[4], suite=2, conclusion="failure")]
    assert _eval(runs)[:2] == (True, False)


def test_empty_required_set_fails_closed():
    done, passed, ev = _eval(_all_green(), required=())
    assert (done, passed) == (True, False)
    assert "contract_error" in ev


def test_contract_normalization_rejects_duplicates_and_empty():
    with pytest.raises(RequiredCheckContractError):
        normalize_required_checks([])
    with pytest.raises(RequiredCheckContractError):
        normalize_required_checks([{"name": "x", "app_slug": "github-actions"}, {"name": "x", "app_slug": "github-actions"}])
    with pytest.raises(RequiredCheckContractError):
        normalize_required_checks([{"name": "x"}])  # no app identity


def test_default_contract_is_the_twelve_activation_checks():
    assert len(ACTIVATION_REQUIRED_CHECKS) == 12
    assert all(c.app_slug == "github-actions" and c.app_id == 15368 and c.allowed_conclusions == ("success",) for c in ACTIVATION_REQUIRED_CHECKS)
    assert "Vercel Preview Comments" not in NAMES and "Vercel" not in NAMES


# ---------------------------------------------------------------- B/C. durable wait + real client pagination
@pytest.fixture(autouse=True, scope="module")
def _privileges():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges, apply_mainai_job_runtime_privileges

    apply_mainai_job_runtime_privileges(migration_engine)
    apply_mainai_execution_privileges(migration_engine)


@pytest.fixture
def owner_id(db_session, make_verified_user):
    user, _ = make_verified_user()
    current_user_id_var.set(str(user.id))
    db_session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user.id)})
    return user.id


def _task(db_session, owner_id):
    """A real task + dispatched job (mainai_task_waits.job_id is a real FK)."""
    from app.mainai_execution import executor

    goal = planner.create_goal(db_session, owner_id=owner_id, title="g", original_instruction="ci", created_by="test")
    planner.create_plan(db_session, goal=goal, rationale="r", created_by="test",
                        tasks=[PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])])
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test")
    db_session.commit()
    task.job_id_for_test = job.id
    return task


def _configure(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")
    monkeypatch.setattr(settings, "github_token", "fake-not-used")


def test_start_ci_wait_stores_the_contract_and_digest(db_session, owner_id):
    import uuid

    task = _task(db_session, owner_id)
    wait = start_ci_wait(db_session, task=task, job_id=task.job_id_for_test, repo="d1n095/LifeAI", sha=SHA)
    assert wait.condition == required_checks_payload(ACTIVATION_REQUIRED_CHECKS)
    assert len(wait.condition["required_checks"]) == 12


@pytest.mark.asyncio
async def test_tampered_contract_fails_closed(db_session, owner_id, monkeypatch):
    import uuid

    _configure(monkeypatch)
    task = _task(db_session, owner_id)
    wait = start_ci_wait(db_session, task=task, job_id=task.job_id_for_test, repo="d1n095/LifeAI", sha=SHA)
    tampered = dict(wait.condition)
    tampered["required_checks"] = tampered["required_checks"][:1]  # drop 11 checks, keep old digest
    wait.condition = tampered

    async def _runs(self, ref):
        return _all_green()

    monkeypatch.setattr(GitHubClient, "list_check_runs", _runs)
    wait = await poll_ci_wait(db_session, wait)
    assert wait.status == MainAITaskWaitStatus.failed
    assert "contract_error" in wait.evidence


@pytest.mark.asyncio
async def test_legacy_wait_without_contract_uses_the_default_contract(db_session, owner_id, monkeypatch):
    import uuid

    _configure(monkeypatch)
    task = _task(db_session, owner_id)
    wait = start_ci_wait(db_session, task=task, job_id=task.job_id_for_test, repo="d1n095/LifeAI", sha=SHA)
    wait.condition = {}

    async def _runs(self, ref):
        return [_run("Some unrelated job")]  # the old "any runs, all green" semantics would PASS here

    monkeypatch.setattr(GitHubClient, "list_check_runs", _runs)
    wait = await poll_ci_wait(db_session, wait)
    assert wait.status == MainAITaskWaitStatus.pending
    assert wait.evidence.get("legacy_wait_default_contract") is True


@pytest.mark.asyncio
async def test_pagination_beyond_thirty_runs_sees_a_late_page_failure(db_session, owner_id, monkeypatch):
    """37 runs (as observed live on GitHub) split 30 + 7 across pages; the failing required
    run is on page 2. A page-1-only reader would see 30 green runs and still be missing
    checks; the real paginated client sees the failure and the wait fails."""
    import uuid

    _configure(monkeypatch)
    task = _task(db_session, owner_id)
    wait = start_ci_wait(db_session, task=task, job_id=task.job_id_for_test, repo="d1n095/LifeAI", sha=SHA)
    runs = _all_green() + [
        _run("Vercel Preview Comments", slug="vercel", app_id=8329),
        _run("Combined container — build, run, verify", conclusion="skipped"),
    ]
    assert len(runs) == 26
    runs += [_run(f"Extra evidence job {i}", conclusion="skipped") for i in range(10)]
    runs.append(_run(NAMES[7], suite=3, conclusion="failure"))  # last item -> page 2
    assert len(runs) == 37
    pages = [runs[:30], runs[30:]]

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        request = httpx.Request(method, url)
        path = str(url)
        assert method == "GET"
        if "page=2" in path:
            return httpx.Response(200, request=request, json={"total_count": 37, "check_runs": pages[1]})
        if "/check-runs" in path:
            nxt = f"https://api.github.com/repos/d1n095/LifeAI/commits/{SHA}/check-runs?per_page=100&page=2"
            return httpx.Response(200, request=request, headers={"Link": f'<{nxt}>; rel="next"'},
                                  json={"total_count": 37, "check_runs": pages[0]})
        return httpx.Response(404, request=request, json={"message": path})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    wait = await poll_ci_wait(db_session, wait)
    assert wait.evidence["check_run_count"] == 37
    assert wait.status == MainAITaskWaitStatus.failed
    assert _state(wait.evidence, NAMES[7]) == "failed"
