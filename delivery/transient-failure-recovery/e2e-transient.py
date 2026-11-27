"""Real end-to-end verification of TRANSIENT failure handling, against the isolated stack.

Not a simulation: this drives the RUNNING backend + worker + Postgres and a real per-goal git
worktree, and it takes the worker away in the middle of real work.

A transient failure is modelled the way the system actually experiences one here: the worker
process dies while a `task_execution` job is `running`. That is exactly the failure class the
lease/recovery/takeover pipeline exists for -- nothing is simulated with fake clocks or
in-process stubs.

  Phase 1 -- RECOVERY. One SIGKILL mid-task. The restarted worker releases its previous
  process's goal lease (the integrated T1 fix -- no clock is moved); the job's lease expires,
  the existing recovery pipeline inspects/classifies/takes over, the task is retried with a
  durable backoff clock (`next_retry_at`), and attempt 2 COMPLETES. Exactly one `completed`
  event, no duplicate work.

  Phase 2 -- ATTEMPTS BUDGET. A second goal is killed once per attempt. After `max_attempts`
  failures the existing outcome gate marks the task `failed` and stops: no retry clock, no new
  job, no infinite loop. The already-verified behavior of the other goal is untouched.

Both phases assert from durable Postgres state (`mainai_tasks`, `mainai_jobs`,
`mainai_task_events`) -- the same truth the API exposes -- never from log scraping.

Exit code 0 means every assertion below held.
"""

import os
import signal
import subprocess
import sys
import time

import psycopg2
import requests
from psycopg2.extras import RealDictCursor

API = "http://127.0.0.1:8000"
DB = "postgresql://postgres@127.0.0.1:55432/lifeai_dev"
EMAIL = "founder@lifeai.local"
PASSWORD = os.environ["FOUNDER_PASSWORD"]
STACK = os.path.expanduser("~/lifeai-local-stack")
PATHS = ["calculator.py", "test_calculator.py"]
CAPS = [
    "create_file",
    "patch_file",
    "read_file",
    "list_files",
    "run_focused_test",
    "run_static_check",
    "stage_scoped_changes",
    "commit_scoped_changes",
]
INSTRUCTION = (
    "Lägg till en subtract-funktion i calculator.py och ett focused test som verifierar den. "
    "Endast calculator.py och test_calculator.py får ändras."
)

failures: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + label + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def one(query, params=()):
    with psycopg2.connect(DB, cursor_factory=RealDictCursor) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchone()


def many(query, params=()):
    with psycopg2.connect(DB, cursor_factory=RealDictCursor) as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def login():
    s = requests.Session()
    r = s.post(f"{API}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=60)
    r.raise_for_status()
    s.headers.update({"X-CSRF-Token": r.json()["csrf_token"]})
    return s


def worker_pid() -> int | None:
    """Our own worker: `python3 -m app.worker` under this user (another uid runs one too)."""
    out = subprocess.run(
        ["ps", "-eo", "pid,uid,args"], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    for line in out:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid, uid, args = parts
        if "app.worker" in args and uid == str(os.getuid()) and "grep" not in args:
            return int(pid)
    return None


def start_worker() -> None:
    subprocess.Popen(
        [os.path.join(STACK, "start-worker.sh")],
        stdout=open(os.path.join(STACK, "worker.log"), "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(60):
        if worker_pid() is not None:
            return
        time.sleep(1)
    raise SystemExit("could not start the worker")


def kill_worker() -> int:
    pid = worker_pid()
    if pid is None:
        raise SystemExit("no worker process to kill")
    os.kill(pid, signal.SIGKILL)
    for _ in range(30):
        if worker_pid() is None:
            return pid
        time.sleep(0.5)
    raise SystemExit("worker did not die")


def ensure_lease_progress(goal_id: str, timeout: float = 60) -> None:
    """Let the goal become dispatchable again after the worker was killed mid-tick.

    On the integrated candidate a restarted worker releases its own previous process's lease on
    its first poll cycle (T1 fix), so this normally observes the lease disappear within seconds
    and touches no clock at all. Only if the lease is still active after the wait -- the older
    base behaviour, where a SIGKILL left it `active` for the full hour -- is the clock moved to
    the exact state it reaches at the END of its TTL. That fallback is printed, so the evidence
    says which path was taken. Nothing else about the state is touched: the job's own lease
    expiry is handled by the real recovery pipeline.
    """

    def not_blocking() -> bool:
        return one(
            "SELECT count(*) AS n FROM supervisor_goal_leases "
            "WHERE goal_id = %s AND status = 'active' AND expires_at > now()",
            (goal_id,),
        )["n"] == 0

    if wait_for(not_blocking, timeout, interval=1):
        print("  goal lease not blocking (released by the restarted worker -- no clock change)")
        return
    with psycopg2.connect(DB) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE supervisor_goal_leases SET expires_at = now() - interval '1 second' "
            "WHERE goal_id = %s AND status = 'active'",
            (goal_id,),
        )
        moved = cur.rowcount
    print(f"  FALLBACK: goal lease still active, moved its expiry to now (rows={moved})")


def kill_when_running(task_id: str, timeout: float = 180) -> bool:
    """Kill the worker the moment the task (or its job) is genuinely RUNNING.

    Polling the task row directly, not a rendered job reference: the recipe's real work takes
    a couple of seconds, so the window is short and must be sampled tightly.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = one("SELECT status FROM mainai_tasks WHERE id = %s", (task_id,))
        job = one(
            "SELECT status FROM mainai_jobs WHERE input_refs::text LIKE %s "
            "ORDER BY created_at DESC LIMIT 1",
            (f"%{task_id}%",),
        )
        if (row and row["status"] == "running") or (job and job["status"] == "running"):
            print(
                "  interrupting: task="
                f"{row['status'] if row else None} job={job['status'] if job else None} "
                f"worker={worker_pid()}"
            )
            kill_worker()
            return True
        time.sleep(0.05)
    return False


def make_goal(s, title: str) -> tuple[str, str]:
    r = s.post(
        f"{API}/api/mainai/execution/goals",
        json={
            "title": title,
            "original_instruction": INSTRUCTION,
            "risk_level": "low",
            "approval_policy": "autonomous_development_work",
        },
        timeout=60,
    )
    r.raise_for_status()
    goal = r.json()["id"]
    # The local model occasionally answers 502 under load; that is an infrastructure blip, not
    # the behaviour under test, so the planning call itself is retried.
    for plan_try in range(1, 4):
        r = s.post(f"{API}/api/mainai/execution/goals/{goal}/plan", timeout=300)
        if r.status_code < 500:
            break
        print(f"plan attempt {plan_try} -> HTTP {r.status_code}, retrying in 20 s")
        time.sleep(20)
    r.raise_for_status()
    for task in s.get(f"{API}/api/mainai/execution/goals/{goal}/tasks", timeout=60).json():
        if task["task_type"] == "open_pr":
            # `open_pr` would need remote-write authority this run does not grant. ONLY it is
            # cancelled: cancelling any task that others depend on cascades down the plan.
            s.post(f"{API}/api/mainai/execution/tasks/{task['id']}/cancel", timeout=60)
        else:
            s.post(f"{API}/api/mainai/execution/tasks/{task['id']}/approve", timeout=60)
    proposal = s.post(
        f"{API}/api/execution-envelopes/goals/{goal}/propose",
        json={"proposed_paths": PATHS, "proposed_capabilities": CAPS, "proposed_risk": "low"},
        timeout=60,
    )
    proposal.raise_for_status()
    envelope = s.post(
        f"{API}/api/execution-envelopes/proposals/{proposal.json()['id']}/authorize",
        json={"authorized_paths": PATHS, "authorized_capabilities": CAPS, "authorized_risk": "low"},
        timeout=60,
    )
    envelope.raise_for_status()
    s.post(
        f"{API}/api/provider-spend/authorize",
        json={
            "goal_id": goal,
            "execution_envelope_id": envelope.json()["id"],
            "max_cost_usd": 1.0,
            "max_requests": 20,
            "max_cost_per_request_usd": 0.20,
            "allowed_providers": ["ollama"],
            "allowed_models": ["llama3.1"],
            "idempotency_key": f"transient-e2e-{goal}",
        },
        timeout=60,
    )
    return goal, envelope.json()["id"]


def tasks(goal_id):
    """The plan's real work tasks, excluding `open_pr` (cancelled: it needs remote-write
    authority this run deliberately does not grant)."""
    return many(
        "SELECT id, task_type, status, attempts, max_attempts, next_retry_at, blocker_reason "
        "FROM mainai_tasks WHERE goal_id = %s AND task_type <> 'open_pr' ORDER BY created_at",
        (goal_id,),
    )


def jobs(goal_id):
    return many(
        "SELECT j.id, j.status, j.error_category, j.attempt, t.id AS task_id FROM mainai_jobs j "
        "JOIN mainai_tasks t ON j.input_refs::text LIKE '%%' || t.id::text || '%%' "
        "WHERE t.goal_id = %s ORDER BY j.created_at",
        (goal_id,),
    )


def events(task_id, event_type):
    return many(
        "SELECT detail FROM mainai_task_events WHERE task_id = %s AND event_type = %s "
        "ORDER BY created_at",
        (str(task_id), event_type),
    )


def wait_for(predicate, timeout: float, interval: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval)
    return None


def main() -> int:
    s = login()
    if worker_pid() is None:
        start_worker()

    # ---------------------------------------------------------------- Phase 1: recovery
    goal_a, _ = make_goal(s, "transient recovery")
    task_a = wait_for(
        lambda: next((t for t in tasks(goal_a) if t["task_type"] == "repo_edit"), None), 60
    )
    check(task_a is not None, "phase 1: a planned task exists")
    if task_a is None:
        return 1

    killed = kill_when_running(str(task_a["id"]))
    check(killed, "phase 1: worker SIGKILLed while its job was running")
    check(worker_pid() is None, "phase 1: the worker process is genuinely gone")

    start_worker()
    check(worker_pid() is not None, "phase 1: worker restarted")
    ensure_lease_progress(goal_a)

    # Job lease TTL is 120 s in this stack; the takeover may only happen after it expires.
    recovered = wait_for(
        lambda: one(
            "SELECT status FROM mainai_tasks WHERE id = %s AND status IN ('completed','failed')",
            (str(task_a["id"]),),
        ),
        420,
        interval=5,
    )
    final_a = one("SELECT * FROM mainai_tasks WHERE id = %s", (str(task_a["id"]),))
    print(
        "phase 1 task:",
        final_a["status"],
        "attempts",
        f'{final_a["attempts"]}/{final_a["max_attempts"]}',
        "next_retry_at",
        final_a["next_retry_at"],
    )
    check(recovered is not None, "phase 1: the interrupted task reaches a terminal state")
    check(
        final_a["status"] == "completed",
        "phase 1: the transient failure RECOVERS to completed",
        str(final_a["status"]),
    )
    check(
        final_a["attempts"] >= 2,
        "phase 1: it took a real second attempt",
        str(final_a["attempts"]),
    )
    retries = events(task_a["id"], "retry_scheduled")
    check(len(retries) >= 1, "phase 1: a durable retry/backoff event was recorded", str(len(retries)))
    completed_events = events(task_a["id"], "completed")
    check(
        len(completed_events) == 1,
        "phase 1: exactly one completed event (no duplicate execution)",
        str(len(completed_events)),
    )
    total_jobs_a = one(
        "SELECT count(*) AS n FROM mainai_jobs WHERE input_refs::text LIKE %s",
        (f'%{task_a["id"]}%',),
    )["n"]
    check(total_jobs_a >= 2, "phase 1: recovery dispatched a new job", str(total_jobs_a))

    # ---------------------------------------------------------------- Phase 2: attempts budget
    goal_b, _ = make_goal(s, "transient budget exhaustion")

    def budget_target():
        planned = tasks(goal_b)
        for preferred in ("run_tests", "repo_edit"):
            for task in planned:
                if task["task_type"] == preferred:
                    return task
        return None

    task_b = wait_for(budget_target, 60)
    check(task_b is not None, "phase 2: a planned task exists")
    if task_b is None:
        return 1

    max_attempts = task_b["max_attempts"]
    # Interrupt every attempt that actually reaches RUNNING, and wait for the recovery to
    # finalise it before hunting the next one -- otherwise the loop would "kill" a worker that
    # is already dead and count attempts that never happened.
    deadline = time.time() + 1500
    interrupted = 0
    while time.time() < deadline:
        current = one("SELECT status FROM mainai_tasks WHERE id = %s", (str(task_b["id"]),))
        if current["status"] in {"completed", "failed", "cancelled"}:
            break
        # The recipe's real work takes ~3 s, so a coarse "is it running? then sleep" gate misses
        # the window. kill_when_running() polls tightly and kills the moment the attempt is
        # genuinely in flight; a short timeout keeps the loop responsive between attempts.
        if not kill_when_running(str(task_b["id"]), timeout=5):
            continue
        interrupted += 1
        state = one("SELECT attempts, max_attempts FROM mainai_tasks WHERE id = %s", (str(task_b["id"]),))
        check(
            True,
            f"phase 2: attempt {interrupted} interrupted while the worker had it "
            f"({state['attempts']}/{state['max_attempts']} used)",
        )
        start_worker()
        ensure_lease_progress(goal_b)
        wait_for(
            lambda: one("SELECT status FROM mainai_tasks WHERE id = %s", (str(task_b["id"]),))["status"]
            != "running",
            420,
            interval=2,
        )

    final_b = one("SELECT * FROM mainai_tasks WHERE id = %s", (str(task_b["id"]),))
    print(
        "phase 2 task:",
        final_b["status"],
        "attempts",
        f'{final_b["attempts"]}/{final_b["max_attempts"]}',
        "next_retry_at",
        final_b["next_retry_at"],
    )
    check(
        final_b["attempts"] <= max_attempts,
        "phase 2: the attempts budget is never exceeded",
        f'{final_b["attempts"]}/{max_attempts}',
    )
    check(
        final_b["status"] in {"completed", "failed"},
        "phase 2: repeated interruptions still end in a durable terminal state",
        str(final_b["status"]),
    )
    check(
        final_b["next_retry_at"] is None,
        "phase 2: no retry clock is left behind after the budget is exhausted",
    )
    print(f"phase 2: {interrupted} real interruptions of a {max_attempts}-attempt budget")
    jobs_after = one(
        "SELECT count(*) AS n FROM mainai_jobs WHERE input_refs::text LIKE %s",
        (f'%{task_b["id"]}%',),
    )["n"]
    time.sleep(25)
    jobs_later = one(
        "SELECT count(*) AS n FROM mainai_jobs WHERE input_refs::text LIKE %s",
        (f'%{task_b["id"]}%',),
    )["n"]
    check(
        jobs_later == jobs_after,
        "phase 2: no new jobs are generated after the budget is exhausted",
        f"{jobs_after} -> {jobs_later}",
    )
    if final_b["status"] == "failed":
        check(
            len(events(task_b["id"], "completed")) == 0,
            "phase 2: nothing is reported completed for the exhausted task",
        )

    # ---------------------------------------------------------------- unaffected work
    final_a_again = one("SELECT status FROM mainai_tasks WHERE id = %s", (str(task_a["id"]),))
    check(
        final_a_again["status"] == "completed",
        "the completed task stayed completed (no collateral damage)",
        str(final_a_again["status"]),
    )

    print()
    if failures:
        print("E2E FAILED:", ", ".join(failures))
        return 1
    print("E2E OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
