"""Real end-to-end verification of F1+F7 against the isolated local stack.

Not a simulated result: this drives the RUNNING backend + worker + Postgres, a real local
Ollama model, and a real per-goal git worktree.

  A. Wrong goal configuration (standard_repo_work) -> Supervisor refuses permanently ->
     task PARKED blocked with no retry clock, exactly ONE job, goal lease released.
  B. The leftover job is claimed by the worker's V0.1 poll and FENCED (never executed):
     job failed/capability_unavailable, no work_result checkpoint, task still blocked.
  C. No churn: 20 s and several poll cycles later, still exactly one job.
  D. Another correctly configured goal COMPLETES while A stays parked.
  E. Founder fixes the configuration + re-authorizes the envelope -> exactly one more
     attempt -> A resumes through the Supervisor and completes.
"""

import json
import os
import sys
import time

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

API = "http://127.0.0.1:8000"
DB = "postgresql://postgres@127.0.0.1:55432/lifeai_dev"
EMAIL = "founder@lifeai.local"
PASSWORD = os.environ["FOUNDER_PASSWORD"]  # never hardcoded in the repo
PARK_BLOCKER = "autonomous execution is refused by this goal's configuration"
INSTRUCTION = (
    "Lägg till en subtract-funktion i calculator.py och ett focused test som verifierar den. "
    "Endast calculator.py och test_calculator.py får ändras."
)
PATHS = ["calculator.py", "test_calculator.py"]
CAPS = ["create_file", "patch_file", "read_file", "list_files", "run_focused_test"]


def db():
    conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


def one(sql, params=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def many(sql, params=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def session():
    s = requests.Session()
    r = s.post(f"{API}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=30)
    r.raise_for_status()
    s.headers.update({"X-CSRF-Token": r.json()["csrf_token"], "Content-Type": "application/json"})
    return s


def create_goal(s, policy, title):
    r = s.post(
        f"{API}/api/mainai/execution/goals",
        json={"title": title, "original_instruction": INSTRUCTION, "risk_level": "low", "approval_policy": policy},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["id"]


def plan(s, goal):
    r = s.post(f"{API}/api/mainai/execution/goals/{goal}/plan", timeout=300)
    r.raise_for_status()


def tasks(s, goal):
    r = s.get(f"{API}/api/mainai/execution/goals/{goal}", timeout=60)
    r.raise_for_status()
    return r.json()["tasks"]


def approve_repo_edits(s, goal):
    """Approve every task the autonomous policy marks approval-required (repo_edit, open_pr).

    Approval is a founder act, and this script stands in for the founder -- exactly like the
    nightly E2E. A task that does not require approval simply answers 200/4xx harmlessly.
    """
    for task in tasks(s, goal):
        if task["task_type"] in {"repo_edit", "open_pr"}:
            r = s.post(f"{API}/api/mainai/execution/tasks/{task['id']}/approve", timeout=60)
            if r.status_code >= 400:
                print(f"    approve {task['id']} -> {r.status_code}")


def authorize_envelope(s, goal, caps=None):
    caps = caps or CAPS
    r = s.post(
        f"{API}/api/execution-envelopes/goals/{goal}/propose",
        json={"proposed_paths": PATHS, "proposed_capabilities": caps, "proposed_risk": "low"},
        timeout=60,
    )
    r.raise_for_status()
    proposal = r.json()["id"]
    r = s.post(
        f"{API}/api/execution-envelopes/proposals/{proposal}/authorize",
        json={"authorized_paths": PATHS, "authorized_capabilities": caps, "authorized_risk": "low"},
        timeout=60,
    )
    r.raise_for_status()
    current = s.get(f"{API}/api/execution-envelopes/current?goal_id={goal}", timeout=60)
    current.raise_for_status()
    return current.json()["id"]


def authorize_spend(s, goal, envelope):
    r = s.post(
        f"{API}/api/provider-spend/authorize",
        json={
            "goal_id": goal,
            "execution_envelope_id": envelope,
            "max_cost_usd": 1.00,
            "max_requests": 50,
            "max_cost_per_request_usd": 0.20,
            "allowed_providers": ["ollama"],
            "allowed_models": ["llama3.1"],
            "idempotency_key": f"f1f7-e2e-{goal}-{envelope}",
        },
        timeout=60,
    )
    r.raise_for_status()


def task_row(goal):
    return one("SELECT * FROM mainai_tasks WHERE goal_id = %s ORDER BY created_at LIMIT 1", (goal,))


def jobs_for_task(task_id):
    return many(
        "SELECT id, status, error_category, lease_generation FROM mainai_jobs "
        "WHERE input_refs::text LIKE %s ORDER BY created_at",
        (f'%{task_id}%',),
    )


def active_leases(goal):
    return one(
        "SELECT count(*) AS n FROM supervisor_goal_leases WHERE goal_id = %s AND status = 'active'", (goal,)
    )["n"]


def work_checkpoints(task_id):
    return one(
        "SELECT count(*) AS n FROM mainai_checkpoints "
        "WHERE task_id = %s AND executor_state->>'step' = 'work_result'",
        (task_id,),
    )["n"]


def task_events(task_id):
    return many(
        "SELECT event_type, detail FROM mainai_task_events WHERE task_id = %s ORDER BY created_at", (task_id,)
    )


def wait_until(predicate, timeout, interval=3, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for {label}")


def goal_status(goal):
    return one("SELECT status FROM mainai_goals WHERE id = %s", (goal,))["status"]


def parked(goal):
    row = task_row(goal)
    if row["status"] == "blocked" and row["blocker_reason"] == PARK_BLOCKER:
        return row
    return None


def completed(goal):
    return goal_status(goal) == "completed"


def main() -> int:
    s = session()

    print("== A: goal with the wrong approval policy (the production F1 shape) ==")
    goal_a = create_goal(s, "standard_repo_work", "F1 E2E: parkerat mål (fel policy)")
    print(f"  goal A = {goal_a}")
    plan(s, goal_a)
    approve_repo_edits(s, goal_a)
    envelope_a1 = authorize_envelope(s, goal_a)
    authorize_spend(s, goal_a, envelope_a1)
    print(f"  envelope {envelope_a1}, spend authorized")

    row_a = wait_until(lambda: parked(goal_a), timeout=180, label="goal A task parked")
    print(f"  task {row_a['id']} parked: status={row_a['status']} blocker_reason={row_a['blocker_reason']!r}")
    print(f"  next_retry_at={row_a['next_retry_at']}")
    assert row_a["next_retry_at"] is None, "a park must have NO retry clock"

    jobs = jobs_for_task(row_a["id"])
    print(f"  jobs: {[(str(j['id'])[:8], j['status'], j['error_category']) for j in jobs]}")
    assert len(jobs) == 1, f"expected exactly one dispatched job, got {len(jobs)}"
    assert active_leases(goal_a) == 0, "the goal lease must be released immediately (F7)"
    assert work_checkpoints(row_a["id"]) == 0, "V0.1 must never have executed work"

    print("== B: the leftover job is claimed by V0.1 and fenced ==")
    fenced = wait_until(
        lambda: jobs_for_task(row_a["id"])[0]["status"] == "failed",
        timeout=60,
        label="leftover job fenced by V0.1",
    )
    job = jobs_for_task(row_a["id"])[0]
    print(f"  job {str(job['id'])[:8]} -> {job['status']} / {job['error_category']}")
    assert job["error_category"] == "capability_unavailable"
    row_a = task_row(goal_a)
    assert row_a["status"] == "blocked" and row_a["blocker_reason"] == PARK_BLOCKER, (
        "the V0.1 fence must never overwrite a parked task"
    )

    print("== C: no job churn ==")
    before = len(jobs_for_task(row_a["id"]))
    time.sleep(20)
    after = jobs_for_task(row_a["id"])
    print(f"  jobs before={before} after 20 s={len(after)}; task still {task_row(goal_a)['status']}")
    assert len(after) == before, "a parked goal must not generate a new job every poll cycle"
    assert task_row(goal_a)["status"] == "blocked"

    print("== D: another, correctly configured goal still completes ==")
    goal_b = create_goal(s, "autonomous_development_work", "F1 E2E: behörigt mål")
    print(f"  goal B = {goal_b}")
    plan(s, goal_b)
    approve_repo_edits(s, goal_b)
    envelope_b = authorize_envelope(s, goal_b)
    authorize_spend(s, goal_b, envelope_b)
    wait_until(lambda: completed(goal_b), timeout=600, label="goal B completed")
    print(f"  goal B status={goal_status(goal_b)}")
    assert task_row(goal_a)["status"] == "blocked", "goal A must stay parked while B runs"

    print("== E: founder fixes the configuration + re-authorizes -> one controlled retry ==")
    with db() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE mainai_goals SET approval_policy = 'autonomous_development_work' WHERE id = %s", (goal_a,)
        )
    # The propose route is idempotent on identical content, so a founder re-authorization is
    # necessarily a NEW explicit scope. Here the founder additionally grants the scoped
    # commit capabilities the same paths already needed -- a deliberate, visible widening.
    envelope_a2 = authorize_envelope(s, goal_a, caps=CAPS + ["stage_scoped_changes", "commit_scoped_changes"])
    authorize_spend(s, goal_a, envelope_a2)
    print(f"  new envelope {envelope_a2}; updated policy")

    wait_until(
        lambda: task_row(goal_a)["status"] in {"ready", "running", "completed"}, timeout=120, label="goal A woken"
    )
    row_a = task_row(goal_a)
    print(f"  task woken: status={row_a['status']}")
    wait_until(lambda: completed(goal_a), timeout=600, label="goal A completed after config fix")
    print(f"  goal A status={goal_status(goal_a)}")

    jobs = jobs_for_task(row_a["id"])
    print(f"  final jobs: {[(str(j['id'])[:8], j['status'], j['error_category']) for j in jobs]}")
    assert len(jobs) == 2, f"exactly one extra attempt for one founder act, got {len(jobs)}"
    events = task_events(row_a["id"])
    reasons = [e["detail"].get("reason") for e in events if isinstance(e["detail"], dict)]
    print(f"  task event reasons: {reasons}")
    assert "supervisor_config_refusal_park" in reasons
    assert "supervisor_refusal_envelope_reauth_wake" in reasons

    print("E2E OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"E2E FAILED: {type(exc).__name__}: {exc}")
        raise
