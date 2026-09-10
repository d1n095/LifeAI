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


class SubstrateEventType(StrEnum):
    JOB_ACCEPTED = "JOB_ACCEPTED"
    JOB_CLAIMED = "JOB_CLAIMED"
    JOB_PROGRESS = "JOB_PROGRESS"
    JOB_COMPLETED = "JOB_COMPLETED"
    JOB_FAILED = "JOB_FAILED"
    JOB_ABANDONED = "JOB_ABANDONED"
    JOB_CANCELLED = "JOB_CANCELLED"
    JOB_SUPERSEDED = "JOB_SUPERSEDED"
    PROVIDER_EXHAUSTED = "PROVIDER_EXHAUSTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    CLAIM_EXPIRED = "CLAIM_EXPIRED"
    WORKTREE_CONFLICT = "WORKTREE_CONFLICT"
    PROTECTED_REF_VIOLATION = "PROTECTED_REF_VIOLATION"
    COMPLETION_REJECTED = "COMPLETION_REJECTED"


PROTECTED_REFS = frozenset({"#245", "818dfb732da47901eb5ae06ffdd9c829fe00c4c5", "main", "master"})


class FailureClass(StrEnum):
    TRANSIENT = "TRANSIENT"
    PROVIDER_LIMIT = "PROVIDER_LIMIT"
    WORKTREE_CONFLICT = "WORKTREE_CONFLICT"
    AUTHORITY_REVOKED = "AUTHORITY_REVOKED"
    PROTECTED_REF = "PROTECTED_REF"
    INVALID_COMPLETION = "INVALID_COMPLETION"
    PROCESS_LOST = "PROCESS_LOST"
    PERMANENT = "PERMANENT"


class Capability(StrEnum):
    CODE_EDIT = "code_edit"
    REPO_READ = "repo_read"
    TEST_RUN = "test_run"
    BROWSER = "browser"
    NETWORK = "network"
    LONG_RUNNING = "long_running"
    REVIEW = "review"
    FILESYSTEM_WRITE = "filesystem_write"


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


@dataclass(frozen=True)
class SubstrateEvent:
    event_id: str
    event_type: SubstrateEventType
    job_id: str | None
    attempt_id: str | None
    occurred_at: str
    payload: dict


@dataclass(frozen=True)
class CompletionEnvelope:
    evidence: CompletionEvidence
    provider: str
    reported_sha: str
    changed_files: tuple[str, ...] = ()
    test_evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class DirectorJob:
    job_id: str
    owner_id: str
    program: str
    capabilities: tuple[str, ...]


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
        raise SubstrateError("stale worktree SHA does not match the expected SHA")
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
            CREATE TABLE IF NOT EXISTS recovery_journal(
              event_id TEXT PRIMARY KEY, owner_id TEXT, event_type TEXT NOT NULL, job_id TEXT,
              attempt_id TEXT, occurred_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS execution_outbox(
              event_id TEXT PRIMARY KEY, owner_id TEXT, event_type TEXT NOT NULL,
              job_id TEXT, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT, blocked_reason TEXT
            );
            CREATE TRIGGER IF NOT EXISTS recovery_journal_no_update BEFORE UPDATE ON recovery_journal BEGIN SELECT RAISE(ABORT, 'recovery journal is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS recovery_journal_no_delete BEFORE DELETE ON recovery_journal BEGIN SELECT RAISE(ABORT, 'recovery journal is append-only'); END;
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(jobs)")}
            for name, definition in (("owner_id", "TEXT"), ("program", "TEXT"), ("capabilities", "TEXT"), ("failure_class", "TEXT"), ("progress", "TEXT"), ("dependencies", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
            journal_columns = {row[1] for row in db.execute("PRAGMA table_info(recovery_journal)")}
            if "owner_id" not in journal_columns:
                db.execute("ALTER TABLE recovery_journal ADD COLUMN owner_id TEXT")

    def _journal(self, db, event_type: SubstrateEventType, *, job_id: str | None = None, attempt_id: str | None = None, payload: dict | None = None):
        safe = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))[:4000]
        owner = db.execute("SELECT owner_id FROM jobs WHERE id=?", (job_id,)).fetchone()[0] if job_id and db.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone() else None
        event_id = str(uuid.uuid4())
        db.execute("INSERT INTO recovery_journal(event_id,owner_id,event_type,job_id,attempt_id,occurred_at,payload) VALUES(?,?,?,?,?,?,?)", (event_id, owner, event_type, job_id, attempt_id, _iso(_now()), safe))
        db.execute("INSERT INTO execution_outbox(event_id,owner_id,event_type,job_id,payload) VALUES(?,?,?,?,?)", (event_id, owner, event_type, job_id, safe))

    def journal_events(self, *, job_id: str | None = None, owner_id: str | None = None) -> list[SubstrateEvent]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM recovery_journal WHERE (job_id=? OR ? IS NULL) AND (owner_id=? OR ? IS NULL) ORDER BY occurred_at,event_id", (job_id, job_id, owner_id, owner_id)).fetchall()
        return [SubstrateEvent(row["event_id"], SubstrateEventType(row["event_type"]), row["job_id"], row["attempt_id"], row["occurred_at"], json.loads(row["payload"])) for row in rows]

    def submit_job(self, *, owner_id: str, program: str, capabilities: Iterable[str] = (), provider: str | None = None, base_sha: str | None = None, worktree: str | None = None, max_active: int = 1, dependencies: dict[str, str] | None = None) -> DirectorJob:
        if not owner_id or not program or max_active < 1:
            raise SubstrateError("owner, program and active-job bound are required")
        caps = tuple(sorted({Capability(c).value for c in capabilities}))
        with self._connect() as db:
            active = db.execute("SELECT COUNT(*) FROM jobs WHERE program=? AND state IN (?,?)", (program, JobState.QUEUED, JobState.RUNNING)).fetchone()[0]
            if active >= max_active:
                raise SubstrateError("program backpressure limit reached")
            if provider and db.execute("SELECT COUNT(*) FROM jobs WHERE provider=? AND state IN (?,?)", (provider, JobState.QUEUED, JobState.RUNNING)).fetchone()[0] >= max_active:
                raise SubstrateError("provider backpressure limit reached")
            if worktree and db.execute("SELECT COUNT(*) FROM jobs WHERE worktree=? AND state IN (?,?)", (worktree, JobState.QUEUED, JobState.RUNNING)).fetchone()[0] >= 1:
                raise SubstrateError("worktree backpressure limit reached")
            job_id = str(uuid.uuid4())
            db.execute("INSERT INTO jobs(id,state,base_sha,worktree,provider,created_at,owner_id,program,capabilities,progress,dependencies) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (job_id, JobState.QUEUED, base_sha, worktree, provider, _iso(_now()), owner_id, program, json.dumps(caps), "{}", json.dumps(dependencies or {}, sort_keys=True)))
            self._journal(db, SubstrateEventType.JOB_ACCEPTED, job_id=job_id, payload={"owner_id": owner_id, "program": program, "capabilities": caps})
        return DirectorJob(job_id, owner_id, program, caps)

    def eligible_jobs(self, *, owner_id: str | None = None, limit: int = 100) -> list[str]:
        """Return only queued jobs whose exact dependency SHAs are completed and certified."""
        with self._connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE state=? AND (owner_id=? OR ? IS NULL) ORDER BY created_at,id LIMIT ?", (JobState.QUEUED, owner_id, owner_id, limit)).fetchall()
            eligible = []
            for row in rows:
                dependencies = json.loads(row["dependencies"] or "{}")
                ok = True
                for dep_id, required_sha in dependencies.items():
                    dep = db.execute("SELECT state,result FROM jobs WHERE id=?", (dep_id,)).fetchone()
                    if dep is None or dep["state"] != JobState.COMPLETED or not dep["result"] or json.loads(dep["result"]).get("observed_sha") != required_sha:
                        ok = False
                        break
                if ok:
                    eligible.append(row["id"])
            return eligible

    def deliver_events(self, handler, *, limit: int = 100, max_attempts: int = 5) -> tuple[int, int, int]:
        """Deliver minimal outbox records with bounded retry and poison-event isolation."""
        delivered = dead = skipped = 0
        with self._connect() as db:
            rows = db.execute("SELECT * FROM execution_outbox WHERE state='pending' AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY event_id LIMIT ?", (_iso(_now()), limit)).fetchall()
            for row in rows:
                try:
                    handler({"event_id": row["event_id"], "event_type": row["event_type"], "job_id": row["job_id"], "payload": json.loads(row["payload"])})
                except Exception:
                    attempts = int(row["attempts"]) + 1
                    if attempts >= max_attempts:
                        db.execute("UPDATE execution_outbox SET state='dead_letter',attempts=?,blocked_reason='bounded delivery failure' WHERE event_id=?", (attempts, row["event_id"]))
                        dead += 1
                    else:
                        db.execute("UPDATE execution_outbox SET attempts=?,next_attempt_at=? WHERE event_id=?", (attempts, _iso(_now() + timedelta(seconds=min(3600, 2 ** attempts))), row["event_id"]))
                        skipped += 1
                    continue
                db.execute("UPDATE execution_outbox SET state='delivered',attempts=attempts+1 WHERE event_id=?", (row["event_id"],))
                delivered += 1
            db.commit()
        return delivered, dead, skipped

    def report_progress(self, claim: JobClaim, *, phase: str, current: int, total: int | None = None) -> SubstrateEvent:
        if not phase or current < 0:
            raise SubstrateError("invalid progress")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._fence(db, claim)
            payload = {"phase": phase, "current": current, "total": total}
            db.execute("UPDATE jobs SET progress=? WHERE id=?", (json.dumps(payload), claim.job_id))
            self._journal(db, SubstrateEventType.JOB_PROGRESS, job_id=claim.job_id, attempt_id=claim.attempt_id, payload=payload)
            db.commit()
        return self.journal_events(job_id=claim.job_id)[-1]

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

    def release_claim(self, claim: JobClaim) -> None:
        """Release only the currently fenced claim; stale workers cannot release a new one."""
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self._fence(db, claim)
            db.execute("UPDATE jobs SET state=?,worker_id=NULL,attempt_id=NULL,lease_until=NULL WHERE id=?", (JobState.ABANDONED, claim.job_id))
            if row["worktree"]:
                db.execute("DELETE FROM worktree_claims WHERE job_id=? AND attempt_id=?", (claim.job_id, claim.attempt_id))
            self._journal(db, SubstrateEventType.JOB_ABANDONED, job_id=claim.job_id, attempt_id=claim.attempt_id)
            db.commit()

    def request_cancel(self, job_id: str) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET cancel_requested=1,state=CASE WHEN state=? THEN ? ELSE state END,lease_until=CASE WHEN state=? THEN NULL ELSE lease_until END WHERE id=? AND state IN (?,?)", (JobState.QUEUED, JobState.CANCELLED, JobState.RUNNING, job_id, JobState.QUEUED, JobState.RUNNING))
            self._journal(db, SubstrateEventType.JOB_CANCELLED, job_id=job_id)
            db.commit()

    def complete(self, claim: JobClaim, *, evidence: CompletionEvidence) -> None:
        if evidence.job_id != claim.job_id or evidence.attempt_id != claim.attempt_id or not evidence.clean or evidence.protected or not evidence.changed:
            raise SubstrateError("completion evidence is invalid")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._fence(db, claim)
            current = inspect_worktree(claim.worktree, expected_sha=evidence.observed_sha, protected_refs=PROTECTED_REFS)
            if current["branch"] != evidence.branch or not current["clean"] or current["sha"] == claim.base_sha or current["protected"]:
                raise SubstrateError("completion evidence is stale or targets a protected ref")
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


def completion_evidence(claim: JobClaim, *, protected_refs: Iterable[str] = PROTECTED_REFS) -> CompletionEvidence:
    if not claim.worktree or not claim.base_sha:
        raise SubstrateError("completion requires a worktree and verified base SHA")
    state = inspect_worktree(claim.worktree, protected_refs=protected_refs)
    return CompletionEvidence(claim.job_id, claim.attempt_id, claim.worktree, str(state["branch"]), claim.base_sha, str(state["sha"]), str(state["sha"]) != claim.base_sha, bool(state["clean"]), bool(state["protected"]), _iso(_now()))


def classify_failure(error: BaseException) -> FailureClass:
    if isinstance(error, LeaseLostError):
        return FailureClass.AUTHORITY_REVOKED
    if isinstance(error, (TimeoutError, ConnectionError, OSError)):
        return FailureClass.TRANSIENT
    if isinstance(error, ProviderError) and error.category in {"rate_limited", "not_configured"}:
        return FailureClass.PROVIDER_LIMIT
    if isinstance(error, SubstrateError) and "protected" in str(error).lower():
        return FailureClass.PROTECTED_REF
    if isinstance(error, SubstrateError) and "worktree" in str(error).lower():
        return FailureClass.WORKTREE_CONFLICT
    return FailureClass.PERMANENT


class DirectorContract:
    """Narrow command/event boundary for a future Director; no orchestration policy lives here."""

    def __init__(self, substrate: ExecutionSubstrate):
        self.substrate = substrate

    def submit_job(self, **kwargs) -> DirectorJob:
        return self.substrate.submit_job(**kwargs)

    def eligible_jobs(self, *, owner_id: str | None = None, limit: int = 100) -> list[str]:
        return self.substrate.eligible_jobs(owner_id=owner_id, limit=limit)

    def deliver_events(self, handler, *, limit: int = 100, max_attempts: int = 5) -> tuple[int, int, int]:
        return self.substrate.deliver_events(handler, limit=limit, max_attempts=max_attempts)

    def claim_job(self, job_id: str, *, owner_id: str, worker_id: str, lease_seconds: int = 60) -> JobClaim:
        if self._owner(job_id) != owner_id:
            raise SubstrateError("owner scope mismatch")
        try:
            claim = self.substrate.claim(job_id, worker_id=worker_id, lease_seconds=lease_seconds)
        except SubstrateError as exc:
            with self.substrate._connect() as db:
                self.substrate._journal(db, SubstrateEventType.WORKTREE_CONFLICT if "worktree" in str(exc) else SubstrateEventType.CLAIM_EXPIRED, job_id=job_id, payload={"failure_class": classify_failure(exc).value})
            raise
        with self.substrate._connect() as db:
            self.substrate._journal(db, SubstrateEventType.JOB_CLAIMED, job_id=job_id, attempt_id=claim.attempt_id, payload={"worker_id": worker_id, "lease_generation": claim.lease_generation})
        return claim

    def heartbeat(self, claim: JobClaim, *, process_nonce: str, pid: int | None = None, lease_seconds: int = 60) -> None:
        self.substrate.heartbeat(claim.worker_id, process_nonce=process_nonce, pid=pid, lease_until=_now() + timedelta(seconds=lease_seconds))
        self.substrate.renew(claim, lease_seconds=lease_seconds)
        with self.substrate._connect() as db:
            self.substrate._journal(db, SubstrateEventType.JOB_PROGRESS, job_id=claim.job_id, attempt_id=claim.attempt_id, payload={"process_alive": True, "provider_responsive": None, "job_progress": None})

    def report_progress(self, claim: JobClaim, **kwargs) -> SubstrateEvent:
        return self.substrate.report_progress(claim, **kwargs)

    def set_provider_state(self, name: str, *, state: ProviderState, authorized: bool, remaining_units: int | None = None) -> ProviderSnapshot:
        snapshot = self.substrate.provider_state(name, state=state, authorized=authorized, remaining_units=remaining_units)
        event_type = SubstrateEventType.PROVIDER_EXHAUSTED if state is ProviderState.EXHAUSTED else SubstrateEventType.PROVIDER_UNAVAILABLE if state is ProviderState.UNAVAILABLE else SubstrateEventType.JOB_PROGRESS
        with self.substrate._connect() as db:
            self.substrate._journal(db, event_type, payload={"provider": name, "state": state.value, "authorized": authorized})
        return snapshot

    def report_completion(self, claim: JobClaim, envelope: CompletionEnvelope, *, protected_refs: Iterable[str] = ()) -> SubstrateEvent:
        try:
            if envelope.evidence.observed_sha != envelope.reported_sha or envelope.provider != self._provider(claim.job_id):
                raise SubstrateError("reported completion does not match observed state")
            if envelope.changed_files and len(envelope.changed_files) > 1000:
                raise SubstrateError("changed file summary exceeds bound")
            self.substrate.complete(claim, evidence=envelope.evidence)
        except BaseException as exc:
            with self.substrate._connect() as db:
                self.substrate._journal(db, SubstrateEventType.COMPLETION_REJECTED, job_id=claim.job_id, attempt_id=claim.attempt_id, payload={"failure_class": classify_failure(exc).value})
            raise
        with self.substrate._connect() as db:
            self.substrate._journal(db, SubstrateEventType.JOB_COMPLETED, job_id=claim.job_id, attempt_id=claim.attempt_id, payload={"provider": envelope.provider, "observed_sha": envelope.evidence.observed_sha, "tests": len(envelope.test_evidence_refs)})
        return self.substrate.journal_events(job_id=claim.job_id)[-1]

    def report_failure(self, claim: JobClaim, error: BaseException) -> SubstrateEvent:
        category = classify_failure(error)
        with self.substrate._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.substrate._fence(db, claim)
            db.execute("UPDATE jobs SET state=?,failure_class=?,lease_until=NULL WHERE id=?", (JobState.FAILED, category, claim.job_id))
            self.substrate._journal(db, SubstrateEventType.JOB_FAILED, job_id=claim.job_id, attempt_id=claim.attempt_id, payload={"failure_class": category.value})
            db.commit()
        return self.substrate.journal_events(job_id=claim.job_id)[-1]

    def cancel_job(self, job_id: str) -> SubstrateEvent:
        self.substrate.request_cancel(job_id)
        with self.substrate._connect() as db:
            db.execute("UPDATE jobs SET state=? WHERE id=? AND state=?", (JobState.CANCELLED, job_id, JobState.QUEUED))
            self.substrate._journal(db, SubstrateEventType.JOB_CANCELLED, job_id=job_id)
        return self.substrate.journal_events(job_id=job_id)[-1]

    def supersede_job(self, claim: JobClaim) -> SubstrateEvent:
        with self.substrate._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.substrate._fence(db, claim)
            db.execute("UPDATE jobs SET superseded=1,state=?,lease_until=NULL WHERE id=?", (JobState.SUPERSEDED, claim.job_id))
            self.substrate._journal(db, SubstrateEventType.JOB_SUPERSEDED, job_id=claim.job_id, attempt_id=claim.attempt_id)
            db.commit()
        return self.substrate.journal_events(job_id=claim.job_id)[-1]

    def release_claim(self, claim: JobClaim) -> None:
        self.substrate.release_claim(claim)

    def recover_incomplete_jobs(self) -> list[SubstrateEvent]:
        self.substrate.abandon_stale()
        with self.substrate._connect() as db:
            rows = db.execute("SELECT id,worktree,state,result,cancel_requested FROM jobs WHERE state IN (?,?,?)", (JobState.RUNNING, JobState.COMPLETED, JobState.CANCELLED)).fetchall()
            for row in rows:
                if row["state"] == JobState.RUNNING and row["cancel_requested"]:
                    db.execute("UPDATE jobs SET state=?,lease_until=NULL,worker_id=NULL,attempt_id=NULL WHERE id=?", (JobState.CANCELLED, row["id"]))
                    self.substrate._journal(db, SubstrateEventType.JOB_CANCELLED, job_id=row["id"], payload={"recovered": True})
                elif row["state"] == JobState.COMPLETED and not row["result"]:
                    db.execute("UPDATE jobs SET state=?,failure_class=? WHERE id=?", (JobState.FAILED, FailureClass.INVALID_COMPLETION, row["id"]))
                    self.substrate._journal(db, SubstrateEventType.COMPLETION_REJECTED, job_id=row["id"], payload={"failure_class": FailureClass.INVALID_COMPLETION.value})
                elif row["worktree"]:
                    try:
                        inspect_worktree(row["worktree"])
                    except SubstrateError:
                        self.substrate._journal(db, SubstrateEventType.WORKTREE_CONFLICT, job_id=row["id"], payload={"failure_class": FailureClass.WORKTREE_CONFLICT.value})
        return self.substrate.journal_events()

    def inspect_job(self, job_id: str) -> dict:
        with self.substrate._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise SubstrateError("job not found")
        return dict(row)

    def _owner(self, job_id: str) -> str | None:
        with self.substrate._connect() as db:
            row = db.execute("SELECT owner_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row[0] if row else None

    def _provider(self, job_id: str) -> str | None:
        with self.substrate._connect() as db:
            row = db.execute("SELECT provider FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row[0] if row else None
