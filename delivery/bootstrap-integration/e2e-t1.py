#!/usr/bin/env python3
"""T1 -- real process-level proof: a restarted worker releases its previous process's goal lease.

Measured 2026-11-27: after a `kill -9` mid-tick, `supervisor_goal_leases` stayed `active` for the
full TTL (3 600 s), because F7 only releases when a tick RAISES -- a killed process raises
nothing -- so that goal could not be picked up again for up to an hour. The E2E that found it
had to move the lease clock to the TTL boundary to make progress.

This driver models the exact crash residue (an ACTIVE lease owned by this worker identity, with
an hour left) and then does the real thing: SIGKILLs the running worker and starts it again.
The restarted process must release that lease on its first poll cycle, write the release to the
append-only history, and be alive afterwards -- with no clock manipulation at all.
"""

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
import redis
from psycopg2.extras import RealDictCursor

STACK = Path(os.environ.get("LIFEAI_STACK", Path.home() / "lifeai-local-stack"))
DB = "postgresql://postgres@127.0.0.1:55432/lifeai_dev"
REDIS_URL = "redis://127.0.0.1:56381/0"


def db():
    conn = psycopg2.connect(DB, cursor_factory=RealDictCursor)
    conn.autocommit = True
    return conn


def one(sql, params=()):
    with db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def worker_pid():
    out = subprocess.run(["pgrep", "-f", r"^python3 -m app\.worker$"], capture_output=True, text=True)
    pids = [int(p) for p in out.stdout.split()]
    return pids[0] if pids else None


def worker_identity() -> str:
    """The worker's own id (`app/worker.py::_worker_id` falls back to the hostname); the
    heartbeat value Redis already records is the authoritative live value."""
    value = redis.Redis.from_url(REDIS_URL).get("worker:heartbeat")
    if value:
        return value.decode().split(":", 1)[0]
    return socket.gethostname()


def start_worker() -> None:
    subprocess.Popen(
        [str(STACK / "start-worker.sh")],
        stdout=open(STACK / "worker.log", "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(60):
        if worker_pid() is not None:
            return
        time.sleep(1)
    raise SystemExit("could not start the worker")


def wait_for(predicate, timeout: float, interval: float = 1.0, label: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for {label}")


def main() -> int:
    target = one(
        "SELECT g.id AS goal_id, g.owner_id, e.id AS envelope_id "
        "FROM mainai_goals g JOIN execution_authorization_envelopes e ON e.goal_id = g.id "
        "WHERE e.status = 'active' ORDER BY g.created_at DESC LIMIT 1"
    )
    assert target is not None, "no goal with an active envelope to attach the crash residue to"
    identity = worker_identity()
    print(f"  worker identity={identity!r}, goal={target['goal_id']}")

    # The exact durable residue of a SIGKILL mid-tick: active, owned by this identity, an hour
    # of TTL left. Nothing else about the goal or its tasks is touched.
    with db() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM supervisor_goal_leases WHERE goal_id = %s", (str(target["goal_id"]),))
    lease = one(
        "INSERT INTO supervisor_goal_leases "
        "(id, owner_id, goal_id, envelope_id, worker_id, lease_generation, status, "
        " acquired_at, expires_at, last_heartbeat_at) "
        "VALUES (gen_random_uuid(), %s, %s, %s, %s, 7, 'active', "
        " now() - interval '5 minutes', now() + interval '1 hour', now() - interval '5 minutes') "
        "RETURNING id, expires_at",
        (target["owner_id"], target["goal_id"], target["envelope_id"], identity),
    )
    print(f"  residue lease {str(lease['id'])[:8]} active, expires_at={lease['expires_at']}")
    assert one(
        "SELECT count(*) AS n FROM supervisor_goal_leases WHERE goal_id = %s AND status = 'active'",
        (str(target["goal_id"]),),
    )["n"] == 1

    pid = worker_pid()
    assert pid is not None, "no worker running to kill"
    os.kill(pid, signal.SIGKILL)
    wait_for(lambda: worker_pid() is None, timeout=30, label="worker process to die")
    print(f"  worker {pid} SIGKILLed")
    start_worker()
    print(f"  worker restarted as {worker_pid()}")

    state = wait_for(
        lambda: one(
            "SELECT status, released_at FROM supervisor_goal_leases WHERE id = %s AND status = 'released'",
            (str(lease["id"]),),
        ),
        timeout=90,
        label="the restarted worker to release its previous process's lease",
    )
    print(f"  lease released at {state['released_at']} (no TTL wait)")

    event = one(
        "SELECT detail->>'reason' AS reason FROM supervisor_goal_lease_events "
        "WHERE lease_id = %s AND event_type = 'released' ORDER BY created_at DESC LIMIT 1",
        (str(lease["id"]),),
    )
    assert event is not None and event["reason"] == "worker_process_restarted", event
    print(f"  durable history reason={event['reason']!r}")
    assert worker_pid() is not None, "the restarted worker must still be alive"

    print("T1 E2E OK")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"T1 E2E FAILED: {type(exc).__name__}: {exc}")
        raise
