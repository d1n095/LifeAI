"""Independent Cursor cross-examination of Track B (#267 da4d96b) on the local
#271+#267 composition. Does not assume Claude's TB conclusions. Fake CLI only.
No live agent, GitHub write, merge, deploy, or Recall."""
from __future__ import annotations

import inspect
import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import text as sa_text

from app.agent_coordination.adapter_config import adapter_max_budget_usd, isolated_agent_env
from app.agent_coordination.service import CoordinationError, create_work_assignment
from app.mainai_execution.worktree import _authed_remote_url
from app.models.agent_coordination import AgentWorkAssignment

from test_first_cycle import M, _drive, _git  # noqa: F401
from test_path_a_269 import PATH_A_CAPS
from test_track_b_authority import (  # noqa: F401
    FAKE_TOKEN,
    _agent_task,
    _checkout_with_credentialed_origin,
    _fake_cli,
    _log,
    owner_id,
)


@pytest.mark.asyncio
async def test_indep_tb1_wrong_capability_and_path_a_only(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    log, _ = _fake_cli(monkeypatch, tmp_path, {})
    results = {}
    for label, caps in (
        ("path_a + open_pr only (no agent_dispatch)", ["path_a", "open_pr"]),
        ("path_a only", ["path_a", "read_file", "repo_edit", "run_tests", "open_pr"]),
    ):
        cwd = tmp_path / f"cwd-{label.replace(' ', '-')[:24]}"
        cwd.mkdir()
        goal, task, asg, _ = _agent_task(
            db_session, owner_id, caps=caps, role="reviewer", mode="read_only", worktree=cwd, allowed_paths=[]
        )
        before = len(_log(log))
        tasks = await _drive(superuser_db, goal.id, n=6)
        results[label] = {
            "task": tasks["agent_assignment"].status.value,
            "cli_launched": len(_log(log)) > before,
            "cwd": _log(log)[-1]["cwd"] if len(_log(log)) > before else None,
        }
        from app.agent_coordination.session import close_agent_session

        superuser_db.expire_all()
        done = superuser_db.get(AgentWorkAssignment, asg.id)
        if done is not None and done.status.value == "waiting_review":
            close_agent_session(superuser_db, assignment=done, accepted=False, reason="probe cleanup")
            superuser_db.commit()
    launched = any(r["cli_launched"] for r in results.values())
    M(
        "INDEP-TB-1 wrong-capability / Path-A-only envelopes cannot launch agent_assignment",
        not launched,
        f"{results}",
    )


def test_indep_tb1_reviewer_cannot_be_created_read_write(db_session, owner_id, tmp_path):
    from app.agent_coordination.bootstrap import bootstrap_known_agents
    from app.mainai_execution import planner
    from app.mainai_execution.planner import PlannedTaskSpec
    from app.models.mainai_execution import MainAITask

    goal = planner.create_goal(db_session, owner_id=owner_id, title="esc", original_instruction="x", created_by="founder")
    planner.create_plan(
        db_session,
        goal=goal,
        rationale="p",
        created_by="founder",
        tasks=[PlannedTaskSpec(description="r", task_type="agent_assignment", verification_plan=[])],
    )
    db_session.commit()
    task = db_session.query(MainAITask).filter(MainAITask.goal_id == goal.id).one()
    agent = next(a for a in bootstrap_known_agents(db_session) if a.agent_key == "claude-code")
    raised = None
    try:
        create_work_assignment(
            db_session,
            owner_id=owner_id,
            goal_id=goal.id,
            task_id=task.id,
            agent_id=agent.id,
            role="reviewer",
            read_write_mode="read_write",
            repository_identity="test-owner/test-repo",
            allowed_paths=[],
            requested_by="founder",
        )
    except CoordinationError as exc:
        raised = str(exc)
    M(
        "INDEP-TB-3e reviewer role cannot be created in read_write / builder mode",
        raised is not None and "read_only" in (raised or ""),
        f"raised={raised!r}",
    )


@pytest.mark.asyncio
async def test_indep_tb2_worktree_symlink_escape(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    real = tmp_path / "real-outside"
    real.mkdir()
    (real / "secret.txt").write_text("host-secret\n")
    link = tmp_path / "linked-worktree"
    link.symlink_to(real)
    actions = {"write": {"pwned.txt": "escaped via worktree symlink\n"}}
    log, _ = _fake_cli(monkeypatch, tmp_path, actions)
    goal, task, asg, _ = _agent_task(
        db_session, owner_id, caps=None, role="builder", mode="read_write",
        worktree=link, allowed_paths=["backend/app/x.py"], branch="claude/symlink-escape",
    )
    await _drive(superuser_db, goal.id, n=6)
    escaped = (real / "pwned.txt").exists()
    M(
        "INDEP-TB-2e worktree_path symlink is resolved/owned before cwd (escape blocked)",
        not escaped,
        f"symlink cwd launched; host file written={escaped}; cli={bool(_log(log))}",
    )


@pytest.mark.asyncio
async def test_indep_tb3_reviewer_push_uses_persisted_credential(db_session, superuser_db, owner_id, monkeypatch, tmp_path):
    """A reviewer process that can run git can push if origin embeds the token.
    Local bare remote only — no GitHub."""
    shaped = tmp_path / f"x-access-token:{FAKE_TOKEN}@github.com" / "test-owner" / "test-repo.git"
    shaped.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "-q", str(shaped)], check=True)
    work = tmp_path / "review-push"
    work.mkdir()
    _git("init", "-q", cwd=work)
    (work / "backend" / "app").mkdir(parents=True)
    (work / "backend" / "app" / "x.py").write_text("X = 1\n")
    _git("add", "-A", cwd=work)
    _git("-c", "user.name=s", "-c", "user.email=s@s", "commit", "-qm", "seed", cwd=work)
    _git("remote", "add", "origin", str(shaped), cwd=work)
    _git("push", "-q", "origin", "HEAD:refs/heads/seed", cwd=work)

    script_extra = (
        "r = subprocess.run(['git','push','-q','origin','HEAD:refs/heads/reviewer-push'], capture_output=True, text=True)\n"
        "out['push_rc'] = r.returncode\n"
        "out['push_err'] = (r.stderr or '')[:200]\n"
        "out['origin'] = subprocess.run(['git','config','--get','remote.origin.url'], capture_output=True, text=True).stdout.strip()\n"
    )
    log, _ = _fake_cli(monkeypatch, tmp_path, {"write": {"backend/app/x.py": "X = 2\n"}, "commit": True})
    script = tmp_path / "fake_claude.py"
    text = script.read_text()
    text = text.replace("if acts.get('commit'):", "if acts.get('commit'):")
    if "push_rc" not in text:
        text = text.replace(
            f"open({str(log)!r}, 'a').write(json.dumps(out) + '\\n')\n",
            script_extra + f"open({str(log)!r}, 'a').write(json.dumps(out) + '\\n')\n",
        )
        script.write_text(text)

    goal, task, asg, _ = _agent_task(
        db_session, owner_id, caps=None, role="reviewer", mode="read_only", worktree=work, allowed_paths=[]
    )
    await _drive(superuser_db, goal.id, n=6)
    e = (_log(log) or [{}])[0]
    refs = subprocess.run(
        ["git", "-C", str(shaped), "for-each-ref", "--format=%(refname:short)"],
        capture_output=True, text=True, check=True,
    ).stdout
    pushed = "reviewer-push" in refs
    origin_has_token = FAKE_TOKEN in (e.get("origin") or "") or FAKE_TOKEN in str(shaped)
    M(
        "INDEP-TB-3d reviewer process cannot push using a persisted origin credential",
        not pushed,
        f"push_rc={e.get('push_rc')}; pushed_ref={pushed}; origin_path_carries_token_shape={origin_has_token}; argv_plan={'plan' in (e.get('argv') or [])}",
    )


def test_indep_tb3c_authed_url_persisted_on_267_and_273():
    url = _authed_remote_url("owner/repo", FAKE_TOKEN)
    path_a_273 = Path("/home/ubuntu/cursor-builder-worktrees/mainai-execution-path-a-spend-8-remain/backend/app/mainai_execution/worktree.py")
    src_273 = path_a_273.read_text() if path_a_273.exists() else ""
    src_comp = inspect.getsource(_authed_remote_url)
    same = "x-access-token:{token}@github.com" in src_273 and "x-access-token:{token}@github.com" in src_comp
    holds = FAKE_TOKEN not in url  # inverted: we WANT this to fail closed; currently token IS in url
    M(
        "INDEP-TB-3c _authed_remote_url does not embed the token in a persisted origin URL (#267 and #273)",
        FAKE_TOKEN not in url,
        f"url_shape={url.replace(FAKE_TOKEN, '<redacted>')}; same_on_frozen_273={same}",
    )


def test_indep_tb4_conversation_collect_result_has_no_pr_ref():
    from app.agent_coordination.adapters import LocalCLIAdapter, AgentResult
    src = inspect.getsource(LocalCLIAdapter.collect_result)
    M(
        "INDEP-TB-4b conversation collect_result never sets pr_ref (TB-4 is generic API, not the CLI path)",
        "pr_ref" not in src,
        f"collect_result binds pr_ref={'pr_ref' in src}",
    )


def test_indep_tb6_spend_not_on_provider_spend_ledger():
    from pathlib import Path as P
    root = P("/tmp/tb-composition/backend/app/agent_coordination")
    hits = []
    for p in root.glob("*.py"):
        text = p.read_text()
        if "provider_spend" in text:
            hits.append(p.name)
    budget = adapter_max_budget_usd("claude-code")
    M(
        "INDEP-TB-6c Track B CLI budget is recorded on the governed provider_spend ledger",
        False if not hits else "reserve_provider_spend" in "".join((root / h).read_text() for h in hits),
        f"agent_coordination files mentioning provider_spend={hits}; adapter_max_budget_usd={budget}",
    )


def test_indep_tb8_no_exact_sha_receipt_call():
    from pathlib import Path as P
    root = P("/tmp/tb-composition/backend/app/agent_coordination")
    calls = []
    for p in root.glob("*.py"):
        text = p.read_text()
        if "record_verification_attestation" in text or "mainai_verification_registry" in text:
            calls.append(p.name)
    M(
        "INDEP-TB-8 review evidence is bound via mainai_verification_registry (repo, PR, exact SHA, distinct identities)",
        bool(calls),
        f"agent_coordination registry references={calls or 'none'}",
    )


def test_indep_tb6_git_subprocess_inherits_process_env():
    from app.mainai_execution import worktree as wt
    src = inspect.getsource(wt._run_git)
    M(
        "INDEP-TB-6d _run_git does not inherit the worker process environment (no leaked GITHUB_TOKEN/helpers)",
        "env=" in src,
        f"_run_git passes env= {'env=' in src}; uses default subprocess inheritance if absent",
    )
