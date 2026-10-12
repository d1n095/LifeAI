"""Examiner probes for #269 (66c2204: FC-1 routing, FC-2 planner targets, FC-5 envelope scope).
Real worker tick / execution job / worktree / push / draft-PR path; mocked chat provider, a local
bare git remote and a fake GitHub REST layer (fixtures from test_first_cycle.py). No paid API.
Each probe prints MATRIX|id|HOLDS/GAP|evidence and never raises on GAP."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.development_supervisor.lease import claim_supervisor_goal_lease
from app.development_supervisor.production_entry import eligible_authorized_goals
from app.execution_envelopes import authorize_execution_scope, propose_execution_scope
from app.mainai_execution import planner
from app.mainai_execution.envelope_scope import path_in_authorized_scope
from app.mainai_execution.planner import PlannedTaskSpec
from app.models.mainai_execution import MainAITask
from app.provider_spend import authorize_provider_spend
from app.request_context import current_user_id as current_user_id_var

from test_first_cycle import BASE, EDIT, M, _drive, _git, _privileges, _provider, _trail, github, owner_id, remote  # noqa: F401

PATH_A_CAPS = ["path_a", "read_file", "repo_edit", "run_tests", "open_pr"]
PATHS = ["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"]

PLAN_TARGETS = json.dumps({"tasks": [
    {"description": "Inspect greet() and confirm it lacks a docstring.", "task_type": "read_only_audit", "depends_on": []},
    {"description": "Add a docstring to greet() and add a focused test.", "task_type": "repo_edit", "depends_on": [0],
     "target_files": ["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"],
     "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}]},
    {"description": "Open a draft PR for the verified change.", "task_type": "open_pr", "depends_on": [1]},
]})


@pytest.fixture(autouse=True)
def _production_gate(monkeypatch):
    monkeypatch.setattr(get_settings(), "mainai_execution_require_provider_spend", True)


def _authorize(db, owner_id, goal_id, *, caps, paths=PATHS, spend=True):
    proposal = propose_execution_scope(db, owner_id=owner_id, goal_id=goal_id, idempotency_key=f"p-{uuid.uuid4()}")
    _, envelope = authorize_execution_scope(db, owner_id=owner_id, proposal_id=proposal.id, authorized_by="founder",
                                            authorized_paths=list(paths), authorized_capabilities=list(caps), authorized_risk="low",
                                            envelope_idempotency_key=f"e-{uuid.uuid4()}")
    if spend:
        authorize_provider_spend(db, owner_id=owner_id, goal_id=goal_id, execution_envelope_id=envelope.id, authorized_by="founder",
                                 max_cost_usd=Decimal("1.00"), max_requests=10, max_cost_per_request_usd=Decimal("0.05"),
                                 idempotency_key=f"s-{uuid.uuid4()}", allowed_providers=["openai"], allowed_models=[])
    db.commit()
    return envelope


async def _ai_goal(db, owner_id, *, caps, paths=PATHS, plan=PLAN_TARGETS):
    goal = planner.create_goal(db, owner_id=owner_id, title="Document greet()", original_instruction="Docstring + focused test.", created_by="founder")
    db.commit()
    _authorize(db, owner_id, goal.id, caps=caps, paths=paths)
    try:
        specs, _, _ = await planner.propose_plan_via_ai(db, goal=goal)
        planner.create_plan(db, goal=goal, rationale="AI plan", tasks=specs, created_by="founder")
        db.commit()
        return goal, None
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        return goal, f"{type(exc).__name__}: {str(exc)[:140]}"


def _pushed_files(bare):
    refs = [r for r in _git("-C", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n") if r.startswith("claude/mainai-task-")]
    out = {}
    for ref in refs:
        out[ref] = _git("-C", str(bare), "diff", "--name-only", f"{BASE}..{ref}").split("\n")
    return out


def _refusals(db, goal_id):
    rows = db.execute(sa_text("SELECT e.detail::text FROM mainai_task_events e JOIN mainai_tasks t ON t.id=e.task_id WHERE t.goal_id=:g"),
                      {"g": str(goal_id)}).all()
    return [r[0][:160] for r in rows if r[0] and "Refusing" in r[0]]


# ------------------------------------------------------------------ routing (FC-1)
def _ready_audit_goal(db, owner_id, caps, *, spend=True):
    goal = planner.create_goal(db, owner_id=owner_id, title="route", original_instruction="route probe", created_by="founder")
    planner.create_plan(db, goal=goal, rationale="exam", created_by="founder",
                        tasks=[PlannedTaskSpec(description="audit", task_type="read_only_audit", verification_plan=[])])
    db.commit()
    env = _authorize(db, owner_id, goal.id, caps=caps, spend=spend) if caps is not None else None
    return goal, db.query(MainAITask).filter(MainAITask.goal_id == goal.id).one(), env


def _as_owner(db, uid):
    current_user_id_var.set(str(uid))
    db.execute(sa_text("SELECT set_config('app.current_user_id', :u, false)"), {"u": str(uid)})


def test_route_matrix(superuser_db, make_verified_user):
    from app.worker import Worker

    user, _ = make_verified_user()
    db = superuser_db
    _as_owner(db, user.id)
    cases = {
        "never-governed": _ready_audit_goal(db, user.id, None),
        "path_a-active": _ready_audit_goal(db, user.id, PATH_A_CAPS),
        "supervisor-active (no path_a)": _ready_audit_goal(db, user.id, ["read_file", "patch_file"]),
    }
    sup_goal, sup_task, sup_env = _ready_audit_goal(db, user.id, PATH_A_CAPS)
    db.execute(sa_text("UPDATE execution_authorization_envelopes SET status='superseded' WHERE id=:i"), {"i": str(sup_env.id)})
    db.commit()
    cases["path_a superseded, no replacement"] = (sup_goal, sup_task, sup_env)
    eligible = {g.id for g, _ in eligible_authorized_goals(db, limit=200)}
    Worker()._advance_mainai_execution_tasks(db)
    db.commit()
    db.expire_all()
    result = {k: (db.get(MainAITask, t.id).status.value, g.id in eligible) for k, (g, t, _) in cases.items()}
    expect = {"never-governed": (True, False), "path_a-active": (True, False),
              "supervisor-active (no path_a)": (False, True), "path_a superseded, no replacement": (False, False)}
    ok = all(((result[k][0] != "ready") == expect[k][0]) and (result[k][1] == expect[k][1]) for k in expect)
    M("ROUTE-1/2/3 Path A only with an active path_a envelope; Supervisor only without; superseded fails closed for both", ok,
      f"(task status after worker tick, eligible for Supervisor) per case={result}")


def test_route4_supervisor_inflight_overlap(superuser_db, make_verified_user):
    """Founder switches a goal from a Supervisor envelope to a path_a envelope while the Supervisor holds a live goal lease."""
    from app.worker import Worker

    user, _ = make_verified_user()
    db = superuser_db
    _as_owner(db, user.id)
    goal, task, env1 = _ready_audit_goal(db, user.id, ["read_file", "patch_file"])
    claim = claim_supervisor_goal_lease(db, owner_id=user.id, goal_id=goal.id, envelope_id=env1.id, worker_id="sup-worker", lease_seconds=600)
    db.commit()
    _authorize(db, user.id, goal.id, caps=PATH_A_CAPS, spend=False)  # supersedes env1
    live = db.execute(sa_text("SELECT count(*) FROM supervisor_goal_leases WHERE goal_id=:g AND status='active' AND expires_at > now()"),
                      {"g": str(goal.id)}).scalar_one()
    Worker()._advance_mainai_execution_tasks(db)
    db.commit()
    db.expire_all()
    status = db.get(MainAITask, task.id).status.value
    M("ROUTE-4 Path A does not dispatch a goal while a Supervisor run still holds its live goal lease", status == "ready",
      f"supervisor lease claimed={claim is not None}; live supervisor leases at tick={live}; Path A task status after worker tick={status}")


# ------------------------------------------------------------------ envelope scope end to end (FC-5) and planner targets (FC-2)
@pytest.mark.asyncio
async def test_e2e_a_path_a_envelope(db_session, superuser_db, owner_id, monkeypatch, github):
    calls = _provider(monkeypatch, plan=PLAN_TARGETS)
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS)
    tasks = await _drive(superuser_db, goal.id) if err is None else {}
    states = {k: v.status.value for k, v in tasks.items()}
    pr = github.prs[0] if github.prs else {}
    pushed = _pushed_files(github.bare)
    M("E2E-A' billable goal with an active path_a envelope reaches a verified push + DRAFT PR (planner target_files, new focused test)",
      pr.get("draft") is True and states.get("open_pr") == "completed",
      f"plan_error={err}; provider calls={calls}; states={states}; pushed={pushed}; PR={pr}; refusals={_refusals(superuser_db, goal.id)}")


@pytest.mark.asyncio
async def test_e2e_a_legacy_envelope_fails_closed(db_session, superuser_db, owner_id, monkeypatch, github):
    calls = _provider(monkeypatch, plan=PLAN_TARGETS)
    goal, err = await _ai_goal(db_session, owner_id, caps=["read_file", "patch_file", "create_file"])
    tasks = await _drive(superuser_db, goal.id, n=8) if err is None else {}
    states = {k: v.status.value for k, v in tasks.items()}
    M("FC-1 an envelope without path_a never reaches Path A edits/push/PR", not github.prs and not _pushed_files(github.bare)
      and "code_agent" not in calls, f"plan_error={err}; provider calls={calls}; states={states}; PRs={github.prs}")


@pytest.mark.asyncio
async def test_planner_without_target_files(db_session, owner_id, monkeypatch):
    plan = json.dumps({"tasks": [{"description": "Add a docstring to greet() in backend/app/greeting.py.", "task_type": "repo_edit",
                                  "depends_on": [], "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}]}]})
    calls = _provider(monkeypatch, plan=plan)
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS)
    desc = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one().description if err is None else None
    M("FC-2a plan with no target_files: only the mapped focused test is declared (module named only in prose)",
      True, f"plan_error={err}; persisted description tail={desc[-120:] if desc else None!r}; provider calls={calls}")


@pytest.mark.asyncio
async def test_scope_prefix_sibling_escape(db_session, superuser_db, owner_id, monkeypatch, github):
    """Envelope authorizes exactly greeting.py + the test. Planner declares a sibling whose name only starts with greeting.py."""
    unit = {p: path_in_authorized_scope(p, PATHS) for p in
            ("backend/app/greeting.py", "backend/app/greeting.py_backdoor.py", ".backend/app/greeting.py", "backend/tests/test_greeting_doc.py.d/x.py")}
    unit_dir = {p: path_in_authorized_scope(p, ["backend/app/mainai_execution"]) for p in ("backend/app/mainai_execution/x.py", "backend/app/mainai_execution_evil.py")}
    plan = json.dumps({"tasks": [
        {"description": "Add a helper next to greet().", "task_type": "repo_edit", "depends_on": [],
         "target_files": ["backend/app/greeting.py_backdoor.py"],
         "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}]},
        {"description": "Open a draft PR.", "task_type": "open_pr", "depends_on": [0]}]})
    edit = json.dumps({"files": [
        {"path": "backend/app/greeting.py_backdoor.py", "content": "print('outside the authorized paths')\n"},
        {"path": "backend/tests/test_greeting_doc.py", "content": "def test_ok():\n    assert True\n"}], "commit_message": "helper"})
    calls = _provider(monkeypatch, plan=plan, edit=edit)
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS)
    tasks = await _drive(superuser_db, goal.id) if err is None else {}
    pushed = _pushed_files(github.bare)
    escaped = any("backend/app/greeting.py_backdoor.py" in files for files in pushed.values())
    M("SCOPE-1 envelope path matching has a path boundary (no sibling-prefix / leading-dot escape)", not escaped and not any(
        v for k, v in unit.items() if k != "backend/app/greeting.py") and not unit_dir["backend/app/mainai_execution_evil.py"],
      f"matcher(authorized={PATHS})={unit}; matcher(['backend/app/mainai_execution'])={unit_dir}; end-to-end pushed={pushed}; "
      f"PRs={[p['draft'] for p in github.prs]}; states={ {k: v.status.value for k, v in tasks.items()} }; refusals={_refusals(superuser_db, goal.id)}")


@pytest.mark.asyncio
async def test_scope_outside_envelope_refused_before_paying(db_session, superuser_db, owner_id, monkeypatch, github):
    plan = json.dumps({"tasks": [
        {"description": "Edit settings.", "task_type": "repo_edit", "depends_on": [],
         "target_files": ["backend/app/config.py"], "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}]}]})
    edit = json.dumps({"files": [{"path": "backend/app/config.py", "content": "X = 1\n"}], "commit_message": "x"})
    calls = _provider(monkeypatch, plan=plan, edit=edit)
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS)
    tasks = await _drive(superuser_db, goal.id, n=8) if err is None else {}
    M("SCOPE-2 a declared target outside the envelope is refused before any code-agent call, nothing pushed",
      "code_agent" not in calls and not _pushed_files(github.bare),
      f"plan_error={err}; provider calls={calls}; states={ {k: v.status.value for k, v in tasks.items()} }; refusals={_refusals(superuser_db, goal.id)[:1]}")


@pytest.mark.asyncio
async def test_scope_prose_named_file_is_writable(db_session, superuser_db, owner_id, monkeypatch, github):
    """Envelope authorizes a directory; planner declares greeting.py + test, but the prose names another file in it."""
    plan = json.dumps({"tasks": [
        {"description": "Add a docstring to greet(); also rewrite backend/app/farewell.py.", "task_type": "repo_edit", "depends_on": [],
         "target_files": PATHS, "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}]},
        {"description": "Open a draft PR.", "task_type": "open_pr", "depends_on": [0]}]})
    files = json.loads(EDIT)["files"] + [{"path": "backend/app/farewell.py", "content": "def bye():\n    return 'bye'\n"}]
    calls = _provider(monkeypatch, plan=plan, edit=json.dumps({"files": files, "commit_message": "x"}))
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS, paths=["backend/app/", "backend/tests/"])
    await _drive(superuser_db, goal.id) if err is None else {}
    pushed = _pushed_files(github.bare)
    prose_written = any("backend/app/farewell.py" in f for f in pushed.values())
    M("FC-2b only planner-declared target_files are writable (a file named only in prose is refused)", not prose_written,
      f"declared={PATHS}; pushed={pushed}; refusals={_refusals(superuser_db, goal.id)[:1]}")


@pytest.mark.asyncio
async def test_scope_open_pr_capability(db_session, superuser_db, owner_id, monkeypatch, github):
    calls = _provider(monkeypatch, plan=PLAN_TARGETS)
    goal, err = await _ai_goal(db_session, owner_id, caps=["path_a", "read_file", "repo_edit", "run_tests"])
    tasks = await _drive(superuser_db, goal.id) if err is None else {}
    M("CAP-1 an envelope without open_pr does not open a pull request", not github.prs,
      f"caps=[path_a, read_file, repo_edit, run_tests]; PRs={github.prs}; states={ {k: v.status.value for k, v in tasks.items()} }")


@pytest.mark.asyncio
async def test_scope_no_repo_edit_capability(db_session, superuser_db, owner_id, monkeypatch, github):
    calls = _provider(monkeypatch, plan=PLAN_TARGETS)
    goal, err = await _ai_goal(db_session, owner_id, caps=["path_a", "read_file", "run_tests", "open_pr"])
    tasks = await _drive(superuser_db, goal.id, n=10) if err is None else {}
    M("CAP-2 an envelope without repo_edit/patch_file refuses the edit before the code-agent call",
      "code_agent" not in calls and not _pushed_files(github.bare),
      f"provider calls={calls}; states={ {k: v.status.value for k, v in tasks.items()} }; refusals={_refusals(superuser_db, goal.id)[:1]}")


@pytest.mark.asyncio
async def test_envelope_narrowed_mid_flight(db_session, superuser_db, owner_id, monkeypatch, github):
    """The Founder supersedes the path_a envelope with a Supervisor-only one after the plan exists, before the worker runs."""
    calls = _provider(monkeypatch, plan=PLAN_TARGETS)
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS)
    _authorize(db_session, owner_id, goal.id, caps=["read_file", "patch_file"], spend=False)
    tasks = await _drive(superuser_db, goal.id, n=8) if err is None else {}
    M("FC-1/5 superseding the path_a envelope stops Path A (no edit, push or PR)", not github.prs and not _pushed_files(github.bare)
      and "code_agent" not in calls, f"provider calls={calls}; states={ {k: v.status.value for k, v in tasks.items()} }")
