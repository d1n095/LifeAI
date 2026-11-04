"""Harness-local Founder grant store. This is the acceptance contract.

Production Codex interfaces are bound separately; until they exist, production compatibility
is BLOCKED rather than invented.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

GrantKind = Literal["push", "draft_pr"]


class GrantError(Exception):
    pass


@dataclass
class Grant:
    id: str
    kind: GrantKind
    owner_id: str
    goal_id: str
    task_id: str
    job_id: str
    worktree_id: str
    repository: str
    branch: str
    base_sha: str
    commit_sha: str
    expires_at: datetime
    consumed: bool = False
    cancelled: bool = False
    consume_result: str | None = None  # success | unknown_network | remote_mismatch
    consumed_at: datetime | None = None


@dataclass
class GrantStore:
    """In-process one-use grants. Duplicate consume is serialized on `_lock`."""

    now_fn: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    _grants: dict[str, Grant] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def issue(
        self,
        *,
        kind: GrantKind,
        owner_id: str,
        goal_id: str,
        task_id: str,
        job_id: str,
        worktree_id: str,
        repository: str,
        branch: str,
        base_sha: str,
        commit_sha: str,
        ttl_seconds: int = 60,
    ) -> Grant:
        grant = Grant(
            id=str(uuid.uuid4()),
            kind=kind,
            owner_id=owner_id,
            goal_id=goal_id,
            task_id=task_id,
            job_id=job_id,
            worktree_id=worktree_id,
            repository=repository,
            branch=branch,
            base_sha=base_sha,
            commit_sha=commit_sha,
            expires_at=self.now_fn() + timedelta(seconds=ttl_seconds),
        )
        with self._lock:
            self._grants[grant.id] = grant
        return grant

    def cancel(self, grant_id: str) -> None:
        with self._lock:
            grant = self._require(grant_id)
            grant.cancelled = True

    def consume(
        self,
        grant_id: str,
        *,
        kind: GrantKind,
        owner_id: str,
        goal_id: str,
        task_id: str,
        job_id: str,
        worktree_id: str,
        repository: str,
        branch: str,
        base_sha: str,
        commit_sha: str,
        remote_tip: str | None = None,
        network_outcome: str = "success",
    ) -> Grant:
        with self._lock:
            grant = self._require(grant_id)
            self._assert_binding(
                grant,
                kind=kind,
                owner_id=owner_id,
                goal_id=goal_id,
                task_id=task_id,
                job_id=job_id,
                worktree_id=worktree_id,
                repository=repository,
                branch=branch,
                base_sha=base_sha,
                commit_sha=commit_sha,
            )
            if grant.cancelled:
                raise GrantError("grant cancelled")
            if grant.consumed:
                raise GrantError("grant already consumed")
            if self.now_fn() >= grant.expires_at:
                raise GrantError("grant expired")
            if network_outcome == "unknown":
                grant.consumed = True
                grant.consume_result = "unknown_network"
                grant.consumed_at = self.now_fn()
                raise GrantError("unknown network outcome; grant consumed pending remote-tip reconciliation")
            if remote_tip is not None and remote_tip != commit_sha:
                grant.consumed = True
                grant.consume_result = "remote_mismatch"
                grant.consumed_at = self.now_fn()
                raise GrantError(
                    f"remote tip {remote_tip} does not match granted SHA {commit_sha}"
                )
            grant.consumed = True
            grant.consume_result = "success"
            grant.consumed_at = self.now_fn()
            return grant

    def reconcile_unknown(self, grant_id: str, *, remote_tip: str) -> str:
        """After unknown network: exact remote tip either confirms or refuses revival."""
        with self._lock:
            grant = self._require(grant_id)
            if grant.consume_result != "unknown_network":
                raise GrantError("grant is not awaiting remote-tip reconciliation")
            if remote_tip == grant.commit_sha:
                grant.consume_result = "success"
                return "confirmed"
            raise GrantError("remote tip does not match granted SHA; grant stays consumed and is not revived")

    def get(self, grant_id: str) -> Grant:
        with self._lock:
            return self._require(grant_id)

    def _require(self, grant_id: str) -> Grant:
        if grant_id not in self._grants:
            raise GrantError("unknown grant")
        return self._grants[grant_id]

    def _assert_binding(self, grant: Grant, **expected) -> None:
        for field_name, value in expected.items():
            if getattr(grant, field_name) != value:
                raise GrantError(f"grant {field_name} mismatch: {getattr(grant, field_name)!r} != {value!r}")
