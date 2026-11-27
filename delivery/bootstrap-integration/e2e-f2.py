#!/usr/bin/env python3
"""F2 -- real end-to-end verification: two DIFFERENT code tasks, sequentially, preserving work.

Not a simulated result. This drives the RUNNING isolated stack (real Postgres, real worker, real
per-goal git worktree) through the real authorization chain:

  founder goal (API) -> a two-task plan in the real planner -> founder envelope (API) ->
  the worker's Supervisor tick -> the real Operator (create/patch, ruff, pytest, git stage and
  commit) -> goal completed.

The plan is created in-process with the SAME `planner.create_plan()` the `/plan` route calls:
the deterministic source of truth under test here is the Supervisor's execution of two distinct
`repo_edit` tasks inside ONE goal, not the model's ability to decompose prose. Everything after
plan creation goes through the shipped API and the shipped worker.

What it proves (the measured F2 failure, 2026-11-27):
  1. the implementation task writes ONLY `calculator.py` and commits it;
  2. the focused-test task writes ONLY `test_calculator.py`, preserves the earlier test file's
     content, and does NOT rewrite the already-committed source (the goal-scoped recipe used to
     repeat the first task's two files);
  3. both commits exist in the goal worktree, in order, one file each;
  4. the implementation commit's `calculator.py` is byte-identical to HEAD's (previous verified
     work preserved, not reverted by the worktree reset between tasks);
  5. the resulting focused test really passes when run in that worktree.
"""

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"
STACK = Path(os.environ.get("LIFEAI_STACK", Path.home() / "lifeai-local-stack"))
API = "http://127.0.0.1:8000"
DB = "postgresql://postgres@127.0.0.1:55432/lifeai_dev"
EMAIL = "founder@lifeai.local"
PASSWORD = os.environ.get("FOUNDER_PASSWORD", "dev-only-founder-pass")

# The stack's own environment (start-backend.sh / start-worker.sh), so importing the real app
# modules below talks to the same database the API and the worker use.
os.environ.setdefault("DATABASE_URL", DB)
os.environ.setdefault("APP_DATABASE_URL", "postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeai_dev")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:56381/0")
os.environ.setdefault("SECRET_KEY", "dev-only-local-secret")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("LOCAL_ONLY_MODE", "true")
os.environ.setdefault("STORAGE_ROOT", str(STACK / "storage"))
os.environ.setdefault("EMBEDDING_DIM", "1536")

INSTRUCTION = (
    "Lägg till en subtract-funktion i calculator.py och ett focused test som verifierar den. "
    "Endast calculator.py och test_calculator.py får ändras."
)
DESC_IMPLEMENT = "Lägg till en subtract-funktion i calculator.py"
DESC_TEST = "Lägg till ett focused test för subtract-funktionen i test_calculator.py"
PATHS = ["calculator.py", "test_calculator.py"]
CAPS = [
    "create_file",
    "patch_file",
    "read_file",
    "list_files",
    "run_static_check",
    "run_focused_test",
    "stage_scoped_changes",
    "commit_scoped_changes",
]
WORKTREES = Path("/tmp/mainai-supervisor-goal-worktrees")


def db():
    conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


def one(sql, params=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def session():
    s = requests.Session()
    r = s.post(f"{API}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    r.raise_for_status()
    s.headers.update({"X-CSRF-Token": r.json()["csrf_token"], "Content-Type": "application/json"})
    return s


def create_goal(s) -> str:
    r = s.post(
        f"{API}/api/mainai/execution/goals",
        json={
            "title": "F2 E2E: två olika koduppgifter",
            "original_instruction": INSTRUCTION,
            "risk_level": "low",
            "approval_policy": "autonomous_development_work",
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["id"]


def create_two_task_plan(goal_id: str) -> list[dict]:
    """The real production planner, called in-process because no route exposes an explicit plan.

    Same function the `/plan` route uses, same database, same owner context -- only the
    decomposition step is supplied explicitly, because the behaviour under test is the
    Supervisor's task-scoped execution, not model prose decomposition.
    """
    sys.path.insert(0, str(BACKEND))
    from sqlalchemy import text as sa_text

    from app.db import SessionLocal
    from app.mainai_execution import planner
    from app.models.mainai_execution import MainAIGoal, MainAITask
    from app.request_context import current_user_id

    founder = one("SELECT id FROM users WHERE email = %s", (EMAIL,))["id"]
    session_db = SessionLocal()
    try:
        current_user_id.set(str(founder))
        session_db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(founder)})
        goal = session_db.get(MainAIGoal, uuid.UUID(goal_id))
        specs = [
            planner.PlannedTaskSpec(description=DESC_IMPLEMENT, task_type="repo_edit"),
            planner.PlannedTaskSpec(description=DESC_TEST, task_type="repo_edit", depends_on=[0]),
        ]
        planner.create_plan(
            session_db,
            goal=goal,
            rationale="F2 E2E: implementation followed by its focused test",
            tasks=specs,
            created_by="f2-e2e",
        )
        session_db.commit()
        rows = (
            session_db.query(MainAITask)
            .filter(MainAITask.goal_id == goal.id)
            .order_by(MainAITask.created_at)
            .all()
        )
        return [{"id": str(row.id), "type": row.task_type, "description": row.description} for row in rows]
    finally:
        session_db.close()


def approve(s, task_id: str) -> None:
    r = s.post(f"{API}/api/mainai/execution/tasks/{task_id}/approve", timeout=60)
    if r.status_code >= 400:
        print(f"    approve {task_id} -> {r.status_code} {r.text[:200]}")


def authorize_envelope(s, goal_id: str) -> str:
    r = s.post(
        f"{API}/api/execution-envelopes/goals/{goal_id}/propose",
        json={"proposed_paths": PATHS, "proposed_capabilities": CAPS, "proposed_risk": "low"},
        timeout=60,
    )
    r.raise_for_status()
    proposal = r.json()["id"]
    r = s.post(
        f"{API}/api/execution-envelopes/proposals/{proposal}/authorize",
        json={"authorized_paths": PATHS, "authorized_capabilities": CAPS, "authorized_risk": "low"},
        timeout=60,
    )
    r.raise_for_status()
    current = s.get(f"{API}/api/execution-envelopes/current?goal_id={goal_id}", timeout=60)
    current.raise_for_status()
    return current.json()["id"]


def wait_until(predicate, timeout, interval=3, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for {label}")


def goal_status(goal_id: str) -> str:
    return one("SELECT status FROM mainai_goals WHERE id = %s", (goal_id,))["status"]


def tasks(goal_id: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT id, status, task_type, description, attempts FROM mainai_tasks "
            "WHERE goal_id = %s ORDER BY created_at",
            (goal_id,),
        )
        return cur.fetchall()


def task_events(task_id: str):
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, detail FROM mainai_task_events WHERE task_id = %s ORDER BY created_at",
            (task_id,),
        )
        return cur.fetchall()


def git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(worktree), capture_output=True, text=True, check=True
    ).stdout


def main() -> int:
    s = session()

    print("== create goal (API) and a real two-task plan ==")
    goal = create_goal(s)
    print(f"  goal = {goal}")
    planned = create_two_task_plan(goal)
    assert len(planned) == 2, f"expected two tasks, got {planned}"
    assert {t["type"] for t in planned} == {"repo_edit"}, planned
    print(f"  tasks: {[(t['id'][:8], t['description']) for t in planned]}")

    for task in planned:
        approve(s, task["id"])
    envelope = authorize_envelope(s, goal)
    print(f"  envelope {envelope} (scoped stage+commit)")

    print("== the real worker executes both tasks through the Supervisor ==")
    done = wait_until(
        lambda: all(t["status"] in {"completed", "failed", "cancelled"} for t in tasks(goal)),
        timeout=900,
        interval=5,
        label="both tasks terminal",
    )
    final = tasks(goal)
    print(f"  task statuses: {[(t['description'][:40], t['status'], t['attempts']) for t in final]}")
    for task in final:
        assert task["status"] == "completed", f"task {task['id']} ended {task['status']}"
        events = [e["event_type"] for e in task_events(str(task["id"]))]
        assert "verification_passed" in events, (task["id"], events)
        print(f"  {task['id'][:8]} events: {sorted(set(events))}")
    assert done is not False
    wait_until(lambda: goal_status(goal) == "completed", timeout=120, label="goal completed")
    print(f"  goal status = {goal_status(goal)}")

    print("== worktree evidence: two distinct, cumulative commits ==")
    worktree = WORKTREES / goal
    assert (worktree / ".git").exists(), f"goal worktree missing: {worktree}"
    subjects = git(worktree, "log", "--format=%s", "-n", "6").splitlines()
    print(f"  recent commits: {subjects}")
    impl_sha = git(worktree, "log", "--format=%H", "--grep=^Add subtract helper$", "-n", "1").strip()
    test_sha = git(worktree, "log", "--format=%H", "--grep=^Add focused subtract test$", "-n", "1").strip()
    assert impl_sha and test_sha, f"expected one commit per task, got {subjects}"
    assert impl_sha != test_sha

    impl_files = [f for f in git(worktree, "show", "--name-only", "--format=", impl_sha).splitlines() if f]
    test_files = [f for f in git(worktree, "show", "--name-only", "--format=", test_sha).splitlines() if f]
    print(f"  implementation commit files: {impl_files}")
    print(f"  focused-test commit files:   {test_files}")
    assert impl_files == ["calculator.py"], impl_files
    assert test_files == ["test_calculator.py"], test_files
    assert git(worktree, "merge-base", "--is-ancestor", impl_sha, test_sha) == ""

    print("== previous verified work preserved ==")
    assert git(worktree, "diff", impl_sha, "--", "calculator.py").strip() == "", (
        "the second task must not rewrite the first task's committed source"
    )
    source = (worktree / "calculator.py").read_text(encoding="utf-8")
    tests_text = (worktree / "test_calculator.py").read_text(encoding="utf-8")
    assert source.count("def subtract(") == 1, "the helper must exist exactly once (no duplication)"
    assert tests_text.count("def test_subtract(") == 1, "the test must exist exactly once"
    print(f"  calculator.py: def subtract count={source.count('def subtract(')}")
    print(f"  test_calculator.py: def test_subtract count={tests_text.count('def test_subtract(')}")

    print("== the resulting test really passes in that worktree ==")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_calculator.py"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    print("  " + (result.stdout.strip().splitlines() or [""])[-1])
    assert result.returncode == 0, result.stdout + result.stderr

    print("F2 E2E OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"F2 E2E FAILED: {type(exc).__name__}: {exc}")
        raise
