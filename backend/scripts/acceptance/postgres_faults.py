#!/usr/bin/env python3
"""Real PostgreSQL fault-injection acceptance matrix (F13).

This does not mock the database. It opens real psycopg2 connections against the
supplied PostgreSQL URL and proves:

  * a rolled-back transaction leaves no row behind;
  * two concurrent writers produce exactly one deadlock (SQLSTATE 40P01), and
    both transactions are safely rolled back;
  * a lock wait is bounded by lock_timeout (55P03);
  * a long statement is bounded by statement_timeout (57014), the PostgreSQL
    equivalent of a commit/statement deadline;
  * a terminated backend is observed as a connection error and a fresh
    connection can read the database again;
  * two concurrent writers can both commit distinct rows.

Application-level lost-connection retry remains covered by the existing
db_retry tests; this matrix proves the database-side faults and rollback
semantics those tests rely on.

Usage:
    python scripts/acceptance/postgres_faults.py \
        --database-url postgresql://postgres@127.0.0.1:55432/lifeos_gate \
        --out /tmp/postgres-faults.json
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
from psycopg2 import errors


def _connect(url: str, *, autocommit: bool = False):
    conn = psycopg2.connect(url, connect_timeout=5)
    conn.autocommit = autocommit
    return conn


def _check(results: list[dict], name: str, ok: bool, detail: str) -> None:
    results.append({"check": name, "status": "PASS" if ok else "FAIL", "detail": detail})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("--database-url or DATABASE_URL is required")

    table = f"acceptance_fault_probe_{os.getpid()}"
    results: list[dict] = []
    admin = _connect(args.database_url, autocommit=True)
    try:
        with admin.cursor() as cur:
            cur.execute(
                f"CREATE TABLE {table} (id integer PRIMARY KEY, value text NOT NULL)"
            )
            cur.execute(f"INSERT INTO {table} (id, value) VALUES (1, 'one'), (2, 'two')")

        # 1. Transaction rollback leaves no durable row.
        conn = _connect(args.database_url)
        try:
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO {table} (id, value) VALUES (3, 'rolled-back')")
            conn.rollback()
        finally:
            conn.close()
        with admin.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table} WHERE id = 3")
            rolled_back = cur.fetchone()[0]
        _check(results, "transaction rollback", rolled_back == 0, f"rows after rollback={rolled_back}")

        # 2. Concurrent opposite-order updates produce exactly one deadlock.
        barrier = threading.Barrier(2)
        deadlock_errors: list[BaseException] = []
        other_errors: list[BaseException] = []

        def _deadlock_worker(first: int, second: int) -> None:
            worker = _connect(args.database_url)
            try:
                with worker.cursor() as cur:
                    cur.execute("SET statement_timeout = '10s'")
                    cur.execute(f"UPDATE {table} SET value = value || '-a' WHERE id = %s", (first,))
                    barrier.wait(timeout=5)
                    time.sleep(0.1)
                    cur.execute(f"UPDATE {table} SET value = value || '-b' WHERE id = %s", (second,))
                worker.commit()
            except BaseException as exc:  # noqa: BLE001 - the fault itself is the assertion
                worker.rollback()
                if getattr(exc, "pgcode", None) == "40P01" or isinstance(exc, errors.DeadlockDetected):
                    deadlock_errors.append(exc)
                else:
                    other_errors.append(exc)
            finally:
                worker.close()

        threads = [
            threading.Thread(target=_deadlock_worker, args=(1, 2)),
            threading.Thread(target=_deadlock_worker, args=(2, 1)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        alive = [thread.is_alive() for thread in threads]
        deadlock_ok = (
            not any(alive)
            and len(deadlock_errors) == 1
            and not other_errors
        )
        _check(
            results,
            "concurrent deadlock",
            deadlock_ok,
            f"deadlocks={len(deadlock_errors)} other_errors={[type(e).__name__ for e in other_errors]} alive={alive}",
        )

        # Reset the probe rows after the deadlock test.
        with admin.cursor() as cur:
            cur.execute(f"UPDATE {table} SET value = CASE id WHEN 1 THEN 'one' ELSE 'two' END")

        # 3. lock_timeout bounds a wait on a row held by another transaction.
        holder = _connect(args.database_url)
        waiter = _connect(args.database_url)
        try:
            with holder.cursor() as cur:
                cur.execute(f"UPDATE {table} SET value = 'held' WHERE id = 1")
            with waiter.cursor() as cur:
                cur.execute("SET lock_timeout = '500ms'")
                try:
                    cur.execute(f"UPDATE {table} SET value = 'blocked' WHERE id = 1")
                    lock_error = None
                except psycopg2.Error as exc:
                    lock_error = exc
            waiter.rollback()
            holder.rollback()
        finally:
            waiter.close()
            holder.close()
        lock_ok = getattr(lock_error, "pgcode", None) == "55P03" or isinstance(
            lock_error, errors.LockNotAvailable
        )
        _check(
            results,
            "lock timeout",
            lock_ok,
            f"sqlstate={getattr(lock_error, 'pgcode', None)} type={type(lock_error).__name__ if lock_error else None}",
        )

        # 4. statement_timeout bounds a long statement; the transaction is aborted.
        timeout_conn = _connect(args.database_url)
        try:
            with timeout_conn.cursor() as cur:
                cur.execute("SET statement_timeout = '300ms'")
                try:
                    cur.execute("SELECT pg_sleep(2)")
                    timeout_error = None
                except psycopg2.Error as exc:
                    timeout_error = exc
            timeout_conn.rollback()
        finally:
            timeout_conn.close()
        timeout_ok = getattr(timeout_error, "pgcode", None) == "57014" or isinstance(
            timeout_error, errors.QueryCanceled
        )
        _check(
            results,
            "statement/commit timeout",
            timeout_ok,
            f"sqlstate={getattr(timeout_error, 'pgcode', None)} type={type(timeout_error).__name__ if timeout_error else None}",
        )

        # 5. A terminated backend is observed, and a fresh connection still works.
        lost = _connect(args.database_url)
        lost_pid = lost.get_backend_pid()
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (lost_pid,))
            terminated = bool(cur.fetchone()[0])
        try:
            with lost.cursor() as cur:
                cur.execute("SELECT 1")
            lost_error = None
        except psycopg2.Error as exc:
            lost_error = exc
        finally:
            lost.close()
        recovered = _connect(args.database_url)
        try:
            with recovered.cursor() as cur:
                cur.execute("SELECT 1")
                recovered_value = cur.fetchone()[0]
        finally:
            recovered.close()
        lost_ok = terminated and lost_error is not None and recovered_value == 1
        _check(
            results,
            "lost connection and reconnect",
            lost_ok,
            f"terminated={terminated} error={type(lost_error).__name__ if lost_error else None} reconnect={recovered_value}",
        )

        # 6. Two concurrent writers can both commit distinct rows.
        write_errors: list[BaseException] = []

        def _writer(row_id: int) -> None:
            writer = _connect(args.database_url)
            try:
                with writer.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO {table} (id, value) VALUES (%s, %s)",
                        (row_id, f"concurrent-{row_id}"),
                    )
                writer.commit()
            except BaseException as exc:  # noqa: BLE001
                writer.rollback()
                write_errors.append(exc)
            finally:
                writer.close()

        writers = [threading.Thread(target=_writer, args=(10,)), threading.Thread(target=_writer, args=(11,))]
        for thread in writers:
            thread.start()
        for thread in writers:
            thread.join(timeout=10)
        with admin.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {table} WHERE id IN (10, 11)")
            concurrent_rows = cur.fetchone()[0]
        concurrent_ok = not write_errors and not any(t.is_alive() for t in writers) and concurrent_rows == 2
        _check(
            results,
            "concurrent writes",
            concurrent_ok,
            f"rows={concurrent_rows} errors={[type(e).__name__ for e in write_errors]}",
        )
    finally:
        try:
            with admin.cursor() as cur:
                cur.execute(f"DROP TABLE IF EXISTS {table}")
        finally:
            admin.close()

    status = "PASS" if all(item["status"] == "PASS" for item in results) else "FAIL"
    parsed = urlparse(args.database_url)
    payload = {
        "status": status,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "database": f"{parsed.hostname}:{parsed.port or 5432}/{parsed.path.lstrip('/')}",
        "checks": results,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if status == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
