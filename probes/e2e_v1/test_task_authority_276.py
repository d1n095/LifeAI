"""#276 (09b87e9 / impl 29d230a) probes: TA-8b, TA-10 against the REAL code paths, plus staging,
symlink and history attacks on the publication boundary. Same harness as test_task_authority_273.py.
An "intruder" stands for anything other than the code agent that can touch the worktree between
write and commit (the TA-9 threat class). MATRIX lines; never raise on GAP."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from app.mainai_execution import planner
from app.mainai_execution.envelope_scope import load_structured_target_authority
from app.models.mainai_execution import MainAITask

from test_first_cycle import EDIT, M, _drive, _git, _privileges, _provider, _trail, github, owner_id, remote  # noqa: F401
from test_path_a_269 import PATH_A_CAPS, PATHS, _ai_goal, _production_gate, _pushed_files, _refusals  # noqa: F401
from test_task_authority_273 import DIRS, _lesson, _seed_guard  # noqa: F401


def _intruder(monkeypatch, action):
    """Run `action(worktree_path)` right before the real commit_worktree_changes()."""
    from app.mainai_execution import execution_job

    original = execution_job.commit_worktree_changes

    def _wrapped(db, worktree, *, message):
        action(Path(worktree.path))
        return original(db, worktree, message=message)

    monkeypatch.setattr(execution_job, "commit_worktree_changes", _wrapped)


def _seed_file(remote, rel, content):
    seed = remote.parent / "seed"
    p = seed / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    _git("add", "-A", cwd=seed)
    _git("commit", "-q", "-m", f"seed {rel}", cwd=seed)
    _git("push", "-q", "origin", "integration/founder-alpha-test-base", cwd=seed)


PLAN_DECL = json.dumps({"tasks": [
    {"description": "Docstring + test.", "task_type": "repo_edit", "depends_on": [], "target_files": PATHS,
     "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}]},
    {"description": "Open a draft PR.", "task_type": "open_pr", "depends_on": [0]}]})


# ---------------------------------------------------------------- TA-10 against the real function
def test_ta10_real_function_reports_both_sides_of_a_rename(tmp_path):
    from app.mainai_execution.worktree import git_changed_paths_no_renames

    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout  # noqa: E731
    run("init", "-q")
    (repo / "backend" / "app").mkdir(parents=True)
    (repo / "backend" / "app" / "security_guard.py").write_text("".join(f"line {i}\n" for i in range(50)))
    (repo / "keep.py").write_text("x = 1\n")
    run("add", "-A")
    run("-c", "user.name=s", "-c", "user.email=s@s", "commit", "-qm", "base")
    base = run("rev-parse", "HEAD").strip()
    run("mv", "backend/app/security_guard.py", "backend/app/greeting.py")
    run("rm", "-q", "keep.py")
    run("-c", "user.name=s", "-c", "user.email=s@s", "commit", "-qam", "rename+delete")
    got = sorted(git_changed_paths_no_renames(cwd=repo, base_sha=base))
    M("TA-10a git_changed_paths_no_renames exposes deletion and both sides of a rename",
      got == ["backend/app/greeting.py", "backend/app/security_guard.py", "keep.py"], f"got={got}")


@pytest.mark.asyncio
async def test_ta10_rename_onto_authorized_target_is_refused_end_to_end(db_session, superuser_db, owner_id, monkeypatch, github, remote):
    # The out-of-scope source is itself a valid greeting module, so the targeted test still
    # passes after the rename and the PUBLICATION boundary (actual changed files) is what decides.
    _seed_file(remote, "backend/app/security_guard.py",
               "def greet(name):\n    \"\"\"Return a greeting.\"\"\"\n    return f'hello {name}'\n"
               + "".join(f"GUARD_{i} = {i}\n" for i in range(60)))

    def rename(wt: Path):  # intruder renames the out-of-scope guard onto the authorized target
        (wt / "backend/app/greeting.py").unlink()
        os.rename(wt / "backend/app/security_guard.py", wt / "backend/app/greeting.py")

    _intruder(monkeypatch, rename)
    _provider(monkeypatch, plan=PLAN_DECL)
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS, paths=DIRS, plan=PLAN_DECL)
    await _drive(superuser_db, goal.id, n=10)
    pushed = _pushed_files(github.bare)
    M("TA-10b out-of-scope rename onto an authorized target is refused before push/PR", not pushed and not github.prs,
      f"plan_error={err}; pushed={pushed}; PRs={len(github.prs)}; refusals={_refusals(superuser_db, goal.id)[:1]}; trail={_trail(superuser_db, goal.id)}")


# ---------------------------------------------------------------- staging / object-type / history attacks
@pytest.mark.asyncio
async def test_symlink_at_authorized_target_object_type(db_session, superuser_db, owner_id, monkeypatch, github, remote):  # noqa: E501
    """Intruder replaces the authorized test file with a SYMLINK to an out-of-scope file. The changed
    path is authorized, but the published object is a link (mode 120000), not the reviewed content."""
    # A passing out-of-scope test exists in the base; the authorized test path becomes a link to it,
    # so verification passes and only the publication check can stop it.
    _seed_file(remote, "backend/tests/other_suite_passes.py", "def test_ok():\n    assert True\n")

    def link(wt: Path):
        target = wt / "backend/tests/test_greeting_doc.py"
        if target.exists() or target.is_symlink():
            target.unlink()
        os.symlink("other_suite_passes.py", target)

    _intruder(monkeypatch, link)
    _provider(monkeypatch, plan=PLAN_DECL)
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS, paths=DIRS, plan=PLAN_DECL)
    await _drive(superuser_db, goal.id, n=10)
    pushed = _pushed_files(github.bare)
    modes = {}
    for ref in pushed:
        out = _git("-C", str(github.bare), "ls-tree", "-r", ref, "backend/tests/test_greeting_doc.py")
        modes[ref] = out.split()[0] if out else None
    published_link = any(m == "120000" for m in modes.values())
    M("TA-13 (P2 probe) a symlink object at an authorized path is not published", not published_link,
      f"pushed={pushed}; tree modes={modes}; refusals={_refusals(superuser_db, goal.id)[:1]}; trail={_trail(superuser_db, goal.id)}")


@pytest.mark.asyncio
async def test_intermediate_commit_history(db_session, superuser_db, owner_id, monkeypatch, github, remote):
    """Intruder commits an out-of-scope file and then a commit deleting it, before the real commit.
    The net base..HEAD tree diff is clean, but the pushed history carries the out-of-scope blob."""
    def history(wt: Path):
        (wt / "backend/app/exfil.py").write_text("SECRET_COPY = 'out-of-scope content'\n")
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "-c", "user.name=i", "-c", "user.email=i@i", "commit", "-qm", "intruder add"], cwd=wt, check=True)
        subprocess.run(["git", "rm", "-q", "backend/app/exfil.py"], cwd=wt, check=True)
        subprocess.run(["git", "-c", "user.name=i", "-c", "user.email=i@i", "commit", "-qm", "intruder remove"], cwd=wt, check=True)

    _intruder(monkeypatch, history)
    _provider(monkeypatch, plan=PLAN_DECL)
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS, paths=DIRS, plan=PLAN_DECL)
    await _drive(superuser_db, goal.id, n=10)
    pushed = _pushed_files(github.bare)
    in_history = {}
    for ref in pushed:
        log = _git("-C", str(github.bare), "log", "--format=%s", "--name-only", f"integration/founder-alpha-test-base..{ref}")
        in_history[ref] = "backend/app/exfil.py" in log
    M("TA-12 (P2 probe) out-of-scope content cannot ride in intermediate commits of the published branch",
      not any(in_history.values()), f"pushed net diff={pushed}; out-of-scope file present in pushed history={in_history}")


# ---------------------------------------------------------------- TA-8b / TA-8c
@pytest.mark.asyncio
async def test_ta8b_negative_case_fails_before_remote_mutation(db_session, superuser_db, owner_id, monkeypatch, github, remote):
    _seed_guard(remote)
    _lesson(db_session)
    plan = json.dumps({"tasks": [{"description": "Docstring.", "task_type": "repo_edit", "depends_on": [], "target_files": ["backend/app/greeting.py"],
                                  "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_lesson_guard.py"}]},
                                 {"description": "Open a draft PR.", "task_type": "open_pr", "depends_on": [0]}]})
    weakened = {"path": "backend/tests/test_lesson_guard.py", "content": "def test_guard():\n    pass\n"}
    _provider(monkeypatch, plan=plan, edit=json.dumps({"files": [json.loads(EDIT)["files"][0], weakened], "commit_message": "x"}))
    goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS, paths=DIRS, plan=plan)
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id, MainAITask.task_type == "repo_edit").one_or_none()
    await _drive(superuser_db, goal.id, n=10)
    refs = [r for r in _git("-C", str(github.bare), "for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n") if r.startswith("claude/mainai-task-")]
    M("TA-8b' existing guard listed only in verification_plan stays verify-only; refused before any remote ref exists",
      not refs and not github.prs, f"authority={load_structured_target_authority(db_session, task) if task else None}; remote task refs={refs}; refusals={_refusals(superuser_db, goal.id)[:1]}")


@pytest.mark.asyncio
async def test_ta8c_new_focused_test_creatable(db_session, superuser_db, owner_id, monkeypatch, github):
    """Undeclared but genuinely NEW focused test (absent at base) remains creatable; also the declared-only case."""
    results = {}
    for label, targets in (("undeclared new focused test", ["backend/app/greeting.py"]), ("declared new test", PATHS)):
        plan = json.dumps({"tasks": [{"description": "Docstring + new test.", "task_type": "repo_edit", "depends_on": [], "target_files": targets,
                                      "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}]},
                                     {"description": "Open a draft PR.", "task_type": "open_pr", "depends_on": [0]}]})
        _provider(monkeypatch, plan=plan)
        goal, err = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS, paths=DIRS, plan=plan)
        await _drive(superuser_db, goal.id)
        results[label] = sorted({f for files in _pushed_files(github.bare).values() for f in files})
        github.prs.clear()
        for ref in [r for r in _git("-C", str(github.bare), "for-each-ref", "--format=%(refname:short)", "refs/heads").split("\n") if r.startswith("claude/mainai-task-")]:
            _git("-C", str(github.bare), "branch", "-D", ref)
    ok = all(v == ["backend/app/greeting.py", "backend/tests/test_greeting_doc.py"] for v in results.values())
    M("TA-8c a genuinely new focused test remains creatable (declared, or undeclared-but-absent-at-base)", ok, f"{results}")


@pytest.mark.asyncio
async def test_ta11_replan_fresh_authority_and_retry(db_session, owner_id, monkeypatch):
    from app.mainai_execution import executor
    from app.models.mainai_execution import MainAITaskStatus

    _provider(monkeypatch, plan=PLAN_DECL)
    goal, _ = await _ai_goal(db_session, owner_id, caps=PATH_A_CAPS, paths=PATHS, plan=PLAN_DECL)
    t1 = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id, MainAITask.task_type == "repo_edit").one()
    before = load_structured_target_authority(db_session, t1)
    t1.status = MainAITaskStatus.retryable_failed
    db_session.commit()
    executor.retry_task(db_session, task=t1)
    db_session.commit()
    after = load_structured_target_authority(db_session, t1)
    replan = json.dumps({"tasks": [{"description": "Docstring only.", "task_type": "repo_edit", "depends_on": [], "target_files": ["backend/app/greeting.py"],
                                    "verification_plan": [{"kind": "targeted_tests", "target": "tests/test_greeting_doc.py"}]}]})
    _provider(monkeypatch, plan=replan)
    specs, _, _ = await planner.propose_plan_via_ai(db_session, goal=goal)
    planner.create_plan(db_session, goal=goal, rationale="replan", tasks=specs, created_by="founder")
    db_session.commit()
    new = [t for t in db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).all() if t.id != t1.id and t.task_type == "repo_edit"]
    new_auth = load_structured_target_authority(db_session, new[-1]) if new else None
    M("TA-11' retry preserves structured authority; replan writes fresh created-event authority (declared only; focused test as undeclared)",
      before == after and new_auth and new_auth["target_files"] == ["backend/app/greeting.py"]
      and new_auth["undeclared_focused_tests"] == ["backend/tests/test_greeting_doc.py"] and new_auth["target_source"] == "planner",
      f"before={before}; after retry={after}; replanned={new_auth}")
