"""Founder Alpha R14: GitHub development status is observed, durable and honest.

  G1  no credentials is a VALUE (`configured: false`), never an error and never a guessed head;
  G2  a successful refresh stores exactly what GitHub returned, with its timestamp;
  G3  a FAILED refresh never overwrites the last verified picture (it is served with its age);
  G4  an old snapshot is reported stale instead of quietly staying current;
  G5  the local checkout is compared against its OWN upstream, and "no upstream" is UNKNOWN,
      never "0 unpushed";
  G6  the founder-only HTTP surface returns the same facts, and is not public.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.github_status import service as gh
from app.models.github_status import GithubStatusSnapshot
from app.models.user import User
from app.project_status.service import build_project_status
from app.request_context import current_user_id as current_user_id_var

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


@pytest.fixture
def founder(client, db_session):
    user = db_session.query(User).filter(User.email == FOUNDER_EMAIL).first()
    _set_rls_user(db_session, user.id)
    return user


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _fake_payload(sha: str = "a" * 40) -> dict:
    return {
        "branch": "main",
        "head_sha": sha,
        "head_commit": {"subject": "fix: en riktig commit", "author": "Founder", "committed_at": "2026-11-19T10:00:00Z"},
        "recent_commits": [{"sha": sha, "subject": "fix: en riktig commit"}],
        "open_prs": [{"number": 457, "title": "En PR", "draft": True, "head": "x", "base": "main"}],
        "open_issue_count": 3,
        "checks": {"total": 2, "passing": 2, "failing": 0, "pending": 0, "named": []},
        "fetched_at": "2026-11-19T10:05:00Z",
    }


@pytest.fixture
def configured(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "github_repo", "d1n095/LifeAI")
    monkeypatch.setattr(gh.GitHubClient, "is_configured", lambda self: True)
    return settings


# --- G1 ----------------------------------------------------------------------------------------


def test_not_configured_is_a_value(db_session, founder, monkeypatch):
    monkeypatch.setattr(gh.GitHubClient, "is_configured", lambda self: False)
    status = gh.dev_status(db_session, owner_id=founder.id)

    assert status["configured"] is False
    assert status["remote"] is None
    assert status["last_verified_at"] is None
    # nothing was invented
    assert "verifieras" in status["note"]
    assert gh.summary_for_prompt(status).startswith("GitHub: inte konfigurerad")


# --- G2 ----------------------------------------------------------------------------------------


def test_successful_refresh_stores_exactly_what_github_returned(db_session, founder, configured, monkeypatch):
    async def _fetch():
        return _fake_payload()

    monkeypatch.setattr(gh, "fetch_remote_status", _fetch)
    result = __import__("asyncio").run(gh.refresh(db_session, owner_id=founder.id))

    assert result["ok"] is True and result["snapshot_saved"] is True
    status = result["status"]
    assert status["configured"] is True
    assert status["remote"]["head_sha"] == "a" * 40
    assert status["remote"]["open_prs"][0]["number"] == 457
    assert status["age_minutes"] == 0
    assert status["stale"] is False

    row = db_session.query(GithubStatusSnapshot).filter(GithubStatusSnapshot.owner_id == founder.id).one()
    assert row.payload["head_sha"] == "a" * 40


# --- G3 ----------------------------------------------------------------------------------------


def test_failed_refresh_never_overwrites_the_last_verified_picture(db_session, founder, configured, monkeypatch):
    async def _ok():
        return _fake_payload("b" * 40)

    monkeypatch.setattr(gh, "fetch_remote_status", _ok)
    __import__("asyncio").run(gh.refresh(db_session, owner_id=founder.id))

    async def _boom():
        raise RuntimeError("GitHub svarade 502")

    monkeypatch.setattr(gh, "fetch_remote_status", _boom)
    result = __import__("asyncio").run(gh.refresh(db_session, owner_id=founder.id))

    assert result["ok"] is False
    assert result["snapshot_saved"] is False
    assert "502" in result["message"]
    # the verified picture is still the one served
    assert result["status"]["remote"]["head_sha"] == "b" * 40
    assert db_session.query(GithubStatusSnapshot).filter(GithubStatusSnapshot.owner_id == founder.id).count() == 1


# --- G4 ----------------------------------------------------------------------------------------


def test_old_snapshot_is_reported_stale(db_session, founder, configured, monkeypatch):
    settings = get_settings()
    row = GithubStatusSnapshot(
        owner_id=founder.id,
        repo="d1n095/LifeAI",
        payload=_fake_payload(),
        fetched_at=datetime.utcnow() - timedelta(minutes=settings.github_status_stale_minutes + 5),
    )
    db_session.add(row)
    db_session.commit()

    status = gh.dev_status(db_session, owner_id=founder.id)
    assert status["stale"] is True
    assert status["age_minutes"] >= settings.github_status_stale_minutes
    assert "INAKTUELL" in gh.summary_for_prompt(status)


# --- G5 ----------------------------------------------------------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_local_state_reports_unknown_instead_of_zero_without_upstream(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")

    monkeypatch.setattr(get_settings(), "local_repo_path", str(repo))
    state = gh.local_git_state()

    assert state["available"] is True
    assert state["head_sha"]
    assert state["branch"]
    assert state["compare_ref"] is None, "no upstream must be UNKNOWN, never assumed origin/main"
    assert state["unpushed_count"] is None
    assert "okänd" in state["compare_note"]


def test_local_state_counts_unpushed_against_its_own_upstream(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    repo = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(remote), str(repo))
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.txt").write_text("x")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "first")
    _git(repo, "push", "-q", "-u", "origin", "HEAD")

    monkeypatch.setattr(get_settings(), "local_repo_path", str(repo))
    assert gh.local_git_state()["unpushed_count"] == 0

    (repo / "a.txt").write_text("y")
    _git(repo, "commit", "-q", "-am", "second")
    state = gh.local_git_state()
    assert state["compare_ref"] == "origin/HEAD" or state["compare_ref"].startswith("origin/")
    assert state["unpushed_count"] == 1


# --- G6 ----------------------------------------------------------------------------------------


def test_http_surface_is_founder_only_and_no_store(client, founder):
    csrf = _login(client)
    res = client.get("/api/mainai/github-status")
    assert res.status_code == 200
    assert res.headers["cache-control"] == "no-store"
    body = res.json()
    assert "configured" in body and "local" in body

    # a non-founder session is rejected
    client.post("/api/auth/logout", headers={"X-CSRF-Token": csrf})
    # (logout clears cookies; a fresh anonymous read must 401)
    assert client.get("/api/mainai/github-status").status_code == 401


def test_project_status_reads_the_same_configuration_fact(client, db_session, founder, monkeypatch):
    monkeypatch.setattr(gh.GitHubClient, "is_configured", lambda self: False)
    _login(client)
    body = client.get("/api/mainai/project-status").json()
    dev = client.get("/api/mainai/github-status").json()
    assert body["github"]["configured"] == dev["configured"] is False
