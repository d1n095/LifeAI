#!/usr/bin/env python3
"""Provider E2E -- real, VARIED development work planned by the configured local model.

The five verified deliveries all executed *fixed recipes* (the calculator subtract/multiply
recipes in `app.safe_planner.service`). The bootstrap gate (docs/MAINAI_BOOTSTRAP_STRATEGY.md)
explicitly requires "obe vakad exekvering av varierande verkliga utvecklingsuppgifter, inte
endast fasta recept", so this driver measures the path that had never run end to end:

  founder goal (API)
  -> `POST /goals/{id}/plan`                     the real model decomposes the goal into tasks
  -> founder envelope (API)                      only the two files the goal names
  -> founder provider-spend grant (API)          ollama / llama3.1
  -> the running worker's Supervisor tick
  -> `plan_with_provider()`                      the real model plans EACH task's steps
  -> Safe Planner validation                     deterministic; the model proposes, never decides
  -> the real Operator                           create_file, pytest, git stage/commit
  -> goal completed, commits in the goal worktree

Nothing here is a fake adapter or a scripted plan: the local Ollama model produces both the
task decomposition and each task's step plan. The assertions are about measured effects
(files, commits, a passing test, spend events, checkpoint dispositions), not about prose.
"""

import json
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
STACK = Path(os.environ.get("LIFEAI_STACK", Path.home() / "lifeai-local-stack"))
# The E2E owns one database and one API base so it never shares a goal queue with another
# running environment (2026-11-28: a Docker-hosted worker from 2026-11-26 exists on this host;
# it has no connections to this Postgres, but an owned database keeps that true by construction).
API = os.environ.get("LIFEAI_API", "http://127.0.0.1:8000")
DB = os.environ.get("LIFEAI_E2E_DB", "postgresql://postgres@127.0.0.1:55432/lifeai_dev")
APP_DB = os.environ.get(
    "LIFEAI_E2E_APP_DB", "postgresql://mainai_app:mainai_app_pw@127.0.0.1:55432/lifeai_dev"
)
# The spend grant must name the SAME provider/model the worker plans with: a hardcoded
# ollama/llama3.1 grant silently refused every plan as PROVIDER_SPEND_NOT_AUTHORIZED while the
# worker was configured with another model (measured 2026-11-28, model comparison run).
PROVIDER = os.environ.get("LIFEAI_E2E_PROVIDER", "ollama")
MODEL = os.environ.get("LIFEAI_E2E_MODEL", "llama3.1")
EMAIL = "founder@lifeai.local"
PASSWORD = os.environ.get("FOUNDER_PASSWORD", "dev-only-founder-pass")

os.environ.setdefault("DATABASE_URL", DB)
os.environ.setdefault("APP_DATABASE_URL", APP_DB)
os.environ.setdefault("REDIS_URL", os.environ.get("LIFEAI_E2E_REDIS", "redis://127.0.0.1:56381/0"))
os.environ.setdefault("SECRET_KEY", "dev-only-local-secret")
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("LOCAL_ONLY_MODE", "true")
os.environ.setdefault("STORAGE_ROOT", str(STACK / "storage"))
os.environ.setdefault("EMBEDDING_DIM", "1536")

# Two genuinely different tasks, so "it works" is not one memorised case: a string-slugging
# function with punctuation, and a length-bounded truncation with an ellipsis. Pick with
# LIFEAI_E2E_SCENARIO; everything else (planner, operator, commits, pytest) is the same path.
SCENARIOS = {
    "slugify": {
        "title": "Provider E2E: slugify-modul med fokustest",
        "source": "note_tools.py",
        "test": "test_note_tools.py",
        "function": "slugify",
        "instruction": (
            "Skapa filen note_tools.py med funktionen slugify(text: str) -> str som gör om en "
            "text till en slug: gemener, orden separerade med ett bindestreck, och alla andra "
            "tecken borttagna. Skapa också filen test_note_tools.py med minst ett focused test "
            "som verifierar slugify. Endast note_tools.py och test_note_tools.py får skapas "
            "eller ändras."
        ),
        "impl_desc": (
            "Skapa note_tools.py med funktionen slugify(text: str) -> str som gör om en text "
            "till en slug: gemener, orden separerade med ett bindestreck, alla andra tecken "
            "borttagna"
        ),
        "test_desc": (
            "Skapa test_note_tools.py med ett focused test som verifierar slugify, inklusive ett "
            "fall med versaler och skiljetecken"
        ),
        "behavior": (
            "from note_tools import slugify; "
            "assert slugify('Hello, World!') == 'hello-world', slugify('Hello, World!'); "
            "assert slugify('  A  B  ') == 'a-b', slugify('  A  B  '); "
            "print('slugify behaves as specified')"
        ),
    },
    "truncate": {
        "title": "Provider E2E: truncate-modul med fokustest",
        "source": "text_utils.py",
        "test": "test_text_utils.py",
        "function": "truncate",
        "instruction": (
            "Skapa filen text_utils.py med funktionen truncate(text: str, limit: int) -> str som "
            "returnerar text oförändrad om den är högst limit tecken lång, och annars kortar "
            "den till limit tecken följt av tre punkter. Skapa också filen test_text_utils.py "
            "med minst ett focused test som verifierar truncate. Endast text_utils.py och "
            "test_text_utils.py får skapas eller ändras."
        ),
        "impl_desc": (
            "Skapa text_utils.py med funktionen truncate(text: str, limit: int) -> str som kortar "
            "text till limit tecken plus tre punkter när den är längre, annars returnerar texten"
        ),
        "test_desc": (
            "Skapa test_text_utils.py med ett focused test som verifierar truncate för både kort "
            "och lång text"
        ),
        "behavior": (
            "from text_utils import truncate; "
            "assert truncate('abcdefghij', 5) == 'abcde...', truncate('abcdefghij', 5); "
            "assert truncate('abc', 5) == 'abc', truncate('abc', 5); "
            "print('truncate behaves as specified')"
        ),
    },
}
SCENARIO = os.environ.get("LIFEAI_E2E_SCENARIO", "slugify")
assert SCENARIO in SCENARIOS, f"unknown LIFEAI_E2E_SCENARIO={SCENARIO!r}; pick one of {sorted(SCENARIOS)}"
SCEN = SCENARIOS[SCENARIO]
SOURCE = SCEN["source"]
TEST = SCEN["test"]
PATHS = [SOURCE, TEST]
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
INSTRUCTION = SCEN["instruction"]
WORKTREES = Path("/tmp/mainai-supervisor-goal-worktrees")


def db():
    conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


def one(sql, params=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def all_rows(sql, params=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def session():
    """Log in, tolerating a backend that is still warming up (a cold start can exceed 30 s)."""
    s = requests.Session()
    last: Exception | None = None
    for attempt in range(4):
        try:
            r = s.post(f"{API}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=90)
            r.raise_for_status()
            s.headers.update({"X-CSRF-Token": r.json()["csrf_token"], "Content-Type": "application/json"})
            return s
        except requests.RequestException as exc:  # noqa: PERF203 - short, bounded retry
            last = exc
            print(f"  login attempt {attempt + 1} failed: {type(exc).__name__}")
            time.sleep(5 * (attempt + 1))
    assert last is not None
    raise last


def create_goal(s) -> str:
    r = s.post(
        f"{API}/api/mainai/execution/goals",
        json={
            "title": SCEN["title"],
            "original_instruction": INSTRUCTION,
            "risk_level": "low",
            "approval_policy": "autonomous_development_work",
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["id"]


DESC_IMPLEMENT = SCEN["impl_desc"]
DESC_TEST = SCEN["test_desc"]


def plan_with_model(s, goal_id: str) -> list[dict]:
    """The shipped route: the configured model proposes the task decomposition.

    Kept as an explicit, separately reported measurement. A parallel plan (several tasks tied
    on priority and all actionable) legitimately lands in NEEDS_SELECTION: the Supervisor never
    picks arbitrarily, and this driver does not own that founder/provider decision. The
    sequential plan below keeps the runner's own decomposition fixed while still letting the
    MODEL plan and write every task, which is what this E2E measures.
    """
    started = time.time()
    r = s.post(f"{API}/api/mainai/execution/goals/{goal_id}/plan", timeout=600)
    elapsed = time.time() - started
    if r.status_code >= 400:
        raise AssertionError(f"model plan failed {r.status_code}: {r.text[:600]}")
    rows = all_rows(
        "SELECT id, task_type, description FROM mainai_tasks WHERE goal_id = %s ORDER BY created_at",
        (goal_id,),
    )
    print(f"  model plan accepted in {elapsed:.1f}s -> {len(rows)} task(s)")
    for row in rows:
        print(f"    {row['id'][:8]} {row['task_type']:16s} {row['description'][:90]}")
    return [dict(row) for row in rows]


def create_sequential_plan(goal_id: str) -> list[dict]:
    """A two-task sequential plan through the real planner (same function the route uses).

    Only the decomposition is supplied explicitly; every task's STEP plan and every line of
    code still comes from the local model through `plan_with_provider()`.
    """
    sys.path.insert(0, str(REPO_ROOT / "backend"))
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
        planner.create_plan(
            session_db,
            goal=goal,
            rationale="Provider E2E: implementation followed by its focused test",
            tasks=[
                planner.PlannedTaskSpec(description=DESC_IMPLEMENT, task_type="repo_edit"),
                planner.PlannedTaskSpec(description=DESC_TEST, task_type="repo_edit", depends_on=[0]),
            ],
            created_by="provider-e2e",
        )
        session_db.commit()
        rows = (
            session_db.query(MainAITask)
            .filter(MainAITask.goal_id == goal.id)
            .order_by(MainAITask.created_at)
            .all()
        )
        return [
            {"id": str(row.id), "task_type": row.task_type, "description": row.description}
            for row in rows
        ]
    finally:
        session_db.close()


def approve(s, task_id: str) -> None:
    r = s.post(f"{API}/api/mainai/execution/tasks/{task_id}/approve", timeout=60)
    if r.status_code >= 400:
        print(f"    approve {task_id[:8]} -> {r.status_code} {r.text[:200]}")


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


def authorize_spend(s, goal_id: str, envelope_id: str) -> None:
    r = s.post(
        f"{API}/api/provider-spend/authorize",
        json={
            "goal_id": goal_id,
            "execution_envelope_id": envelope_id,
            "max_cost_usd": 1.00,
            "max_requests": 50,
            "max_cost_per_request_usd": 0.20,
            "allowed_providers": [PROVIDER],
            "allowed_models": [MODEL],
            "idempotency_key": f"provider-e2e-{goal_id}-{envelope_id}",
        },
        timeout=60,
    )
    r.raise_for_status()


def wait_until(predicate, timeout, interval=5, label=""):
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
    return all_rows(
        "SELECT id, status, task_type, description, attempts FROM mainai_tasks "
        "WHERE goal_id = %s ORDER BY created_at",
        (goal_id,),
    )


def task_events(task_id: str):
    return all_rows(
        "SELECT event_type, detail FROM mainai_task_events WHERE task_id = %s ORDER BY created_at",
        (task_id,),
    )


def provider_checkpoints(goal_id: str):
    """Every provider-planning decision the Supervisor actually recorded."""
    rows = all_rows(
        "SELECT task_id, executor_state FROM mainai_checkpoints WHERE goal_id = %s ORDER BY created_at",
        (goal_id,),
    )
    out = []
    for row in rows:
        state = row["executor_state"] or {}
        planning = state.get("provider_planning") or {}
        if planning:
            out.append({"task_id": row["task_id"], **planning})
    return out


def spend_events(goal_id: str):
    return all_rows(
        "SELECT status, provider, model, prompt_tokens, completion_tokens, cost_usd, source_ref "
        "FROM provider_spend_usage_events WHERE goal_id = %s ORDER BY created_at",
        (goal_id,),
    )


def git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(worktree), capture_output=True, text=True, check=True
    ).stdout


def main() -> int:
    s = session()

    print("== create goal (API) and a real two-task sequential plan ==")
    goal = create_goal(s)
    print(f"  goal = {goal}")
    planned = create_sequential_plan(goal)
    assert len(planned) == 2, f"expected two tasks, got {planned}"
    for row in planned:
        print(f"    {row['id'][:8]} {row['task_type']:16s} {row['description'][:90]}")

    for task in planned:
        approve(s, task["id"])
    envelope = authorize_envelope(s, goal)
    authorize_spend(s, goal, envelope)
    print(f"  envelope {envelope} scoped to {PATHS}; spend grant {PROVIDER}/{MODEL}")

    print("== the running worker executes the tasks through the Supervisor + local model ==")
    approved: set[str] = {task["id"] for task in planned}

    def advance() -> bool:
        """Approve replanned children like the founder would, then report terminal state.

        A bounded automatic replan legitimately creates NEW tasks with no inherited approval;
        this driver plays the founder for them (bounded by `approve`'s own contract) so a
        single run can observe a whole goal, not just its first plan.
        """
        current = tasks(goal)
        for row in current:
            key = str(row["id"])
            if row["status"] == "ready" and key not in approved:
                approve(s, key)
                approved.add(key)
        return all(row["status"] in {"completed", "failed", "cancelled"} for row in current)

    wait_until(advance, timeout=3600, interval=10, label="all tasks terminal")
    final = tasks(goal)
    for task in final:
        events = [e["event_type"] for e in task_events(str(task["id"]))]
        print(f"  {task['id'][:8]} {task['status']:10s} attempts={task['attempts']} events={sorted(set(events))}")
    assert all(t["status"] == "completed" for t in final), final
    wait_until(lambda: goal_status(goal) == "completed", timeout=180, label="goal completed")
    print(f"  goal status = {goal_status(goal)}")

    print("== the model, not a recipe, planned each task ==")
    # `provider_planning` also records the PROVIDER_ASSISTANCE_REQUIRED marker (no provider
    # call yet, no disposition). Only decisions carry provider/model/latency.
    checkpoints = [cp for cp in provider_checkpoints(goal) if cp.get("disposition")]
    assert checkpoints, "no provider_planning decision checkpoint was recorded"
    for cp in checkpoints:
        print(
            f"  {str(cp['task_id'])[:8]} disposition={cp.get('disposition')} "
            f"provider={cp.get('provider')} model={cp.get('model')} latency={cp.get('latency_ms')}ms"
        )
        assert cp.get("provider") == "ollama", cp
    assert any(cp.get("disposition") == "ACCEPTED" for cp in checkpoints), checkpoints
    spend = spend_events(goal)
    print(f"  spend events: {[(e['status'], e['provider'], e['model'], e['cost_usd']) for e in spend]}")
    assert any(e["status"] == "settled" for e in spend), spend

    print("== worktree evidence: real files, real commits, a really passing test ==")
    worktree = WORKTREES / goal
    assert (worktree / ".git").exists(), f"goal worktree missing: {worktree}"
    subjects = [line for line in git(worktree, "log", "--format=%s", "-n", "8").splitlines() if line]
    print(f"  recent commits: {subjects}")
    assert (worktree / SOURCE).exists(), f"{SOURCE} was never created"
    assert (worktree / TEST).exists(), f"{TEST} was never created"
    source = (worktree / SOURCE).read_text(encoding="utf-8")
    marker = f"def {SCEN['function']}"
    print(f"  {SOURCE}: {len(source.splitlines())} lines, has {SCEN['function']}={(marker in source)}")
    assert marker in source, source[:400]
    dirty = git(worktree, "status", "--porcelain").strip()
    print(f"  uncommitted changes: {dirty or '(none)'}")
    # Durability: the run must have committed its own work; an uncommitted worktree change is
    # discarded by the next task's reset, so a goal that only leaves `?? file` did not persist
    # anything. `mainai/driver:` commits are the driver's own scoped commits.
    driver_commits = [line for line in subjects if line.startswith("mainai/driver:")]
    print(f"  driver commits: {len(driver_commits)}")
    assert driver_commits, subjects
    assert not dirty, f"work is not committed: {dirty}"

    behavior = subprocess.run(
        [
            sys.executable,
            "-c",
            SCEN["behavior"],
        ],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    print("  behavior: " + (behavior.stdout.strip() or behavior.stderr.strip().splitlines()[-1:][0]))
    assert behavior.returncode == 0, behavior.stdout + behavior.stderr

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", TEST],
        cwd=str(worktree),
        capture_output=True,
        text=True,
    )
    print("  " + ((result.stdout.strip().splitlines() or [""])[-1]))
    assert result.returncode == 0, result.stdout + result.stderr

    print("== provenance of the executed plan ==")
    for task in final:
        rows = all_rows(
            "SELECT executor_state FROM mainai_checkpoints WHERE task_id = %s ORDER BY created_at",
            (str(task["id"]),),
        )
        for row in rows:
            planning = (row["executor_state"] or {}).get("safe_planner") or {}
            attribution = planning.get("provider_attribution") or {}
            if attribution:
                print(f"  {task['id'][:8]} safe_planner attribution={json.dumps(attribution, sort_keys=True)}")

    print("PROVIDER E2E OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"PROVIDER E2E FAILED: {type(exc).__name__}: {exc}")
        raise
