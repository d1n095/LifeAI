"""Read-only GitHub development intake into the durable coordination ledger."""

from __future__ import annotations

import httpx

from app.agent_coordination.durable import DurableCoordinationService
from app.agent_coordination.github_intake import (
    DEFAULT_PUBLIC_REPO,
    GitHubDevelopmentIntake,
    GitHubReadClient,
    GitHubReadError,
    github_check_run,
    passing_required_runs,
)

OWNER = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
HEAD = "1105e331a444c47d80d10265c4947eb1b48aeb72"
OLD = "20bfd1d6bd3c31a54d4ae25e975375096e084829"
PARENT = "76f2ad6818f3faab0603ceff03a06bd4c3842d43"


def _pr(*, sha=HEAD, draft=False, checks=None, workflows=None, reviews=None):
    if checks is None:
        checks, workflows = passing_required_runs(sha)
    return {
        "number": 299,
        "title": "Accept MainAI agent-coordination handover",
        "draft": draft,
        "merged": False,
        "state": "open",
        "html_url": "https://github.com/d1n095/LifeAI/pull/299",
        "head_sha": sha,
        "base_sha": PARENT,
        "head_branch": "cursor/durable-coordination-acceptance-91c1",
        "base_branch": "cursor/durable-agent-coordination-91c1",
        "updated_at": "2026-10-22T10:12:32Z",
        "ancestry": [OLD, PARENT, sha],
        "reviews": reviews or [],
        "check_runs": checks,
        "workflow_runs": workflows,
    }


def _running_checks(sha: str):
    checks, workflows = passing_required_runs(sha)
    for row in checks:
        if row["name"] == "Backend — unit/integration tests":
            row["status"] = "in_progress"
            row["conclusion"] = None
    return checks, workflows


def test_published_candidate_creates_examiner_without_duplicate():
    intake = GitHubDevelopmentIntake(owner_id=OWNER)
    first = intake.ingest_pull_request(_pr())
    second = intake.ingest_pull_request(_pr())
    assert first["reviewable"] is True
    assert first["freeze_allowed"] is True
    assert first["examiner_assignment_id"]
    assert second["duplicate_examiner"] is True
    assert second["examiner_assignment_id"] == first["examiner_assignment_id"]
    builders = [row for row in intake.service.founder_view()["obligations"] if row["workstream"] == "github-builder"]
    examiners = [row for row in intake.service.founder_view()["obligations"] if row["workstream"] == "github-examiner"]
    assert len(builders) == 1
    assert len(examiners) == 1
    assert intake.claim_agent_delivery(agent_identity="claude")["delivery_claimed"] is False
    assert intake.claim_github_publication()["missing_capability"] == "codex_github_publication_authority"


def test_running_or_failed_required_check_blocks_freeze_on_that_sha_only():
    running_checks, workflows = _running_checks(HEAD)
    intake = GitHubDevelopmentIntake(owner_id=OWNER)
    current = intake.ingest_pull_request(_pr(checks=running_checks, workflows=workflows))
    assert current["freeze_allowed"] is False
    assert "required_checks_incomplete" in current["freeze_blockers"]

    failed_old, old_workflows = passing_required_runs(OLD, base_id=5000)
    for row in failed_old:
        if row["name"] == "Frontend — npm audit":
            row["conclusion"] = "failure"
    stale = intake.ingest_event(
        {
            "delivery_id": "obsolete-fail",
            "pr": 299,
            "sha": OLD,
            "conclusion": "failure",
            "check_runs": failed_old,
            "workflow_runs": old_workflows,
        }
    )
    assert stale["outcome"] == "stale_rejected"
    assert stale["kind"] == "obsolete_sha"
    assert intake.state["candidates"]["299"]["ci"]["freeze_allowed"] is False

    green_checks, green_workflows = passing_required_runs(HEAD, base_id=8000)
    green = intake.ingest_event(
        {
            "delivery_id": "head-green",
            "pr": 299,
            "sha": HEAD,
            "check_runs": green_checks,
            "workflow_runs": green_workflows,
        }
    )
    assert green["freeze_allowed"] is True
    late_old = intake.ingest_event(
        {
            "delivery_id": "late-old-fail",
            "pr": 299,
            "sha": OLD,
            "conclusion": "failure",
            "check_runs": failed_old,
            "workflow_runs": old_workflows,
        }
    )
    assert late_old["outcome"] == "stale_rejected"
    assert intake.state["candidates"]["299"]["head_sha"] == HEAD
    assert intake.state["candidates"]["299"]["ci"]["freeze_allowed"] is True


def test_duplicate_webhook_and_out_of_order_ack():
    intake = GitHubDevelopmentIntake(owner_id=OWNER)
    intake.ingest_pull_request(_pr())
    first = intake.ingest_event({"delivery_id": "dup-1", "pr": 299, "sha": HEAD})
    second = intake.ingest_event({"delivery_id": "dup-1", "pr": 299, "sha": HEAD})
    assert first["outcome"] == "applied"
    assert second["outcome"] == "replay_ignored"
    delayed = intake.ingest_event({"delivery_id": "ack-old", "pr": 299, "sha": OLD})
    assert delayed["outcome"] == "stale_rejected"


def test_superseded_head_does_not_keep_old_builder_active():
    intake = GitHubDevelopmentIntake(owner_id=OWNER)
    old_checks, old_wf = passing_required_runs(OLD, base_id=2000)
    first = intake.ingest_pull_request(_pr(sha=OLD, checks=old_checks, workflows=old_wf))
    new = intake.ingest_pull_request(_pr(sha=HEAD))
    view = intake.service.founder_view()
    builders = [row for row in view["obligations"] if row["workstream"] == "github-builder"]
    examiners = [row for row in view["obligations"] if row["workstream"] == "github-examiner"]
    assert len(builders) == 1
    assert len(examiners) == 1
    assert new["builder_assignment_id"] == first["builder_assignment_id"]
    assert new["head_sha"] == HEAD
    assert intake.state["candidates"]["299"]["head_sha"] == HEAD
    stale = intake.ingest_event({"delivery_id": "old-head", "pr": 299, "sha": OLD, "conclusion": "failure"})
    assert stale["outcome"] == "stale_rejected"
    assert intake.state["candidates"]["299"]["ci"]["freeze_allowed"] is True


def test_forged_sha_and_impersonated_check_are_rejected():
    intake = GitHubDevelopmentIntake(owner_id=OWNER)
    intake.ingest_pull_request(_pr())
    forged = intake.ingest_event({"delivery_id": "bad-sha", "pr": 299, "sha": "not-a-sha"})
    assert forged["outcome"] == "forged_rejected"
    impersonated = github_check_run(
        name="All required checks passed",
        sha=HEAD,
        event="pull_request",
        conclusion="success",
        run_id=1,
        suite_id=1,
        app_slug="imposter",
        app_id=1,
    )
    spoof = intake.ingest_event(
        {
            "delivery_id": "spoof",
            "pr": 299,
            "sha": HEAD,
            "app_slug": "imposter",
            "check_runs": [impersonated],
        }
    )
    assert spoof["outcome"] == "forged_rejected"


def test_rate_limit_and_unavailable_api_are_blocked():
    intake = GitHubDevelopmentIntake(owner_id=OWNER)

    def rate_limited(method, url, headers):
        return httpx.Response(429, json={"message": "API rate limit exceeded"}, headers={"X-RateLimit-Remaining": "0"})

    limited = GitHubReadClient(repo=DEFAULT_PUBLIC_REPO, transport=rate_limited)
    blocked = intake.refresh(limited, numbers=[299])
    assert blocked["outcome"] == "blocked"
    assert blocked["missing_capability"] == "github_read_rate_limit"

    def down(method, url, headers):
        return httpx.Response(503, json={"message": "GitHub is unavailable"})

    unavailable = GitHubReadClient(transport=down)
    downed = intake.refresh(unavailable, numbers=[299])
    assert downed["missing_capability"] == "github_read_unavailable"

    def forbidden(method, url, headers):
        return httpx.Response(403, json={"message": "Resource not accessible by integration"})

    denied = GitHubReadClient(token="x", transport=forbidden)
    assert intake.refresh(denied, numbers=[299])["missing_capability"] == "github_read_permission"


def test_public_client_sends_no_authorization_header():
    seen = {}

    def capture(method, url, headers):
        seen["headers"] = headers
        seen["url"] = url
        return httpx.Response(200, json={"number": 299, "head": {"sha": HEAD, "ref": "cursor/x"}, "base": {"sha": PARENT, "ref": "main"}, "draft": False, "merged": False, "state": "open"})

    client = GitHubReadClient(transport=capture)
    assert client.access_mode == "public_unauthenticated"
    client.get("/repos/d1n095/LifeAI/pulls/299")
    assert "Authorization" not in seen["headers"]


def test_founder_view_exposes_operational_github_state_without_transcription():
    intake = GitHubDevelopmentIntake(owner_id=OWNER)
    intake.ingest_pull_request(_pr())
    view = intake.service.founder_view()
    assert view["first_cycle_go"] is False
    github = view["github_intake"]
    assert github["delivery_claimed"] is False
    assert github["write_attempted"] is False
    assert github["candidates"][0]["head_sha"] == HEAD
    assert github["candidates"][0]["ci"]["freeze_allowed"] is True
    assert github["needed_review"][0]["need"] == "independent_review"
    assert view["needed_work"]["founder_must_forward_reports"] is False


def test_public_github_can_read_frozen_pr_299_without_token():
    client = GitHubReadClient()
    try:
        payload = client.get("/repos/d1n095/LifeAI/pulls/299")
    except GitHubReadError as exc:
        assert exc.missing_capability in {
            "github_read_rate_limit",
            "github_read_unavailable",
            "github_read_permission",
        }
        return
    assert payload["number"] == 299
    sha = (payload.get("head") or {}).get("sha")
    assert isinstance(sha, str) and len(sha) == 40
    assert "Authorization" not in GitHubReadClient()._headers()


def test_draft_pr_is_not_reviewable():
    intake = GitHubDevelopmentIntake(owner_id=OWNER)
    result = intake.ingest_pull_request(_pr(draft=True))
    assert result["reviewable"] is False
    assert result["examiner_assignment_id"] is None
