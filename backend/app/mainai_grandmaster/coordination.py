"""Durable, fenced coordination for Grandmaster assignments.

SQLite is deliberately used as a narrow single-host authority: BEGIN IMMEDIATE serializes
contending processes, rows survive restarts, and monotonically increasing fencing tokens
prevent a stale holder from renewing or releasing a reclaimed lease. A kernel without this
store fails closed; it never falls back to process-local locks while claiming durable safety.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from app.mainai_grandmaster.types import ProposedMove, ResourceLease, WorkspaceMode


def normalize_physical_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class CoordinationStore(Protocol):
    def active(self, now: datetime) -> tuple[ResourceLease, ...]: ...
    def acquire(
        self, move: ProposedMove, workspace_path: str, ttl: timedelta, now: datetime
    ) -> ResourceLease | None: ...
    def heartbeat(
        self,
        workspace_key: str,
        agent_key: str,
        execution_id: str,
        fencing_token: int,
        ttl: timedelta,
        now: datetime,
    ) -> ResourceLease | None: ...
    def release(
        self,
        workspace_key: str,
        agent_key: str,
        execution_id: str,
        fencing_token: int,
        now: datetime,
    ) -> bool: ...
    def expire(self, now: datetime) -> int: ...


class SqliteCoordinationStore:
    """Authoritative for one shared host/filesystem; not a multi-host coordinator."""

    def __init__(self, path: str) -> None:
        if path == ":memory:":
            raise ValueError("in-memory coordination is not durable")
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS grandmaster_leases (
                    workspace_key TEXT PRIMARY KEY,
                    workspace_path TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    owner_agent TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    released_at TEXT,
                    fencing_token INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_grandmaster_lease_agent ON grandmaster_leases(owner_agent);
                CREATE INDEX IF NOT EXISTS ix_grandmaster_lease_task ON grandmaster_leases(task_key);
                CREATE INDEX IF NOT EXISTS ix_grandmaster_lease_path ON grandmaster_leases(workspace_path);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _lease(row: sqlite3.Row) -> ResourceLease:
        return ResourceLease(
            workspace_key=row["workspace_key"],
            workspace_path=row["workspace_path"],
            task_key=row["task_key"],
            owner_agent=row["owner_agent"],
            execution_id=row["execution_id"],
            branch=row["branch"],
            mode=WorkspaceMode.MUTABLE,
            acquired_at=_time(row["acquired_at"]),
            heartbeat_at=_time(row["heartbeat_at"]),
            expires_at=_time(row["expires_at"]),
            released_at=_time(row["released_at"]),
            fencing_token=row["fencing_token"],
        )

    @staticmethod
    def _is_active(row: sqlite3.Row, now: datetime) -> bool:
        return row["released_at"] is None and _time(row["expires_at"]) > now

    def active(self, now: datetime) -> tuple[ResourceLease, ...]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM grandmaster_leases WHERE released_at IS NULL AND expires_at > ?",
                (_stamp(now),),
            ).fetchall()
        return tuple(self._lease(row) for row in rows)

    def acquire(
        self, move: ProposedMove, workspace_path: str, ttl: timedelta, now: datetime
    ) -> ResourceLease | None:
        path = normalize_physical_path(workspace_path)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT * FROM grandmaster_leases").fetchall()
            active = [row for row in rows if self._is_active(row, now)]
            for row in active:
                same = (
                    row["owner_agent"] == move.agent_key
                    and row["execution_id"] == move.execution_id
                )
                if (
                    row["owner_agent"] == move.agent_key
                    or row["task_key"] == move.task_key
                    or row["workspace_path"] == path
                ):
                    if (
                        same
                        and row["task_key"] == move.task_key
                        and row["workspace_path"] == path
                    ):
                        conn.commit()
                        return self._lease(row)
                    conn.rollback()
                    return None
            old = conn.execute(
                "SELECT fencing_token FROM grandmaster_leases WHERE workspace_key = ?",
                (move.workspace_key,),
            ).fetchone()
            token = (old[0] if old else 0) + 1
            values = (
                move.workspace_key,
                path,
                move.task_key,
                move.agent_key,
                move.execution_id,
                move.branch,
                _stamp(now),
                _stamp(now),
                _stamp(now + ttl),
                None,
                token,
            )
            conn.execute(
                """INSERT INTO grandmaster_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_key) DO UPDATE SET
                  workspace_path=excluded.workspace_path, task_key=excluded.task_key,
                  owner_agent=excluded.owner_agent, execution_id=excluded.execution_id,
                  branch=excluded.branch, acquired_at=excluded.acquired_at,
                  heartbeat_at=excluded.heartbeat_at, expires_at=excluded.expires_at,
                  released_at=NULL, fencing_token=excluded.fencing_token""",
                values,
            )
            row = conn.execute(
                "SELECT * FROM grandmaster_leases WHERE workspace_key = ?",
                (move.workspace_key,),
            ).fetchone()
            conn.commit()
            return self._lease(row)

    def heartbeat(
        self,
        workspace_key: str,
        agent_key: str,
        execution_id: str,
        fencing_token: int,
        ttl: timedelta,
        now: datetime,
    ) -> ResourceLease | None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """UPDATE grandmaster_leases SET heartbeat_at=?, expires_at=?
                WHERE workspace_key=? AND owner_agent=? AND execution_id=? AND fencing_token=?
                  AND released_at IS NULL AND expires_at > ?""",
                (
                    _stamp(now),
                    _stamp(now + ttl),
                    workspace_key,
                    agent_key,
                    execution_id,
                    fencing_token,
                    _stamp(now),
                ),
            ).rowcount
            row = (
                conn.execute(
                    "SELECT * FROM grandmaster_leases WHERE workspace_key=?",
                    (workspace_key,),
                ).fetchone()
                if changed
                else None
            )
            conn.commit()
            return self._lease(row) if row else None

    def release(
        self,
        workspace_key: str,
        agent_key: str,
        execution_id: str,
        fencing_token: int,
        now: datetime,
    ) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """UPDATE grandmaster_leases SET released_at=? WHERE workspace_key=? AND owner_agent=?
                AND execution_id=? AND fencing_token=? AND released_at IS NULL""",
                (_stamp(now), workspace_key, agent_key, execution_id, fencing_token),
            ).rowcount
            conn.commit()
            return changed == 1

    def expire(self, now: datetime) -> int:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE grandmaster_leases SET released_at=? WHERE released_at IS NULL AND expires_at <= ?",
                (_stamp(now), _stamp(now)),
            ).rowcount
            conn.commit()
            return changed
