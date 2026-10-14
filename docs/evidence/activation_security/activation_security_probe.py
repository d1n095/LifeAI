#!/usr/bin/env python3
"""Pure local reference probes for the MainAI activation-security contract.

No imports from production, network calls, provider calls, GitHub mutations, agents, or
database writes. Cursor should translate each vector into production service/API tests.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone


NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


def push_allowed(grant: dict, context: dict, *, now: datetime = NOW) -> bool:
    if grant.get("status") != "active":
        return False
    if datetime.fromisoformat(grant["expires_at"]) <= now:
        return False
    keys = (
        "owner_id", "goal_id", "task_id", "job_id", "worktree_id",
        "execution_envelope_id", "preflight_receipt_id", "repository_identity",
        "branch", "base_branch", "base_sha", "local_commit_sha",
    )
    return all(grant.get(key) == context.get(key) for key in keys)


def consume(grant: dict, context: dict) -> bool:
    if not push_allowed(grant, context):
        return False
    grant["status"] = "consumed"
    grant["consumed_by_job_id"] = context["job_id"]
    return True


def receipt_live(receipt: dict, state: dict, *, now: datetime = NOW) -> bool:
    if receipt.get("verdict") != "go" or receipt.get("unknown_required_facts"):
        return False
    if receipt.get("invalidated_at") is not None:
        return False
    if datetime.fromisoformat(receipt["expires_at"]) <= now:
        return False
    keys = (
        "activation_sha", "base_sha", "execution_envelope_id",
        "provider_spend_authorization_id", "runtime_config_digest",
        "runtime_token_attestation_digest", "ruleset_attestation_digest",
    )
    return all(receipt.get(key) == state.get(key) for key in keys)


def ci_result(required: set[tuple[str, str]], runs: list[dict], *, expected_sha: str) -> str:
    observed: dict[tuple[str, str], dict] = {}
    for run in runs:
        if run.get("head_sha") != expected_sha:
            continue
        observed[(run.get("name"), run.get("app_slug"))] = run
    if not required <= observed.keys():
        return "waiting"
    for key in required:
        run = observed[key]
        if run.get("status") != "completed":
            return "waiting"
        if run.get("conclusion") not in {"success", "neutral", "skipped"}:
            return "failed"
    return "passed"


def remote_effect_allowed(goal: dict, effect: str) -> bool:
    envelope = goal.get("active_envelope")
    if not envelope or envelope.get("status") != "active":
        return False
    caps = set(envelope.get("capabilities", []))
    required = {"push": {"path_a", "repo_edit", "push"}, "open_pr": {"path_a", "open_pr"}}
    return required[effect] <= caps


def base_grant() -> tuple[dict, dict]:
    context = {
        "owner_id": "owner-a", "goal_id": "goal-a", "task_id": "task-a",
        "job_id": "job-2", "worktree_id": "wt-2", "execution_envelope_id": "env-3",
        "preflight_receipt_id": "receipt-4", "repository_identity": "d1n095/LifeAI",
        "branch": "mainai/task-a", "base_branch": "mainai/integration",
        "base_sha": "a" * 40, "local_commit_sha": "b" * 40,
    }
    grant = {**context, "status": "active", "expires_at": (NOW + timedelta(minutes=5)).isoformat()}
    return grant, context


def run() -> None:
    grant, context = base_grant()

    # 1. Replay on another retry/job.
    retry = {**context, "job_id": "job-3", "worktree_id": "wt-3"}
    assert not push_allowed(grant, retry)

    # 2. Local commit changed after approval.
    amended = {**context, "local_commit_sha": "c" * 40}
    assert not push_allowed(grant, amended)

    # 3. Base branch moved after preflight/grant.
    moved_base = {**context, "base_sha": "d" * 40}
    assert not push_allowed(grant, moved_base)

    # 4. Legacy goal cannot remotely write merely because the global switch is on.
    legacy = {"github_write_enabled": True, "active_envelope": None}
    assert not remote_effect_allowed(legacy, "push")

    # 5. Missing required CI check remains waiting.
    required = {("Backend", "github-actions"), ("Frontend — npm audit", "github-actions")}
    runs = [{"name": "Backend", "app_slug": "github-actions", "head_sha": "b" * 40,
             "status": "completed", "conclusion": "success"}]
    assert ci_result(required, runs, expected_sha="b" * 40) == "waiting"

    # 6. Late-arriving required failure makes the exact required set fail.
    runs.append({"name": "Frontend — npm audit", "app_slug": "github-actions", "head_sha": "b" * 40,
                 "status": "completed", "conclusion": "failure"})
    assert ci_result(required, runs, expected_sha="b" * 40) == "failed"

    # 7. Wrong check SHA cannot substitute; PR base/head identity is an additional production assertion.
    wrong_sha = [{"name": name, "app_slug": app, "head_sha": "e" * 40,
                  "status": "completed", "conclusion": "success"} for name, app in required]
    assert ci_result(required, wrong_sha, expected_sha="b" * 40) == "waiting"

    # 8. Expired and consumed grants deny.
    expired = {**grant, "expires_at": (NOW - timedelta(seconds=1)).isoformat()}
    assert not push_allowed(expired, context)
    one_shot = deepcopy(grant)
    assert consume(one_shot, context)
    assert not consume(one_shot, context)

    # 9. Owner/worktree/branch mismatches deny independently.
    for key, value in (("owner_id", "owner-b"), ("worktree_id", "wt-other"), ("branch", "other")):
        changed = {**context, key: value}
        assert not push_allowed(grant, changed), key

    # 10. Stale or incomplete receipt denies.
    receipt = {
        "verdict": "go", "unknown_required_facts": [], "invalidated_at": None,
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "activation_sha": "1" * 40, "base_sha": "a" * 40,
        "execution_envelope_id": "env-3", "provider_spend_authorization_id": "spend-5",
        "runtime_config_digest": "cfg", "runtime_token_attestation_digest": "token",
        "ruleset_attestation_digest": "rules",
    }
    state = {key: receipt[key] for key in (
        "activation_sha", "base_sha", "execution_envelope_id",
        "provider_spend_authorization_id", "runtime_config_digest",
        "runtime_token_attestation_digest", "ruleset_attestation_digest",
    )}
    assert receipt_live(receipt, state)
    assert not receipt_live(receipt, {**state, "base_sha": "f" * 40})
    assert not receipt_live({**receipt, "unknown_required_facts": ["token_permissions"]}, state)

    print("activation-security reference probes: 10/10 PASS")


if __name__ == "__main__":
    run()
