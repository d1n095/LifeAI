"""Provider-neutral execution substrate for a future Development Director.

This module deliberately composes no orchestration policy and registers no routes.  It stores
only execution truth (job claims, heartbeats, provider health, and bounded evidence) in a small
durable SQLite ledger suitable for tests and a local controller.  A production adapter can map
the same operations to ``mainai_jobs``; it must preserve the fencing and evidence invariants.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from app.providers.base import ChatResult, LLMProvider, Message, ProviderError


class SubstrateError(RuntimeError):
    pass


class LeaseLostError(SubstrateError):
    pass


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    ABANDONED = "abandoned"


class ProviderState(StrEnum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"
    UNAUTHORIZED = "unauthorized"


@dataclass(frozen=True)
class JobClaim:
    job_id: str
    attempt_id: str
    worker_id: str
    lease_generation: int
    base_sha: str | None
    worktree: str | None


@dataclass(frozen=True)
class CompletionEvidence:
    job_id: str
    attempt_id: str
    worktree: str
    branch: str
    base_sha: str
    observed_sha: str
    changed: bool
    clean: bool
    protected: bool
    verified_at: str


@dataclass(frozen=True)
class ProviderSnapshot:
    name: str
    state: ProviderState
    authorized: bool
    remaining_units: int | None
    checked_at: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True, check=False, timeout=30)
    if result.returncode:
        raise SubstrateError(f"git command failed: {result.stderr[:400]}")
    return result.stdout.strip()


def inspect_worktree(path: str | Path, *, expected_sha: str | None = None, protected_refs: Iterable[str] = ()) -> dict[str, object]:
    root = Path(path).resolve()
    if not root.is_dir():
        raise SubstrateError("worktree does not exist")
    branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    sha = _git(root, "rev-parse", "HEAD")
    clean = not bool(_git(root, "status", "--porcelain"))
    protected = branch in set(protected_refs)
    if expected_sha is not None and sha != expected_sha:
        raise SubstrateError("worktree SHA does not match the expected SHA")
    if protected:
        raise SubstrateError("protected ref cannot be used as an execution worktree")
    return {"branch": branch, "sha": sha, "clean": clean, "protected": protected}


class ExecutionSubstrate:
    """Durable, fenced state machine with no provider invocation side effects."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _init(self):
        with self._connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, state TEXT NOT NULL, base_sha TEXT, worktree TEXT,
              provider TEXT, lease_generation INTEGER NOT NULL DEFAULT 0,
              worker_id TEXT, attempt_id TEXT, lease_until TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
              superseded INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, result TEXT
            );
            CREATE TABLE IF NOT EXISTS heartbeats(
              worker_id TEXT PRIMARY KEY, process_nonce TEXT NOT NULL, pid INTEGER NOT NULL,
              last_seen TEXT NOT NULL, lease_until TEXT
            );
            CREATE TABLE IF NOT EXISTS providers(
              name TEXT PRIMARY KEY, state TEXT NOT NULL, authorized INTEGER NOT NULL,
              remaining_units INTEGER, checked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS worktree_claims(
              worktree TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt_id TEXT NOT NULL
            );
            """)

    def create_job(self, *, base_sha: str | None = None, worktree: str | None = None, provider: str | None = None) -> str:
        job_id = str(uuid.uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO jobs(id,state,base_sha,worktree,provider,created_at) VALUES(?,?,?,?,?,?)", (job_id, JobState.QUEUED, base_sha, worktree, provider, _iso(_now())))
        return job_id

    def heartbeat(self, worker_id: str, *, process_nonce: str, pid: int | None = None, lease_until: datetime | None = None) -> None:
        if not worker_id or not process_nonce:
            raise SubstrateError("worker identity is required")
        with self._connect() as db:
            db.execute("INSERT INTO heartbeats(worker_id,process_nonce,pid,last_seen,lease_until) VALUES(?,?,?,?,?) ON CONFLICT(worker_id) DO UPDATE SET process_nonce=excluded.process_nonce,pid=excluded.pid,last_seen=excluded.last_seen,lease_until=excluded.lease_until", (worker_id, process_nonce, pid or os.getpid(), _iso(_now()), _iso(lease_until) if lease_until else None))

    def stale_workers(self, *, now: datetime | None = None, timeout_seconds: int = 60) -> list[str]:
        at = now or _now()
        with self._connect() as db:
            rows = db.execute("SELECT worker_id,last_seen FROM heartbeats").fetchall()
        return [row[0] for row in rows if _parse(row[1]) + timedelta(seconds=timeout_seconds) <= at]

    def claim(self, job_id: str, *, worker_id: str, lease_seconds: int = 60) -> JobClaim:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        attempt_id = str(uuid.uuid4())
        until = _now() + timedelta(seconds=lease_seconds)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None or row["state"] not in (JobState.QUEUED, JobState.ABANDONED):
                db.rollback()
                raise SubstrateError("job is not claimable")
            if row["worktree"]:
                occupied = db.execute("SELECT job_id FROM worktree_claims WHERE worktree=?", (row["worktree"],)).fetchone()
                if occupied and occupied[0] != job_id:
                    db.rollback()
                    raise SubstrateError("worktree is already claimed")
                db.execute("INSERT OR REPLACE INTO worktree_claims(worktree,job_id,attempt_id) VALUES(?,?,?)", (row["worktree"], job_id, attempt_id))
            generation = int(row["lease_generation"]) + 1
            db.execute("UPDATE jobs SET state=?,worker_id=?,attempt_id=?,lease_generation=?,lease_until=? WHERE id=?", (JobState.RUNNING, worker_id, attempt_id, generation, _iso(until), job_id))
            db.commit()
        return JobClaim(job_id, attempt_id, worker_id, generation, row["base_sha"], row["worktree"])

    def _fence(self, db, claim: JobClaim):
        row = db.execute("SELECT * FROM jobs WHERE id=? AND state=? AND worker_id=? AND attempt_id=? AND lease_generation=?", (claim.job_id, JobState.RUNNING, claim.worker_id, claim.attempt_id, claim.lease_generation)).fetchone()
        if row is None or row["cancel_requested"] or row["superseded"]:
            raise LeaseLostError("job claim is no longer authoritative")
        if row["lease_until"] and _parse(row["lease_until"]) <= _now():
            raise LeaseLostError("job lease expired")
        return row

    def renew(self, claim: JobClaim, *, lease_seconds: int = 60) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._fence(db, claim)
            db.execute("UPDATE jobs SET lease_until=? WHERE id=?", (_iso(_now() + timedelta(seconds=lease_seconds)), claim.job_id))
            db.commit()

    def abandon_stale(self, *, now: datetime | None = None) -> int:
        at = now or _now()
        with self._connect() as db:
            rows = db.execute("SELECT id,worktree FROM jobs WHERE state=? AND lease_until IS NOT NULL AND lease_until <= ?", (JobState.RUNNING, _iso(at))).fetchall()
            for row in rows:
                db.execute("UPDATE jobs SET state=?,worker_id=NULL,attempt_id=NULL,lease_until=NULL WHERE id=?", (JobState.ABANDONED, row["id"]))
                if row["worktree"]:
                    db.execute("DELETE FROM worktree_claims WHERE job_id=?", (row["id"],))
            db.commit()
            return len(rows)

    def request_cancel(self, job_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE jobs SET cancel_requested=1 WHERE id=? AND state=?", (job_id, JobState.RUNNING))
            db.commit()

    def complete(self, claim: JobClaim, *, evidence: CompletionEvidence) -> None:
        if evidence.job_id != claim.job_id or evidence.attempt_id != claim.attempt_id or not evidence.clean or evidence.protected or not evidence.changed:
            raise SubstrateError("completion evidence is invalid")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._fence(db, claim)
            db.execute("UPDATE jobs SET state=?,result=?,lease_until=NULL WHERE id=?", (JobState.COMPLETED, json.dumps(evidence.__dict__, sort_keys=True), claim.job_id))
            if claim.worktree:
                db.execute("DELETE FROM worktree_claims WHERE job_id=?", (claim.job_id,))
            db.commit()

    def provider_state(self, name: str, *, state: ProviderState, authorized: bool, remaining_units: int | None = None) -> ProviderSnapshot:
        checked = _iso(_now())
        with self._connect() as db:
            db.execute("INSERT INTO providers(name,state,authorized,remaining_units,checked_at) VALUES(?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET state=excluded.state,authorized=excluded.authorized,remaining_units=excluded.remaining_units,checked_at=excluded.checked_at", (name, state, int(authorized), remaining_units, checked))
            db.commit()
        return ProviderSnapshot(name, state, authorized, remaining_units, checked)

    def retry_or_reassign(self, job_id: str, *, new_provider: str | None = None) -> None:
        with self._connect() as db:
            db.execute("UPDATE jobs SET state=?,provider=COALESCE(?,provider),worker_id=NULL,attempt_id=NULL,lease_until=NULL WHERE id=? AND state IN (?,?)", (JobState.QUEUED, new_provider, job_id, JobState.ABANDONED, JobState.FAILED))
            db.commit()


class DeterministicFakeProvider(LLMProvider):
    """No-network provider used solely for orchestration tests."""
    name = "fake"

    def __init__(self, *, remaining_units: int | None = None, fail_after: int | None = None):
        self.remaining_units = remaining_units
        self.fail_after = fail_after
        self.calls = 0

    async def chat(self, messages: list[Message], model: str, *, timeout: float | None = None, **kwargs) -> ChatResult:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise ProviderError("fake provider exhausted", category="rate_limited", provider_request_may_have_left=False)
        if self.remaining_units is not None and self.remaining_units <= 0:
            raise ProviderError("fake provider exhausted", category="rate_limited", provider_request_may_have_left=False)
        if self.remaining_units is not None:
            self.remaining_units -= 1
        return ChatResult(content=f"fake:{messages[-1].content if messages else ''}", provider=self.name, model=model, raw_usage={"calls": self.calls})

    async def embed(self, texts: list[str], model: str, *, timeout: float | None = None) -> list[list[float]]:
        if self.remaining_units is not None and self.remaining_units < len(texts):
            raise ProviderError("fake provider exhausted", category="rate_limited", provider_request_may_have_left=False)
        if self.remaining_units is not None:
            self.remaining_units -= len(texts)
        return [[float(len(text))] for text in texts]

    def is_configured(self) -> bool:
        return True


def completion_evidence(claim: JobClaim, *, protected_refs: Iterable[str] = ()) -> CompletionEvidence:
    if not claim.worktree or not claim.base_sha:
        raise SubstrateError("completion requires a worktree and verified base SHA")
    state = inspect_worktree(claim.worktree, protected_refs=protected_refs)
    return CompletionEvidence(claim.job_id, claim.attempt_id, claim.worktree, str(state["branch"]), claim.base_sha, str(state["sha"]), str(state["sha"]) != claim.base_sha, bool(state["clean"]), bool(state["protected"]), _iso(_now()))
