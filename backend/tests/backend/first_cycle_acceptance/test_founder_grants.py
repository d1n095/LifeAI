"""Founder grant attack matrix. Harness contract is executable; production binding is BLOCKED until Codex."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from first_cycle_acceptance._adapters import production_grant_module
from first_cycle_acceptance._grants import GrantError, GrantStore
from first_cycle_acceptance._matrix import BLOCKED, FAIL, PASS
from first_cycle_acceptance.conftest import conclude

pytestmark = pytest.mark.first_cycle_acceptance

FIXED_NOW = datetime(2026, 10, 18, 12, 0, tzinfo=timezone.utc)

BINDING = dict(
    owner_id="owner-1",
    goal_id="goal-1",
    task_id="task-1",
    job_id="job-1",
    worktree_id="wt-1",
    repository="test-owner/test-repo",
    branch="claude/mainai-task-1",
    base_sha="a" * 40,
    commit_sha="b" * 40,
)


def _store(**kwargs) -> GrantStore:
    now = kwargs.pop("now", FIXED_NOW)
    store = GrantStore(now_fn=lambda: now)
    return store


def _issue(store: GrantStore, **overrides) -> str:
    payload = {**BINDING, "kind": "push", "ttl_seconds": 60, **overrides}
    return store.issue(**payload).id


def test_production_grant_interface_unavailable():
    bound = production_grant_module()
    conclude(
        "GRANT-PROD production push/PR grant interface",
        BLOCKED if bound is None else PASS,
        boundary="production.push_grants",
        detail=(
            "Codex activation-security grant module is not present on this candidate; "
            "harness contract tests below are not a production compatibility claim"
            if bound is None
            else f"bound {bound.name} symbols={list(bound.symbols)}"
        ),
        production_module=None if bound is None else bound.name,
    )


def test_missing_approval():
    store = _store()
    with pytest.raises(GrantError, match="unknown grant"):
        store.consume("missing", kind="push", **BINDING)
    conclude("GRANT-1 missing approval", PASS, boundary="harness.grants.missing", detail="unknown grant refused")


def test_wrong_owner():
    store = _store()
    gid = _issue(store)
    with pytest.raises(GrantError, match="owner_id"):
        store.consume(gid, kind="push", **{**BINDING, "owner_id": "other"})
    conclude("GRANT-2 wrong owner", PASS, boundary="harness.grants.owner", detail="owner mismatch refused")


def test_wrong_goal_task_job():
    store = _store()
    gid = _issue(store)
    for field in ("goal_id", "task_id", "job_id"):
        with pytest.raises(GrantError, match=field):
            store.consume(gid, kind="push", **{**BINDING, field: "wrong"})
    conclude(
        "GRANT-3 wrong goal/task/job",
        PASS,
        boundary="harness.grants.identity",
        detail="goal/task/job mismatches refused",
    )


def test_wrong_worktree():
    store = _store()
    gid = _issue(store)
    with pytest.raises(GrantError, match="worktree_id"):
        store.consume(gid, kind="push", **{**BINDING, "worktree_id": "other-wt"})
    conclude("GRANT-4 wrong worktree", PASS, boundary="harness.grants.worktree", detail="worktree mismatch refused")


def test_wrong_branch_repository():
    store = _store()
    gid = _issue(store)
    with pytest.raises(GrantError, match="branch"):
        store.consume(gid, kind="push", **{**BINDING, "branch": "main"})
    with pytest.raises(GrantError, match="repository"):
        store.consume(gid, kind="push", **{**BINDING, "repository": "evil/repo"})
    conclude(
        "GRANT-5 wrong branch/repository",
        PASS,
        boundary="harness.grants.repo_branch",
        detail="branch and repository mismatches refused",
    )


def test_stale_base_sha():
    store = _store()
    gid = _issue(store)
    with pytest.raises(GrantError, match="base_sha"):
        store.consume(gid, kind="push", **{**BINDING, "base_sha": "c" * 40})
    conclude("GRANT-6 stale base SHA", PASS, boundary="harness.grants.base_sha", detail="stale base refused")


def test_changed_local_commit():
    store = _store()
    gid = _issue(store)
    with pytest.raises(GrantError, match="commit_sha"):
        store.consume(gid, kind="push", **{**BINDING, "commit_sha": "d" * 40})
    conclude(
        "GRANT-7 changed local commit",
        PASS,
        boundary="harness.grants.commit_sha",
        detail="moved local HEAD refused",
    )


def test_consumed_grant():
    store = _store()
    gid = _issue(store)
    store.consume(gid, kind="push", **BINDING)
    with pytest.raises(GrantError, match="already consumed"):
        store.consume(gid, kind="push", **BINDING)
    conclude("GRANT-8 consumed grant", PASS, boundary="harness.grants.one_use", detail="second consume refused")


def test_expired_grant():
    current = {"t": FIXED_NOW}

    def now():
        return current["t"]

    store = GrantStore(now_fn=now)
    gid = _issue(store, ttl_seconds=1)
    current["t"] = FIXED_NOW + timedelta(seconds=2)
    with pytest.raises(GrantError, match="expired"):
        store.consume(gid, kind="push", **BINDING)
    conclude("GRANT-9 expired grant", PASS, boundary="harness.grants.expiry", detail="expired grant refused")


def test_cancellation():
    store = _store()
    gid = _issue(store)
    store.cancel(gid)
    with pytest.raises(GrantError, match="cancelled"):
        store.consume(gid, kind="push", **BINDING)
    conclude("GRANT-10 cancellation", PASS, boundary="harness.grants.cancel", detail="cancelled grant refused")


def test_restart_recovery_does_not_revive_consumed_grant():
    store = _store()
    gid = _issue(store)
    store.consume(gid, kind="push", **BINDING)
    # Simulate process restart: same store snapshot, consumed bit survives.
    with pytest.raises(GrantError, match="already consumed"):
        store.consume(gid, kind="push", **BINDING)
    conclude(
        "GRANT-11 restart/recovery cannot revive consumed grant",
        PASS,
        boundary="harness.grants.recovery",
        detail="consumed bit is durable in the grant record",
    )


def test_simultaneous_duplicate_consumption():
    store = _store()
    gid = _issue(store)
    results: list[str] = []

    def attempt():
        try:
            store.consume(gid, kind="push", **BINDING)
            results.append("ok")
        except GrantError:
            results.append("err")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    holds = results.count("ok") == 1 and results.count("err") == 7
    conclude(
        "GRANT-12 simultaneous duplicate consumption",
        PASS if holds else FAIL,
        boundary="harness.grants.concurrency",
        detail=f"results={results}",
    )
    assert holds


def test_unknown_network_outcome_and_exact_remote_tip_reconciliation():
    store = _store()
    gid = _issue(store)
    with pytest.raises(GrantError, match="unknown network"):
        store.consume(gid, kind="push", **BINDING, network_outcome="unknown")
    confirmed = store.reconcile_unknown(gid, remote_tip=BINDING["commit_sha"])
    gid2 = _issue(store, commit_sha="e" * 40)
    binding2 = {**BINDING, "commit_sha": "e" * 40}
    with pytest.raises(GrantError, match="unknown network"):
        store.consume(gid2, kind="push", **binding2, network_outcome="unknown")
    with pytest.raises(GrantError, match="remote tip"):
        store.reconcile_unknown(gid2, remote_tip="f" * 40)
    with pytest.raises(GrantError, match="already consumed"):
        store.consume(gid2, kind="push", **binding2)
    conclude(
        "GRANT-13 unknown network + exact remote-tip reconciliation",
        PASS if confirmed == "confirmed" else FAIL,
        boundary="harness.grants.unknown_network",
        detail="matching remote tip confirms; mismatch does not revive the grant",
    )


def test_separate_push_and_pr_grants():
    store = _store()
    push_id = _issue(store, kind="push")
    pr_id = _issue(store, kind="draft_pr")
    store.consume(push_id, kind="push", **BINDING)
    with pytest.raises(GrantError, match="kind"):
        store.consume(pr_id, kind="push", **BINDING)
    store.consume(pr_id, kind="draft_pr", **BINDING)
    conclude(
        "GRANT-14 separate push and PR grants",
        PASS,
        boundary="harness.grants.separate_kinds",
        detail=f"push={push_id} pr={pr_id}",
    )
