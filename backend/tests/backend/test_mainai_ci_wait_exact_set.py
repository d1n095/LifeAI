"""Exact-set, event-bound CI wait: MainAI waits only for the immutable required-check contract
stored on the wait, bound to exact SHA, exact GitHub App identity, exact workflow file and the
exact set of GitHub event suites (push / pull_request) that the activation stage requires.

A. expected_events(): stage x head x base matrix (no deadlock, no single-suite overclaim).
B. evaluate_required_check_runs(): adversarial matrix -- dual suite (one whole suite missing can
   never pass), missing / in-progress / skipped / neutral, wrong app, wrong app id, wrong
   workflow, unbound suite, wrong event, stale SHA, SHA case, Vercel/extras, re-runs, failure in
   one suite, empty contract.
C. Contract storage: digest over the whole contract (checks + stage + branches + events);
   tampering, event/stage disagreement and legacy empty conditions fail closed.
D. Pagination: poll_ci_wait() through the real GitHubClient follows every page of check runs
   and workflow runs (37 check runs across 2 pages, failure on page 2).
"""

import pytest
import httpx
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.integrations.github_client import GitHubClient
from app.mainai_execution import planner
from app.mainai_execution.ci_wait import evaluate_required_check_runs, poll_ci_wait, start_ci_wait, suite_bindings
from app.mainai_execution.planner import PlannedTaskSpec
from app.mainai_execution.required_checks import (
    ACTIVATION_REQUIRED_CHECKS,
    CI_STAGE_BRANCH_PUSHED,
    CI_STAGE_PR_OPEN,
    GITHUB_ACTIONS_APP_ID,
    GITHUB_ACTIONS_APP_SLUG,
    RequiredCheck,
    RequiredCheckContractError,
    expected_events,
    normalize_required_checks,
    required_checks_from_condition,
    required_checks_payload,
)
from app.models.mainai_execution import MainAITask
from app.models.mainai_wait import MainAITaskWaitStatus
from app.request_context import current_user_id as current_user_id_var

SHA = "a" * 40
OTHER_SHA = "b" * 40
HEAD = "claude/mainai-task-00000000-0000-4000-8000-000000000000"
BASE = "mainai/integration-founder-supervised-v1"
NAMES = [c.name for c in ACTIVATION_REQUIRED_CHECKS]
PUSH, PR, OTHER_WF, VERCEL = 1, 2, 3, 4
CI = ".github/workflows/ci.yml"
SUITES = {
    PUSH: {"event": "push", "path": CI},
    PR: {"event": "pull_request", "path": CI},
    OTHER_WF: {"event": "push", "path": ".github/workflows/other.yml"},
}
BOTH = ("push", "pull_request")


def _run(name, *, suite, sha=SHA, conclusion="success", status="completed", slug=GITHUB_ACTIONS_APP_SLUG, app_id=GITHUB_ACTIONS_APP_ID, run_id=None):
    _run.counter += 1
    return {
        "id": run_id if run_id is not None else _run.counter,
        "name": name,
        "head_sha": sha,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
        "app": {"slug": slug, "id": app_id},
        "check_suite": {"id": suite},
    }


_run.counter = 0


def _green(suites=(PUSH, PR)):
    return [_run(n, suite=s) for s in suites for n in NAMES]


def _eval(runs, *, events=BOTH, required=ACTIVATION_REQUIRED_CHECKS, suites=SUITES, sha=SHA):
    return evaluate_required_check_runs(runs, sha=sha, required_checks=required, required_events=events, suites=suites)


def _state(evidence, name, event):
    return next(r["state"] for r in evidence["required"] if r["name"] == name and r["event"] == event)


# ---------------------------------------------------------------- A. stage / event matrix
@pytest.mark.parametrize(
    "stage, head, base, expected",
    [
        (CI_STAGE_PR_OPEN, HEAD, BASE, BOTH),                          # PR into the activation integration branch
        (CI_STAGE_PR_OPEN, HEAD, "claude/det-kommer-mer-879lcm", BOTH),
        (CI_STAGE_PR_OPEN, HEAD, "release/x", ("push",)),               # PR base not covered -> PR suite can never exist
        (CI_STAGE_BRANCH_PUSHED, HEAD, None, ("push",)),                # no PR yet -> never require a PR suite
        (CI_STAGE_BRANCH_PUSHED, HEAD, BASE, ("push",)),
        (CI_STAGE_PR_OPEN, "feature/x", BASE, ("pull_request",)),       # head not pushed under a CI filter
    ],
)
def test_expected_events_matrix(stage, head, base, expected):
    assert expected_events(stage=stage, head_branch=head, base_branch=base) == expected


@pytest.mark.parametrize(
    "stage, head, base",
    [
        ("unknown_stage", HEAD, BASE),
        (CI_STAGE_PR_OPEN, None, BASE),
        (CI_STAGE_PR_OPEN, HEAD, None),
        (CI_STAGE_PR_OPEN, "feature/x", "release/x"),   # no suite can ever run -> refuse instead of timing out
        (CI_STAGE_BRANCH_PUSHED, "feature/x", None),
    ],
)
def test_unsatisfiable_or_incomplete_stage_is_a_contract_error(stage, head, base):
    with pytest.raises(RequiredCheckContractError):
        expected_events(stage=stage, head_branch=head, base_branch=base)


# ---------------------------------------------------------------- B. evaluation matrix
def test_both_suites_green_passes():
    done, passed, ev = _eval(_green())
    assert (done, passed) == (True, True)
    assert len(ev["required"]) == 12 * 2 and all(r["state"] == "passed" for r in ev["required"])


def test_pr_suite_entirely_missing_never_passes():
    """The examiner's dual-suite overclaim: push suite all green, PR suite has no runs yet."""
    done, passed, ev = _eval(_green(suites=(PUSH,)))
    assert (done, passed) == (False, False)
    assert all(_state(ev, n, "pull_request") == "missing" for n in NAMES)
    assert all(_state(ev, n, "push") == "passed" for n in NAMES)


def test_push_suite_entirely_missing_never_passes():
    assert _eval(_green(suites=(PR,)))[:2] == (False, False)


def test_branch_pushed_stage_needs_only_the_push_suite():
    assert _eval(_green(suites=(PUSH,)), events=("push",))[:2] == (True, True)


def test_failure_in_one_suite_fails_even_if_the_other_passed():
    runs = _green(suites=(PUSH,)) + [_run(n, suite=PR, conclusion="failure" if n == NAMES[4] else "success") for n in NAMES]
    done, passed, ev = _eval(runs)
    assert (done, passed) == (True, False)
    assert _state(ev, NAMES[4], "pull_request") == "failed"


def test_missing_single_required_check_is_pending():
    runs = [r for r in _green() if not (r["name"] == NAMES[7] and r["check_suite"]["id"] == PR)]
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert _state(ev, NAMES[7], "pull_request") == "missing"


def test_in_progress_is_pending():
    runs = _green()
    runs.append(_run(NAMES[0], suite=PUSH, status="in_progress", run_id=10_000))  # newer attempt still running
    assert _eval(runs)[:2] == (False, False)


@pytest.mark.parametrize("conclusion", ["skipped", "neutral", "failure", "cancelled", "timed_out", "action_required", "stale", "future_value"])
def test_non_success_conclusion_fails(conclusion):
    runs = _green()
    runs.append(_run(NAMES[3], suite=PR, conclusion=conclusion, run_id=20_000))  # latest attempt in that suite
    done, passed, ev = _eval(runs)
    assert (done, passed) == (True, False)
    assert _state(ev, NAMES[3], "pull_request") == "failed"


def test_contract_may_explicitly_allow_skipped():
    required = (RequiredCheck(name="Optional job", allowed_conclusions=("success", "skipped")),)
    runs = [_run("Optional job", suite=s, conclusion="skipped") for s in (PUSH, PR)]
    assert _eval(runs, required=required)[:2] == (True, True)


def test_same_name_wrong_app_never_substitutes():
    runs = [r for r in _green() if r["name"] != NAMES[4]]
    runs += [_run(NAMES[4], suite=VERCEL, slug="vercel", app_id=8329), _run(NAMES[4], suite=PUSH, slug="vercel", app_id=8329)]
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert any(r["reason"] == "app_identity" and r["app_slug"] == "vercel" for r in ev["rejected_runs"])


def test_same_slug_wrong_app_id_never_substitutes():
    runs = [r for r in _green() if r["name"] != NAMES[1]]
    runs += [_run(NAMES[1], suite=s, app_id=999_999) for s in (PUSH, PR)]
    assert _eval(runs)[:2] == (False, False)


def test_same_name_from_another_workflow_never_substitutes():
    runs = [r for r in _green() if not (r["name"] == NAMES[2] and r["check_suite"]["id"] == PUSH)]
    runs.append(_run(NAMES[2], suite=OTHER_WF))
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert _state(ev, NAMES[2], "push") == "missing"
    assert any(r["reason"] == "workflow_path" for r in ev["rejected_runs"])


def test_run_in_an_unbound_suite_never_counts():
    runs = [r for r in _green() if not (r["name"] == NAMES[2] and r["check_suite"]["id"] == PR)]
    runs.append(_run(NAMES[2], suite=99))
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert any(r["reason"] == "unbound_suite" for r in ev["rejected_runs"])


def test_push_run_does_not_satisfy_the_pull_request_slot():
    runs = _green(suites=(PUSH,)) + [_run(n, suite=PUSH, run_id=50_000 + i) for i, n in enumerate(NAMES)]
    assert _eval(runs)[:2] == (False, False)


def test_stale_sha_runs_and_suites_are_ignored():
    runs = [_run(n, suite=s, sha=OTHER_SHA) for s in (PUSH, PR) for n in NAMES]
    done, passed, ev = _eval(runs)
    assert (done, passed) == (False, False)
    assert ev["stale_sha_runs_ignored"] == 24


def test_sha_comparison_is_case_insensitive():
    runs = [_run(n, suite=s, sha=SHA.upper()) for s in (PUSH, PR) for n in NAMES]
    assert _eval(runs, sha=SHA)[:2] == (True, True)


def test_suite_bindings_only_for_the_wait_sha():
    wr = [
        {"check_suite_id": PUSH, "event": "push", "path": CI, "head_sha": SHA.upper()},
        {"check_suite_id": PR, "event": "pull_request", "path": CI, "head_sha": OTHER_SHA},
    ]
    assert suite_bindings(wr, sha=SHA) == {PUSH: {"event": "push", "path": CI}}


def test_vercel_and_extras_are_evidence_only():
    runs = _green() + [
        _run("Vercel Preview Comments", suite=VERCEL, slug="vercel", app_id=8329, conclusion="failure"),
        _run("VPS bootstrap scripts — shellcheck + dry-run", suite=PUSH, conclusion="skipped"),
        _run("Some new job", suite=PR, status="in_progress"),
    ]
    done, passed, ev = _eval(runs)
    assert (done, passed) == (True, True)
    assert {e["name"] for e in ev["unexpected_checks_evidence_only"]} == {
        "Vercel Preview Comments", "VPS bootstrap scripts — shellcheck + dry-run", "Some new job",
    }


def test_rerun_supersedes_an_earlier_attempt_in_the_same_event():
    runs = _green()
    runs.append(_run(NAMES[0], suite=PUSH, conclusion="failure", run_id=1))  # older failed attempt
    assert _eval(runs)[:2] == (True, True)


def test_empty_contract_fails_closed():
    assert _eval(_green(), required=())[:2] == (True, False)
    assert _eval(_green(), events=())[:2] == (True, False)


def test_contract_normalization_rejects_duplicates_and_missing_identity():
    with pytest.raises(RequiredCheckContractError):
        normalize_required_checks([])
    with pytest.raises(RequiredCheckContractError):
        normalize_required_checks([{"name": "x", "app_slug": "github-actions"}] * 2)
    with pytest.raises(RequiredCheckContractError):
        normalize_required_checks([{"name": "x"}])


def test_default_contract_is_the_twelve_activation_checks():
    assert len(ACTIVATION_REQUIRED_CHECKS) == 12
    assert all(
        c.app_slug == "github-actions" and c.app_id == 15368 and c.allowed_conclusions == ("success",) and c.workflow_path == CI
        for c in ACTIVATION_REQUIRED_CHECKS
    )
    assert "Vercel Preview Comments" not in NAMES and "Vercel" not in NAMES


# ---------------------------------------------------------------- C. contract storage
def test_payload_binds_stage_branches_and_events():
    payload = required_checks_payload(stage=CI_STAGE_PR_OPEN, head_branch=HEAD, base_branch=BASE)
    assert payload["required_events"] == ["push", "pull_request"]
    checks, events = required_checks_from_condition(payload)
    assert events == BOTH and len(checks) == 12


@pytest.mark.parametrize(
    "mutate",
    [
        lambda c: c.update(required_checks=c["required_checks"][:1]),
        lambda c: c.update(required_events=["push"]),                    # drop the PR suite, keep digest
        lambda c: c.update(stage=CI_STAGE_BRANCH_PUSHED),               # stage changed
        lambda c: c.update(base_branch="release/x"),                     # base changed
        lambda c: c.update(required_checks_sha256="0" * 64),
        lambda c: c.pop("required_events"),
    ],
)
def test_tampered_contract_fails_closed(mutate):
    condition = required_checks_payload(stage=CI_STAGE_PR_OPEN, head_branch=HEAD, base_branch=BASE)
    mutate(condition)
    with pytest.raises(RequiredCheckContractError):
        required_checks_from_condition(condition)


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


def _task_and_job(db_session, owner_id):
    from app.mainai_execution import executor

    goal = planner.create_goal(db_session, owner_id=owner_id, title="g", original_instruction="ci", created_by="test")
    planner.create_plan(db_session, goal=goal, rationale="r", created_by="test",
                        tasks=[PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])])
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    job = executor.dispatch_ready_task(db_session, task=task, goal=goal, dispatched_by="test")
    db_session.commit()
    return task, job


def _configure(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")
    monkeypatch.setattr(settings, "github_token", "fake-not-used")


def _workflow_runs(sha):
    return [
        {"check_suite_id": PUSH, "event": "push", "path": CI, "head_sha": sha},
        {"check_suite_id": PR, "event": "pull_request", "path": CI, "head_sha": sha},
    ]


def test_start_ci_wait_stores_the_full_contract(db_session, owner_id):
    task, job = _task_and_job(db_session, owner_id)
    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha=SHA, head_branch=HEAD, base_branch=BASE)
    assert wait.condition == required_checks_payload(stage=CI_STAGE_PR_OPEN, head_branch=HEAD, base_branch=BASE)
    assert wait.condition["required_events"] == ["push", "pull_request"]


def test_start_ci_wait_refuses_an_unsatisfiable_contract(db_session, owner_id):
    task, job = _task_and_job(db_session, owner_id)
    with pytest.raises(RequiredCheckContractError):
        start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha=SHA, head_branch="feature/x", base_branch="release/x")


@pytest.mark.asyncio
async def test_poll_with_only_push_suite_stays_pending(db_session, owner_id, monkeypatch):
    _configure(monkeypatch)
    task, job = _task_and_job(db_session, owner_id)
    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha=SHA, head_branch=HEAD, base_branch=BASE)

    async def _runs(self, ref):
        return _green(suites=(PUSH,))

    async def _wfr(self, sha):
        return _workflow_runs(sha)[:1]

    monkeypatch.setattr(GitHubClient, "list_check_runs", _runs)
    monkeypatch.setattr(GitHubClient, "list_workflow_runs_for_sha", _wfr)
    wait = await poll_ci_wait(db_session, wait)
    assert wait.status == MainAITaskWaitStatus.pending


@pytest.mark.asyncio
async def test_tampered_stored_contract_fails_closed_at_poll(db_session, owner_id, monkeypatch):
    _configure(monkeypatch)
    task, job = _task_and_job(db_session, owner_id)
    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha=SHA, head_branch=HEAD, base_branch=BASE)
    tampered = dict(wait.condition)
    tampered["required_events"] = ["push"]
    wait.condition = tampered

    async def _runs(self, ref):
        return _green(suites=(PUSH,))

    async def _wfr(self, sha):
        return _workflow_runs(sha)

    monkeypatch.setattr(GitHubClient, "list_check_runs", _runs)
    monkeypatch.setattr(GitHubClient, "list_workflow_runs_for_sha", _wfr)
    wait = await poll_ci_wait(db_session, wait)
    assert wait.status == MainAITaskWaitStatus.failed
    assert "contract_error" in wait.evidence


@pytest.mark.asyncio
async def test_legacy_wait_without_contract_fails_closed(db_session, owner_id, monkeypatch):
    _configure(monkeypatch)
    task, job = _task_and_job(db_session, owner_id)
    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha=SHA, head_branch=HEAD, base_branch=BASE)
    wait.condition = {}

    async def _never(self, *a):
        raise AssertionError("a wait without a contract must not be evaluated against GitHub state")

    monkeypatch.setattr(GitHubClient, "list_check_runs", _never)
    wait = await poll_ci_wait(db_session, wait)
    assert wait.status == MainAITaskWaitStatus.failed
    assert "contract_error" in wait.evidence


# ---------------------------------------------------------------- D. pagination through the real client
@pytest.mark.asyncio
async def test_pagination_beyond_thirty_runs_sees_a_late_page_failure(db_session, owner_id, monkeypatch):
    _configure(monkeypatch)
    task, job = _task_and_job(db_session, owner_id)
    wait = start_ci_wait(db_session, task=task, job_id=job.id, repo="d1n095/LifeAI", sha=SHA, head_branch=HEAD, base_branch=BASE)
    runs = _green() + [
        _run("Vercel Preview Comments", suite=VERCEL, slug="vercel", app_id=8329),
        _run("Combined container — build, run, verify", suite=PUSH, conclusion="skipped"),
    ]
    runs += [_run(f"Extra evidence job {i}", suite=PUSH, conclusion="skipped") for i in range(10)]
    runs.append(_run(NAMES[7], suite=PR, conclusion="failure", run_id=90_000))  # newest attempt, on page 2
    assert len(runs) == 37
    pages = [runs[:30], runs[30:]]
    wf_pages = [_workflow_runs(SHA)[:1], _workflow_runs(SHA)[1:]]

    async def _fake_request(self, method, url, *, headers=None, json=None, **kwargs):
        request = httpx.Request(method, url)
        path = str(url)
        assert method == "GET"
        if "/check-runs" in path:
            if "page=2" in path:
                return httpx.Response(200, request=request, json={"total_count": 37, "check_runs": pages[1]})
            nxt = f"https://api.github.com/repos/d1n095/LifeAI/commits/{SHA}/check-runs?per_page=100&page=2"
            return httpx.Response(200, request=request, headers={"Link": f'<{nxt}>; rel="next"'}, json={"total_count": 37, "check_runs": pages[0]})
        if "/actions/runs" in path:
            if "page=2" in path:
                return httpx.Response(200, request=request, json={"total_count": 2, "workflow_runs": wf_pages[1]})
            nxt = f"https://api.github.com/repos/d1n095/LifeAI/actions/runs?head_sha={SHA}&per_page=100&page=2"
            return httpx.Response(200, request=request, headers={"Link": f'<{nxt}>; rel="next"'}, json={"total_count": 2, "workflow_runs": wf_pages[0]})
        return httpx.Response(404, request=request, json={"message": path})

    monkeypatch.setattr(httpx.AsyncClient, "request", _fake_request)
    wait = await poll_ci_wait(db_session, wait)
    assert wait.evidence["check_run_count"] == 37
    assert set(wait.evidence["bound_suites"]) == {str(PUSH), str(PR)}
    assert wait.status == MainAITaskWaitStatus.failed
    assert _state(wait.evidence, NAMES[7], "pull_request") == "failed"
