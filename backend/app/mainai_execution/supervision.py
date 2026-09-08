"""Durable, provider-neutral supervision for the execution substrate.

The supervisor stores observations and delivery bookkeeping only.  Job claims, leases and
completion authority remain in :class:`ExecutionSubstrate`; an idle/result event can therefore
request work without becoming authority by itself.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from app.mainai_execution.substrate import ExecutionSubstrate


class AgentState(StrEnum):
    REGISTERED = "REGISTERED"
    AVAILABLE = "AVAILABLE"
    CLAIMED = "CLAIMED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PROGRESSING = "PROGRESSING"
    WAITING = "WAITING"
    RESULT_SUBMITTED = "RESULT_SUBMITTED"
    VERIFYING = "VERIFYING"
    BLOCKED = "BLOCKED"
    USAGE_EXHAUSTED = "USAGE_EXHAUSTED"
    CRASHED = "CRASHED"
    COMPLETED = "COMPLETED"
    IDLE = "IDLE"
    DISCONNECTED = "DISCONNECTED"
    QUARANTINED = "QUARANTINED"


class SupervisionEvent(StrEnum):
    AGENT_REGISTERED = "AGENT_REGISTERED"
    AGENT_HEARTBEAT = "AGENT_HEARTBEAT"
    AGENT_PROGRESS = "AGENT_PROGRESS"
    AGENT_RESULT_SUBMITTED = "AGENT_RESULT_SUBMITTED"
    AGENT_IDLE = "AGENT_IDLE"
    AGENT_BLOCKED = "AGENT_BLOCKED"
    AGENT_CRASHED = "AGENT_CRASHED"
    AGENT_USAGE_EXHAUSTED = "AGENT_USAGE_EXHAUSTED"
    JOB_RESUME_REQUESTED = "JOB_RESUME_REQUESTED"
    JOB_REASSIGNED = "JOB_REASSIGNED"
    JOB_VERIFICATION_REQUIRED = "JOB_VERIFICATION_REQUIRED"
    CONTINUATION_SENT = "CONTINUATION_SENT"
    BLOCKER_VALIDATED = "BLOCKER_VALIDATED"


class BlockerClass(StrEnum):
    REAL_EXTERNAL = "REAL_EXTERNAL"
    FOUNDER_REQUIRED = "FOUNDER_REQUIRED"
    AUTHORITY_MISSING = "AUTHORITY_MISSING"
    LOCAL_REPAIR = "LOCAL_REPAIR"
    INCOMPLETE_IMPLEMENTATION = "INCOMPLETE_IMPLEMENTATION"
    TEMPORARY_PROVIDER = "TEMPORARY_PROVIDER"
    UNKNOWN = "UNKNOWN"


class SupervisionError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentObservation:
    agent_id: str
    owner_id: str
    state: AgentState
    process_nonce: str
    pid: int | None
    job_id: str | None
    attempt_id: str | None
    provider: str | None
    heartbeat_at: str
    progress_key: str | None = None


@dataclass(frozen=True)
class Continuation:
    message_id: str
    job_id: str
    attempt_id: str | None
    kind: str
    reason: str
    sequence: int


@dataclass(frozen=True)
class BlockerDecision:
    classification: BlockerClass
    founder_required: bool
    retryable: bool
    reason: str


def classify_blocker(reason: str, *, external_unavailable: bool = False, founder_only: bool = False) -> BlockerDecision:
    text = (reason or "").lower()
    if founder_only or any(token in text for token in ("founder approval", "owner decision", "irreversible")):
        return BlockerDecision(BlockerClass.FOUNDER_REQUIRED, True, False, reason)
    if external_unavailable:
        return BlockerDecision(BlockerClass.REAL_EXTERNAL, False, True, reason)
    if any(token in text for token in ("missing test", "missing code", "not implemented", "pytest", "ruff", "worktree", "environment")):
        return BlockerDecision(BlockerClass.LOCAL_REPAIR if "environment" in text or "worktree" in text else BlockerClass.INCOMPLETE_IMPLEMENTATION, False, True, reason)
    if any(token in text for token in ("provider unavailable", "rate limit", "usage exhausted")):
        return BlockerDecision(BlockerClass.TEMPORARY_PROVIDER, False, True, reason)
    if any(token in text for token in ("unauthorized", "permission", "authorization")):
        return BlockerDecision(BlockerClass.AUTHORITY_MISSING, False, False, reason)
    return BlockerDecision(BlockerClass.UNKNOWN, False, False, reason)


class ContinuousSupervisor:
    """Restart-safe controller around the substrate's canonical job/lease state."""

    def __init__(self, substrate: ExecutionSubstrate):
        self.substrate = substrate
        self.path = Path(substrate.path).with_name(f"{Path(substrate.path).stem}-supervision.sqlite")
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
            CREATE TABLE IF NOT EXISTS agents(
              agent_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, state TEXT NOT NULL,
              process_nonce TEXT NOT NULL, pid INTEGER, job_id TEXT, attempt_id TEXT,
              provider TEXT, heartbeat_at TEXT NOT NULL, progress_key TEXT,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supervision_events(
              event_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, agent_id TEXT,
              job_id TEXT, attempt_id TEXT, event_type TEXT NOT NULL,
              occurred_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS supervision_messages(
              message_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, job_id TEXT NOT NULL,
              attempt_id TEXT, kind TEXT NOT NULL, reason TEXT NOT NULL,
              sequence INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_supervision_message ON supervision_messages(owner_id, job_id, kind, sequence);
            CREATE TABLE IF NOT EXISTS budget_reservations(
              reservation_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, job_id TEXT NOT NULL,
              amount REAL NOT NULL CHECK(amount >= 0), state TEXT NOT NULL,
              attempt_id TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL
            );
            """)

    def _event(self, db, *, owner_id: str, event_type: SupervisionEvent, agent_id: str | None = None, job_id: str | None = None, attempt_id: str | None = None, payload: dict | None = None):
        db.execute("INSERT INTO supervision_events(event_id,owner_id,agent_id,job_id,attempt_id,event_type,occurred_at,payload) VALUES(?,?,?,?,?,?,?,?)", (str(uuid.uuid4()), owner_id, agent_id, job_id, attempt_id, event_type, datetime.now(timezone.utc).isoformat(), json.dumps(payload or {}, sort_keys=True)[:4000]))

    def observe(self, observation: AgentObservation) -> None:
        now = datetime.now(timezone.utc).isoformat()
        event = {
            AgentState.IDLE: SupervisionEvent.AGENT_IDLE,
            AgentState.RESULT_SUBMITTED: SupervisionEvent.AGENT_RESULT_SUBMITTED,
            AgentState.BLOCKED: SupervisionEvent.AGENT_BLOCKED,
            AgentState.CRASHED: SupervisionEvent.AGENT_CRASHED,
            AgentState.USAGE_EXHAUSTED: SupervisionEvent.AGENT_USAGE_EXHAUSTED,
            AgentState.PROGRESSING: SupervisionEvent.AGENT_PROGRESS,
        }.get(observation.state, SupervisionEvent.AGENT_HEARTBEAT)
        with self._connect() as db:
            db.execute("INSERT INTO agents(agent_id,owner_id,state,process_nonce,pid,job_id,attempt_id,provider,heartbeat_at,progress_key,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(agent_id) DO UPDATE SET owner_id=excluded.owner_id,state=excluded.state,process_nonce=excluded.process_nonce,pid=excluded.pid,job_id=excluded.job_id,attempt_id=excluded.attempt_id,provider=excluded.provider,heartbeat_at=excluded.heartbeat_at,progress_key=excluded.progress_key,updated_at=excluded.updated_at", (observation.agent_id, observation.owner_id, observation.state, observation.process_nonce, observation.pid, observation.job_id, observation.attempt_id, observation.provider, observation.heartbeat_at, observation.progress_key, now))
            self._event(db, owner_id=observation.owner_id, agent_id=observation.agent_id, job_id=observation.job_id, attempt_id=observation.attempt_id, event_type=event, payload={"process_nonce": observation.process_nonce, "progress_key": observation.progress_key})
            db.commit()

    def continuation(self, *, owner_id: str, agent_id: str, job_id: str, attempt_id: str | None, reason: str, sequence: int, kind: str = "CONTINUE_SAME_JOB") -> Continuation:
        if not reason or sequence < 1:
            raise SupervisionError("bounded continuation metadata is required")
        message_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{owner_id}:{job_id}:{agent_id}:{kind}:{sequence}"))
        with self._connect() as db:
            existing = db.execute("SELECT * FROM supervision_messages WHERE message_id=?", (message_id,)).fetchone()
            if existing is None:
                db.execute("INSERT INTO supervision_messages(message_id,owner_id,job_id,attempt_id,kind,reason,sequence) VALUES(?,?,?,?,?,?,?)", (message_id, owner_id, job_id, attempt_id, kind, reason[:1000], sequence))
                self._event(db, owner_id=owner_id, agent_id=agent_id, job_id=job_id, attempt_id=attempt_id, event_type=SupervisionEvent.CONTINUATION_SENT, payload={"sequence": sequence, "reason": reason[:1000]})
            db.commit()
        return Continuation(message_id, job_id, attempt_id, kind, reason[:1000], sequence)

    def supervise(self, *, owner_id: str, agent_id: str, now: datetime | None = None, idle_timeout: timedelta = timedelta(minutes=5)) -> list[Continuation]:
        now = now or datetime.now(timezone.utc)
        with self._connect() as db:
            row = db.execute("SELECT * FROM agents WHERE agent_id=? AND owner_id=?", (agent_id, owner_id)).fetchone()
        if row is None:
            raise SupervisionError("agent is not registered for owner")
        heartbeat = datetime.fromisoformat(row["heartbeat_at"])
        state = AgentState(row["state"])
        if state in {AgentState.RUNNING, AgentState.PROGRESSING, AgentState.WAITING} and now - heartbeat <= idle_timeout:
            return []
        if state == AgentState.RESULT_SUBMITTED:
            with self._connect() as db:
                self._event(db, owner_id=owner_id, agent_id=agent_id, job_id=row["job_id"], attempt_id=row["attempt_id"], event_type=SupervisionEvent.JOB_VERIFICATION_REQUIRED)
                db.commit()
            return []
        if state == AgentState.IDLE and row["job_id"]:
            return [self.continuation(owner_id=owner_id, agent_id=agent_id, job_id=row["job_id"], attempt_id=row["attempt_id"], reason="agent idle with unfinished bound job; resume same job", sequence=1)]
        if state == AgentState.IDLE:
            ready = self.substrate.eligible_jobs(owner_id=owner_id, limit=1)
            if ready:
                return [self.continuation(owner_id=owner_id, agent_id=agent_id, job_id=ready[0], attempt_id=None, reason="agent idle with authorized READY work; assign one bounded job", sequence=1, kind="START_NEXT_READY_JOB")]
        if state == AgentState.BLOCKED:
            return []
        return []

    def reconcile(self, *, owner_id: str | None = None) -> list[dict]:
        """Rebuild safe decisions from durable substrate state after supervisor restart."""
        self.substrate.recover_incomplete_jobs()
        with self._connect() as db:
            rows = db.execute("SELECT * FROM agents WHERE (? IS NULL OR owner_id=?)", (owner_id, owner_id)).fetchall()
        decisions = []
        for row in rows:
            decisions.append({"agent_id": row["agent_id"], "owner_id": row["owner_id"], "state": row["state"], "job_id": row["job_id"], "attempt_id": row["attempt_id"]})
        return decisions

    def deliver(self, handler, *, owner_id: str, limit: int = 100, max_attempts: int = 5) -> tuple[int, int, int]:
        if not 1 <= limit <= 1000 or not 1 <= max_attempts <= 20:
            raise ValueError("invalid delivery bounds")
        delivered = dead = retrying = 0
        with self._connect() as db:
            rows = db.execute("SELECT * FROM supervision_messages WHERE owner_id=? AND state IN ('pending','retrying') AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY sequence,message_id LIMIT ?", (owner_id, datetime.now(timezone.utc).isoformat(), limit)).fetchall()
            for row in rows:
                try:
                    handler({"message_id": row["message_id"], "owner_id": row["owner_id"], "job_id": row["job_id"], "attempt_id": row["attempt_id"], "kind": row["kind"], "reason": row["reason"], "sequence": row["sequence"]})
                except Exception:
                    attempts = row["attempts"] + 1
                    if attempts >= max_attempts:
                        db.execute("UPDATE supervision_messages SET state='dead_letter',attempts=? WHERE message_id=?", (attempts, row["message_id"]))
                        dead += 1
                    else:
                        db.execute("UPDATE supervision_messages SET state='retrying',attempts=?,next_attempt_at=? WHERE message_id=?", (attempts, (datetime.now(timezone.utc) + timedelta(seconds=min(3600, 2 ** attempts))).isoformat(), row["message_id"]))
                        retrying += 1
                    continue
                db.execute("UPDATE supervision_messages SET state='delivered',attempts=attempts+1 WHERE message_id=?", (row["message_id"],))
                delivered += 1
            db.commit()
        return delivered, dead, retrying

    def reserve_budget(self, *, owner_id: str, job_id: str, amount: float, ttl_seconds: int = 3600) -> str:
        if amount < 0 or ttl_seconds < 1:
            raise ValueError("invalid reservation")
        reservation_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        with self._connect() as db:
            db.execute("INSERT INTO budget_reservations(reservation_id,owner_id,job_id,amount,state,created_at,expires_at) VALUES(?,?,?,?,?,?,?)", (reservation_id, owner_id, job_id, amount, "reserved", now.isoformat(), (now + timedelta(seconds=ttl_seconds)).isoformat()))
            db.commit()
        return reservation_id

    def bind_budget(self, reservation_id: str, attempt_id: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE budget_reservations SET attempt_id=? WHERE reservation_id=? AND state='reserved'", (attempt_id, reservation_id))
            db.commit()

    def release_budget(self, reservation_id: str) -> bool:
        with self._connect() as db:
            changed = db.execute("UPDATE budget_reservations SET state='released' WHERE reservation_id=? AND state='reserved'", (reservation_id,)).rowcount
            db.commit()
        return bool(changed)

    def reconcile_budget(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(timezone.utc)
        with self._connect() as db:
            count = db.execute("UPDATE budget_reservations SET state='expired' WHERE state='reserved' AND expires_at<=?", (now.isoformat(),)).rowcount
            db.commit()
        return count


def validate_cost(*, reserved: float, reported: float, prompt_tokens: int, completion_tokens: int, unit_price: float | None = None) -> bool:
    if min(reserved, reported, prompt_tokens, completion_tokens) < 0 or reported > reserved:
        return False
    if unit_price is not None and unit_price < 0:
        return False
    if unit_price is not None and (prompt_tokens + completion_tokens) * unit_price > reserved * 1.000001:
        return False
    return True
