"""EXAMINER PROBE (not part of the candidate) — adversarial checks on #300 read-only intake.

Run-only; never committed to the frozen branch. Probes the security claims independently:
read-only transport, rate-limit/forbidden → BLOCKED, forged/replay/stale/impersonation
rejection, duplicate-obligation idempotency, no delivery/publication claims.
"""

from __future__ import annotations

import json

from app.agent_coordination.github_intake import (
    MISSING_FORBIDDEN,
    MISSING_MESSAGING,
    MISSING_PUBLICATION,
    MISSING_RATE_LIMIT,
    GitHubDevelopmentIntake,
    GitHubReadClient,
    passing_required_runs,
)


class _Resp:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    @property
    def content(self):
        return b"x"

    def json(self):
        return self._body


def _owner():
    return "00000000-0000-0000-0000-000000000300"


def test_client_is_get_only_and_sends_no_token_when_public():
    calls = []

    def transport(method, url, headers=None):
        calls.append((method, url, headers))
        return _Resp(200, {"ok": True})

    client = GitHubReadClient(transport=transport)
    assert client.access_mode == "public_unauthenticated"
    client.get("/repos/x/y/pulls/1")
    # Only GET is ever issued, and no Authorization header without a token.
    assert calls and all(m == "GET" for m, _u, _h in calls)
    assert all("Authorization" not in (h or {}) for _m, _u, h in calls)


def test_rate_limit_and_forbidden_become_blocked_with_named_capability():
    def rate_limited(method, url, headers=None):
        return _Resp(403, {"message": "rate"}, {"X-RateLimit-Remaining": "0"})

    intake = GitHubDevelopmentIntake(owner_id=_owner())
    out = intake.refresh(GitHubReadClient(transport=rate_limited), numbers=[299])
    assert out["outcome"] == "blocked"
    assert out["missing_capability"] == MISSING_RATE_LIMIT
    assert intake.state["candidates"] == {}  # nothing ingested on a blocked read

    def forbidden(method, url, headers=None):
        return _Resp(403, {"message": "no"}, {"X-RateLimit-Remaining": "42"})

    intake2 = GitHubDevelopmentIntake(owner_id=_owner())
    out2 = intake2.refresh(GitHubReadClient(transport=forbidden), numbers=[299])
    assert out2["outcome"] == "blocked"
    assert out2["missing_capability"] == MISSING_FORBIDDEN


def test_forged_head_sha_is_rejected():
    intake = GitHubDevelopmentIntake(owner_id=_owner())
    out = intake.ingest_pull_request({"number": 1, "head_sha": "not-a-sha", "head_branch": "cursor/x"})
    assert out["outcome"] == "forged_rejected"
    assert intake.state["candidates"] == {}


def test_duplicate_pr_ingest_is_idempotent_no_second_examiner_obligation():
    sha = "1105e331a444c47d80d10265c4947eb1b48aeb72"
    checks, workflows = passing_required_runs(sha)
    obs = {
        "number": 299, "title": "t", "draft": False, "merged": False, "state": "open",
        "head_sha": sha, "base_sha": None, "head_branch": "cursor/x", "base_branch": "main",
        "ancestry": [sha], "reviews": [], "check_runs": checks, "workflow_runs": workflows,
    }
    intake = GitHubDevelopmentIntake(owner_id=_owner())
    first = intake.ingest_pull_request(obs)
    second = intake.ingest_pull_request(obs)
    assert first["examiner_assignment_id"] == second["examiner_assignment_id"]
    assert second["duplicate_examiner"] is True  # not a new conflicting obligation


def test_webhook_replay_and_stale_and_impersonation_are_rejected():
    sha = "1105e331a444c47d80d10265c4947eb1b48aeb72"
    checks, workflows = passing_required_runs(sha)
    intake = GitHubDevelopmentIntake(owner_id=_owner())
    intake.ingest_pull_request({
        "number": 299, "title": "t", "draft": False, "merged": False, "state": "open",
        "head_sha": sha, "base_sha": None, "head_branch": "cursor/x", "base_branch": "main",
        "ancestry": [sha], "reviews": [], "check_runs": checks, "workflow_runs": workflows,
    })
    # Replay: same delivery_id twice.
    ev = {"delivery_id": "d1", "pr": 299, "sha": sha}
    assert intake.ingest_event(ev)["outcome"] in ("applied", "stale_rejected")
    assert intake.ingest_event(ev)["outcome"] == "replay_ignored"
    # Stale/foreign sha for the PR.
    foreign = "0" * 40
    out = intake.ingest_event({"delivery_id": "d2", "pr": 299, "sha": foreign})
    assert out["outcome"] == "stale_rejected"
    # Required-check impersonation: app_slug not github-actions.
    imp = intake.ingest_event({
        "delivery_id": "d3", "pr": 299, "sha": sha, "app_slug": "evil-app",
        "check_runs": [{"name": "x", "app": {"slug": "evil-app"}}],
    })
    assert imp["outcome"] == "forged_rejected"


def test_never_claims_delivery_or_publication():
    intake = GitHubDevelopmentIntake(owner_id=_owner())
    d = intake.claim_agent_delivery(agent_identity="claude")
    assert d["outcome"] == "blocked" and d["delivery_claimed"] is False and d["missing_capability"] == MISSING_MESSAGING
    p = intake.claim_github_publication()
    assert p["outcome"] == "blocked" and p["write_attempted"] is False and p["missing_capability"] == MISSING_PUBLICATION
    assert intake.view()["write_attempted"] is False
    assert intake.view()["delivery_claimed"] is False
    assert intake.view()["first_cycle_go"] is False
