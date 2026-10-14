"""Examiner probes added for #271 (05df3db: SCOPE-1 / CAP-1 / FC-2b / ROUTE-4 remediation).
Same harness as test_path_a_269.py (real worker tick, execution job, worktree, push, draft PR;
mocked chat provider, local bare remote, fake GitHub REST). MATRIX lines; never raise on GAP."""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from app.development_supervisor.production_entry import eligible_authorized_goals
from app.mainai_execution import planner
from app.mainai_execution.envelope_scope import path_in_authorized_scope
from app.mainai_execution.plan_insertion import InsertedTaskSpec, insert_plan_tasks
from app.models.mainai_execution import MainAIPlan, MainAITask

from test_first_cycle import EDIT, M, _drive, _privileges, _provider, github, owner_id, remote  # noqa: F401
from test_path_a_269 import PATH_A_CAPS, _ai_goal, _as_owner, _authorize, _production_gate, _pushed_files, _ready_audit_goal, _refusals  # noqa: F401


def test_scope_matcher_edge_matrix():
    files = ["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"]
    must_allow = {
        ("backend/app/greeting.py", "f"), ("./backend/app/greeting.py", "f"),
        ("backend/tests/a.py", "d/"), ("backend/tests/sub/b.py", "d/"), ("backend/tests/a.py", "d/**"),
    }
    cases = [
        # (candidate, grant-kind)
        ("backend/app/greeting.py", "f"), ("./backend/app/greeting.py", "f"),
        ("backend/app/greeting.py_backdoor.py", "f"), (".backend/app/greeting.py", "f"), ("/backend/app/greeting.py", "f"),
        ("..backend/app/greeting.py", "f"), ("backend/app/greeting.py/x.py", "f"), ("backend/app/Greeting.py", "f"),
        ("backend/app/../app/greeting.py", "f"), ("backend/app/greeting.py.d/x.py", "f"),
        ("backend/tests/a.py", "d/"), ("backend/tests/sub/b.py", "d/"), ("backend/testsx/a.py", "d/"), ("backend/tests", "d/"),
        ("backend/tests/.hidden/x.py", "d/"), ("backend/tests/a.py", "d/**"), ("backend/tests_evil/a.py", "d/**"),
        ("backend/tests/a.py", "bare-dir"), ("backend/app/x.py", "**"), ("backend/app/x.py", "/"), ("backend/app/x.py", "./"),
        ("backend/app/x.py", "*"), ("backend/app/x.py", "backend/app/*"),
    ]
    grants = {"f": files, "d/": ["backend/tests/"], "d/**": ["backend/tests/**"], "bare-dir": ["backend/tests"],
              "**": ["**"], "/": ["/"], "./": ["./"], "*": ["*"], "backend/app/*": ["backend/app/*"]}
    results = {f"{c} | {k}": path_in_authorized_scope(c, grants[k]) for c, k in cases}
    wrong = {key: got for (c, k), (key, got) in zip(cases, results.items()) if got != ((c, k) in must_allow)}
    M("SCOPE-1b matcher: exact files, explicit `/`/`/**` directory boundary only; every lookalike / dot / absolute / wildcard variant refused",
      not wrong, f"unexpected results={wrong or 'none'}; full={results}")


def test_route5_reverse_overlap(superuser_db, make_verified_user):
    """Path A job is running; Founder supersedes the path_a envelope with a Supervisor envelope."""
    from app.worker import Worker

    user, _ = make_verified_user()
    db = superuser_db
    _as_owner(db, user.id)
    goal, task, _ = _ready_audit_goal(db, user.id, PATH_A_CAPS)
    Worker()._advance_mainai_execution_tasks(db)  # dispatches the Path A job
    db.commit()
    db.expire_all()
    status_before = db.get(MainAITask, task.id).status.value
    _authorize(db, user.id, goal.id, caps=["read_file", "patch_file"], spend=False)
    eligible = goal.id in {g.id for g, _ in eligible_authorized_goals(db, limit=500)}
    M("ROUTE-5 (reverse, informational) Supervisor eligibility while a Path A job for the goal is still in flight",
      True, f"Path A task status={status_before}; goal eligible for Supervisor after switch={eligible} "
            "(Supervisor cannot claim the running Path A job's lease; Path A re-checks the envelope at every step and fails closed before push)")


@pytest.mark.asyncio
async def test_fc2b_inserted_task_prose_marker(db_session, superuser_db, owner_id, monkeypatch, github):
    """insert_plan_tasks() has no structured target_files: a repo_edit task inserted with a prose
    'Target files:' line. Envelope authorizes backend/app/ and backend/tests/."""
    first = json.dumps({"tasks": [{"description": "Inspect greet().", "task_type": "read_only_audit", "depends_on": []}]})
    files = json.loads(EDIT)["files"] + [{"path": "backend/app/farewell.py", "content": "def bye():\n    return 'bye'\n"}]
    _provider(monkeypatch, plan=first, edit=json.dumps({"files": files, "commit_message": "x"}))
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS, paths=["backend/app/", "backend/tests/"], plan=first)
    plan = db_session.query(MainAIPlan).filter(MainAIPlan.goal_id == goal.id).one()
    inserted = insert_plan_tasks(
        db_session, goal=goal, plan=plan, authority_kind="founder_requirement",
        authorized_instruction_sha256=hashlib.sha256(goal.original_instruction.encode()).hexdigest(),
        idempotency_key=f"ins-{uuid.uuid4()}", source_type="examiner", source_ref="probe", reason="probe", requested_by="examiner",
        tasks=[InsertedTaskSpec(description="Add a docstring to greet().\nTarget files: backend/app/greeting.py backend/app/farewell.py",
                                task_type="repo_edit", verification_plan=({"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"},))])
    db_session.commit()
    await _drive(superuser_db, goal.id)
    pushed = _pushed_files(github.bare)
    written = any("backend/app/farewell.py" in f for f in pushed.values())
    M("FC-2b-ins an inserted task's prose 'Target files:' line does not grant edit authority (no structured target_files)",
      not written, f"plan_error={err}; inserted={len(inserted)}; pushed={pushed}; refusals={_refusals(superuser_db, goal.id)[:1]}")
