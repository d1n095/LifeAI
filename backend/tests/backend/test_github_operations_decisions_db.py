from __future__ import annotations

import json
import uuid

import pytest

from app.integrations import integration_analysis
from app.integrations.github_client import GitHubListResult
from app.models.project_memory import NoteKind, NoteStatus, ProjectNote

FOUNDER_EMAIL = "founder@lifeos.local"
REPOSITORY = "d1n095/LifeAI"
CANONICAL_SHA = "7878958bfa57fa999122ac3008e9f261233f158e"
REJECTED_SHA = "3a19c7640ec75b5574cc268ed1c8b76ddba4d43e"


def _decision_content(decision_id: str, repository: str) -> dict:
    return {
        "schema": "lifeai.architecture-decision.v1",
        "decision_id": decision_id,
        "version": 1,
        "repository": repository,
        "approver_email": FOUNDER_EMAIL,
        "approved_at": "2026-11-10T12:00:00+00:00",
        "effective_at": "2026-11-10T12:00:00+00:00",
        "summary": "Use the founder-selected stop-control architecture.",
        "affected_modules": ["backend/app/workforce/"],
        "canonical": {
            "pull_request": 237,
            "head_sha": CANONICAL_SHA,
            "architecture": ["mainai_stop_state", "mainai_stop_events"],
        },
        "rejected_alternatives": [{
            "pull_request": 243,
            "head_sha": REJECTED_SHA,
            "label": "workforce_authority_epoch",
            "signals": ["workforce_authority_epoch"],
        }],
    }


@pytest.mark.parametrize(
    ("creator", "repository", "rejected_sha", "expected_status", "expected_readiness"),
    [
        (FOUNDER_EMAIL, REPOSITORY, REJECTED_SHA, "VERIFIED", "READY"),
        ("builder@example.test", REPOSITORY, REJECTED_SHA, "UNKNOWN", "UNKNOWN"),
        (FOUNDER_EMAIL, "another-owner/another-project", REJECTED_SHA, "OUT_OF_SCOPE", "UNKNOWN"),
        (FOUNDER_EMAIL, REPOSITORY, "f" * 40, "STALE", "UNKNOWN"),
    ],
)
def test_database_decision_provenance_scope_and_pr_heads_gate_readiness(
    client, db_session, monkeypatch, request, creator, repository, rejected_sha,
    expected_status, expected_readiness,
):
    from app.routers import github_operations as route

    decision_id = f"db-test-{uuid.uuid4().hex}"
    note = ProjectNote(
        kind=NoteKind.decision,
        status=NoteStatus.open,
        content=json.dumps(_decision_content(decision_id, repository)),
        source_type="doc",
        source_ref="Founder stop-control decision",
        created_by=creator,
    )
    db_session.add(note)
    db_session.commit()

    def cleanup_note() -> None:
        db_session.delete(note)
        db_session.commit()

    request.addfinalizer(cleanup_note)

    calls: list[int] = []

    class ReadOnlyGitHub:
        async def get_pull_request(self, number: int) -> dict:
            calls.append(number)
            if number == 237:
                return {"state": "open", "head": {"sha": CANONICAL_SHA}}
            assert number == 243
            return {
                "state": "closed",
                "merged_at": None,
                "head": {"sha": rejected_sha},
            }

        async def list_pull_request_files(self, _number: int) -> GitHubListResult:
            return GitHubListResult(items=[], pages_fetched=1, truncated=False)

    monkeypatch.setattr(integration_analysis, "GitHubClient", ReadOnlyGitHub)

    async def github_observation(_force_refresh: bool) -> dict:
        return {
            "pull_requests": [{
                "number": 237,
                "head_sha": CANONICAL_SHA,
                "head_branch": "canonical-stop-control",
                "base_branch": "main",
                "head_repository": REPOSITORY,
                "author": "builder",
                "url": "https://github.com/d1n095/LifeAI/pull/237",
                "dependencies": [],
                "reviews": [{"state": "APPROVED", "commit_sha": CANONICAL_SHA}],
                "review_state": "approved_exact_head",
                "ci_state": "success",
                "mergeability": {"state": "MERGEABLE"},
                "canonical_claims": [],
            }],
            "closed_pull_requests": [],
            "freshness": "live",
            "repository": {"full_name": REPOSITORY},
            "pagination": {"open_pull_requests_truncated": False},
            "observed_at": "2026-11-12T00:00:00Z",
        }

    monkeypatch.setattr(route, "_get_github_observation", github_observation)
    login = client.post(
        "/api/auth/login",
        json={"email": FOUNDER_EMAIL, "password": "TestFounderPassword123!"},
    )
    assert login.status_code == 200

    response = client.get("/api/github-operations/integration-analysis")
    assert response.status_code == 200, response.text
    result = response.json()
    decisions = result["coverage"]["canonical_decisions"]["decisions"]
    if expected_status == "UNKNOWN" and creator != FOUNDER_EMAIL:
        assert decisions == []
        assert 243 not in calls
    else:
        assert len(decisions) == 1
        assert decisions[0]["status"] == expected_status
    assert result["candidates"][0]["readiness"] == expected_readiness
    assert result["candidates"][0]["safe_to_integrate"] != "YES" or expected_status == "VERIFIED"
    if repository != REPOSITORY:
        assert 243 not in calls
        assert decisions[0]["repository_evidence"] == {
            "expected": REPOSITORY,
            "recorded": repository,
        }
    elif creator == FOUNDER_EMAIL:
        assert 243 in calls
