#!/usr/bin/env python3
"""Network-free reference oracle for the MainAI transport-security contract.

This is specification evidence, not production enforcement. It models the minimum
bindings and monotonic state transitions required of the implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone


UTC = timezone.utc
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class Denied(RuntimeError):
    pass


@dataclass(frozen=True)
class PushGrant:
    grant_id: str
    owner: str
    goal: str
    task: str
    job: str
    attempt: str
    worktree: str
    repo: str
    branch: str
    base_sha: str
    expected_remote_sha: str
    local_sha: str
    expires_at: datetime
    consumed_at: datetime | None = None
    cancelled_at: datetime | None = None


@dataclass(frozen=True)
class Request:
    owner: str
    goal: str
    task: str
    job: str
    attempt: str
    worktree: str
    repo: str
    branch: str
    base_sha: str
    expected_remote_sha: str
    local_sha: str


def authorize(grant: PushGrant, request: Request, *, now: datetime) -> PushGrant:
    if grant.cancelled_at is not None:
        raise Denied("cancelled")
    if grant.consumed_at is not None:
        raise Denied("consumed")
    if now >= grant.expires_at:
        raise Denied("expired")
    for field in (
        "owner", "goal", "task", "job", "attempt", "worktree", "repo",
        "branch", "base_sha", "expected_remote_sha", "local_sha",
    ):
        if getattr(grant, field) != getattr(request, field):
            raise Denied(f"binding:{field}")
    return replace(grant, consumed_at=now)


def clean_git_environment(source: dict[str, str]) -> dict[str, str]:
    allowed = {"LANG", "LC_ALL", "PATH", "TMPDIR"}
    result = {key: value for key, value in source.items() if key in allowed}
    result.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GCM_INTERACTIVE": "Never",
    })
    return result


BASE = PushGrant(
    grant_id="grant-1", owner="founder", goal="goal-1", task="task-1",
    job="job-1", attempt="attempt-1", worktree="/srv/wt/task-1",
    repo="d1n095/LifeAI", branch="mainai/task-1", base_sha="a" * 40,
    expected_remote_sha="a" * 40, local_sha="b" * 40,
    expires_at=NOW + timedelta(minutes=5),
)
REQUEST = Request(**{field: getattr(BASE, field) for field in Request.__dataclass_fields__})


def denied(callable_) -> None:
    try:
        callable_()
    except Denied:
        return
    raise AssertionError("request unexpectedly authorized")


def expired_credential_reuse() -> None:
    denied(lambda: authorize(replace(BASE, expires_at=NOW), REQUEST, now=NOW))


def direct_push_bypass() -> None:
    env = clean_git_environment({
        "PATH": "/usr/bin", "GITHUB_TOKEN": "secret", "GH_TOKEN": "secret",
        "GIT_ASKPASS": "/tmp/leak", "SSH_AUTH_SOCK": "/tmp/sock",
    })
    assert "GITHUB_TOKEN" not in env and "GH_TOKEN" not in env
    assert "GIT_ASKPASS" not in env and "SSH_AUTH_SOCK" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


def remote_url_substitution() -> None:
    denied(lambda: authorize(BASE, replace(REQUEST, repo="attacker/other"), now=NOW))
    denied(lambda: authorize(BASE, replace(REQUEST, branch="main"), now=NOW))


def second_push() -> None:
    consumed = authorize(BASE, REQUEST, now=NOW)
    denied(lambda: authorize(consumed, REQUEST, now=NOW + timedelta(seconds=1)))


def retry_after_consumption() -> None:
    consumed = authorize(BASE, REQUEST, now=NOW)
    retry = replace(REQUEST, attempt="attempt-2", job="job-2")
    denied(lambda: authorize(consumed, retry, now=NOW + timedelta(seconds=1)))


def credential_helper_leakage() -> None:
    env = clean_git_environment({
        "PATH": "/usr/bin", "GIT_CONFIG_GLOBAL": "/tmp/host.gitconfig",
        "GIT_CONFIG_SYSTEM": "/tmp/system.gitconfig", "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "evil",
    })
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "GIT_CONFIG_SYSTEM" not in env and "GIT_CONFIG_COUNT" not in env
    assert not any(key.startswith("GIT_CONFIG_KEY_") for key in env)


def restart_recovery() -> None:
    durable = authorize(BASE, REQUEST, now=NOW)
    reconstructed_after_restart = replace(durable)
    denied(lambda: authorize(reconstructed_after_restart, REQUEST, now=NOW + timedelta(seconds=2)))


def unrelated_legacy_goal_write() -> None:
    unrelated = replace(REQUEST, goal="legacy-goal", task="legacy-task")
    denied(lambda: authorize(BASE, unrelated, now=NOW))


PROBES = (
    expired_credential_reuse,
    direct_push_bypass,
    remote_url_substitution,
    second_push,
    retry_after_consumption,
    credential_helper_leakage,
    restart_recovery,
    unrelated_legacy_goal_write,
)


if __name__ == "__main__":
    for probe in PROBES:
        probe()
    print(f"{len(PROBES)}/{len(PROBES)} transport-security reference probes passed")
