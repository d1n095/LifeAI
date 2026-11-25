"""Founder personal work management: real PostgreSQL acceptance tests.

Covers the additive migration 0095 path, owner isolation, task dependencies/history,
milestone rollup, calendar recurrence/conflicts, dashboard aggregation and cross-object
search. The browser is not required for these assertions; each request goes through the real
FastAPI app and the restricted runtime database role.
"""

import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import event

from app.config import get_settings
from app.db import engine
from app.models.conversation import Conversation
from app.models.document import Document, DocumentSource
from app.models.user import User

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    response = client.post(
        "/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def _headers(csrf: str) -> dict:
    return {"X-CSRF-Token": csrf}


def _founder_id(superuser_db) -> uuid.UUID:
    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def _workspace(client, headers, name: str = "Produktlansering") -> dict:
    response = client.post(
        "/api/workspaces",
        json={
            "name": name,
            "purpose": "Leverera en granskningsbar produkt.",
            "goals": ["Lansera beta", "Samla återkoppling"],
            "status": "active",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


def _other_client(make_verified_user) -> tuple[TestClient, str]:
    from app.main import app

    user, password = make_verified_user(
        email=f"other-{uuid.uuid4().hex[:8]}@example.com"
    )
    other = TestClient(app, base_url="https://testserver")
    other.__enter__()
    response = other.post("/api/auth/login", json={"email": user.email, "password": password})
    assert response.status_code == 200, response.text
    return other, response.json()["csrf_token"]


def test_task_manager_dependencies_history_milestone_and_owner_scope(
    client, superuser_db, make_verified_user
):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = _workspace(client, headers)
    project_id = workspace["id"]

    milestone = client.post(
        f"/api/projects/{project_id}/milestones",
        json={"title": "Beta klar", "due_at": (datetime.utcnow() + timedelta(days=14)).isoformat()},
        headers=headers,
    )
    assert milestone.status_code == 201, milestone.text
    milestone_id = milestone.json()["id"]

    first = client.post(
        "/api/projects/tasks",
        json={
            "project_id": project_id,
            "milestone_id": milestone_id,
            "title": "Skriv specifikation",
            "priority": "high",
            "status": "todo",
        },
        headers=headers,
    )
    assert first.status_code == 201, first.text
    first = first.json()

    second = client.post(
        "/api/projects/tasks",
        json={
            "project_id": project_id,
            "milestone_id": milestone_id,
            "title": "Bygg beta",
            "priority": "critical",
            "status": "todo",
            "depends_on_task_ids": [first["id"]],
        },
        headers=headers,
    )
    assert second.status_code == 201, second.text
    second = second.json()
    assert second["status"] == "blocked"
    assert second["is_blocked"] is True
    assert second["dependency_count"] == 1

    detail = client.get(f"/api/projects/tasks/{second['id']}", headers=headers)
    assert detail.status_code == 200, detail.text
    detail = detail.json()
    assert detail["kind"] == "user_task"
    assert detail["execution_authority"] == "none"
    assert detail["dependencies"][0]["depends_on_title"] == "Skriv specifikation"
    assert any(item["event_type"] == "blocked" for item in detail["events"])

    filtered = client.get(
        f"/api/projects/tasks?project_id={project_id}&blocked_only=true", headers=headers
    )
    assert filtered.status_code == 200, filtered.text
    assert [item["id"] for item in filtered.json()] == [second["id"]]

    done_first = client.patch(
        f"/api/projects/tasks/{first['id']}",
        json={"status": "done", "expected_version": first["version"]},
        headers=headers,
    )
    assert done_first.status_code == 200, done_first.text

    unblocked = client.get(f"/api/projects/tasks/{second['id']}", headers=headers).json()
    assert unblocked["status"] == "todo"
    assert unblocked["is_blocked"] is False

    in_progress = client.patch(
        f"/api/projects/tasks/{second['id']}",
        json={"status": "in_progress", "expected_version": unblocked["version"]},
        headers=headers,
    )
    assert in_progress.status_code == 200, in_progress.text
    completed = client.patch(
        f"/api/projects/tasks/{second['id']}",
        json={"status": "done", "expected_version": in_progress.json()["version"]},
        headers=headers,
    )
    assert completed.status_code == 200, completed.text

    project = client.get(f"/api/projects/{project_id}", headers=headers)
    assert project.status_code == 200, project.text
    milestone_after = project.json()["milestones"][0]
    assert milestone_after["status"] == "completed"
    assert milestone_after["completed_task_count"] == 2

    stale = client.patch(
        f"/api/projects/tasks/{second['id']}",
        json={"title": "Gammal klient", "expected_version": second["version"]},
        headers=headers,
    )
    assert stale.status_code == 409

    other, other_csrf = _other_client(make_verified_user)
    try:
        # MainAI is founder-only: the second account is denied before any owner-filtered
        # query runs. The owner-filtered 404 path is covered by the workspace tests.
        assert other.get(f"/api/projects/{project_id}").status_code == 403
        assert other.get(f"/api/projects/tasks/{second['id']}").status_code == 403
        assert other.get("/api/projects/tasks").status_code == 403
    finally:
        other.__exit__(None, None, None)


def test_calendar_recurrence_conflict_edit_delete_and_links(client, make_verified_user):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = _workspace(client, headers, "Kalenderprojekt")
    project_id = workspace["id"]
    task = client.post(
        "/api/projects/tasks",
        json={"project_id": project_id, "title": "Förbered demo"},
        headers=headers,
    ).json()

    start = datetime.utcnow() + timedelta(days=2)
    created = client.post(
        "/api/calendar/events",
        json={
            "title": "Veckodemo",
            "starts_at": start.isoformat(),
            "ends_at": (start + timedelta(hours=1)).isoformat(),
            "category": "work",
            "timezone": "Europe/Stockholm",
            "recurrence_rule": "weekly",
            "recurrence_until": (start + timedelta(days=15)).isoformat(),
            "project_id": project_id,
            "task_id": task["id"],
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    base = created.json()
    assert base["series_id"] is not None
    assert base["category"] == "work"
    assert base["project_id"] == project_id
    assert base["task_id"] == task["id"]

    # Browser clients send timezone-aware ISO strings; stored rows are naive. This exact
    # shape used to raise TypeError in conflict detection and return 500.
    aware_start = start.replace(tzinfo=timezone.utc)
    listed = client.get(
        "/api/calendar/events",
        params={
            "starts_after": (aware_start - timedelta(days=1)).isoformat(),
            "starts_before": (aware_start + timedelta(days=20)).isoformat(),
        },
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    occurrences = listed.json()
    assert len(occurrences) >= 3
    assert all(item["series_id"] == base["series_id"] for item in occurrences)

    conflicts = client.get(
        "/api/calendar/conflicts",
        params={
            "starts_at": (aware_start + timedelta(minutes=15)).isoformat(),
            "ends_at": (aware_start + timedelta(minutes=45)).isoformat(),
        },
        headers=headers,
    )
    assert conflicts.status_code == 200, conflicts.text
    assert any(item["id"] == base["id"] for item in conflicts.json())

    updated = client.patch(
        f"/api/calendar/events/{base['id']}",
        json={"title": "Veckodemo uppdaterad", "expected_version": base["version"]},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["title"] == "Veckodemo uppdaterad"

    deleted = client.delete(
        f"/api/calendar/events/{base['id']}?apply_to_series=true", headers=headers
    )
    assert deleted.status_code == 200, deleted.text
    remaining = client.get(
        "/api/calendar/events",
        params={
            "starts_after": (start - timedelta(days=1)).isoformat(),
            "starts_before": (start + timedelta(days=20)).isoformat(),
        },
        headers=headers,
    ).json()
    assert remaining == []

    other, _ = _other_client(make_verified_user)
    try:
        assert other.get(f"/api/calendar/events/{base['id']}").status_code == 403
    finally:
        other.__exit__(None, None, None)


def test_dashboard_search_restart_persistence_and_query_budget(
    client, superuser_db, make_verified_user
):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = _workspace(client, headers, "Sökbart projekt alpha")
    project_id = workspace["id"]
    task = client.post(
        "/api/projects/tasks",
        json={"project_id": project_id, "title": "Unik uppgift alpha"},
        headers=headers,
    ).json()
    decision = client.post(
        f"/api/workspaces/{project_id}/decisions",
        json={
            "title": "Unikt beslut alpha",
            "decision": "Vi väljer alpha.",
            "status": "accepted",
            "epistemic_status": "confirmed",
        },
        headers=headers,
    )
    assert decision.status_code == 200, decision.text
    previous = decision.json()
    superseded = client.post(
        f"/api/workspaces/{project_id}/decisions/{previous['id']}/supersede",
        json={
            "title": "Unikt beslut alpha v2",
            "decision": "Vi väljer beta i stället.",
            "status": "accepted",
            "epistemic_status": "confirmed",
        },
        headers=headers,
    )
    assert superseded.status_code == 200, superseded.text
    assert superseded.json()["supersedes_id"] == previous["id"]
    decisions_after = client.get(f"/api/workspaces/{project_id}/decisions", headers=headers)
    assert decisions_after.status_code == 200, decisions_after.text
    old_decision = next(
        item for item in decisions_after.json() if item["id"] == previous["id"]
    )
    assert old_decision["status"] == "superseded"
    assert old_decision["superseded_at"] is not None
    calendar_event = client.post(
        "/api/calendar/events",
        json={
            "title": "Unikt kalendermöte alpha",
            "starts_at": (datetime.utcnow() + timedelta(days=1)).isoformat(),
            "category": "personal",
        },
        headers=headers,
    )
    assert calendar_event.status_code == 201, calendar_event.text

    document = Document(
        title="Unik källa alpha",
        source=DocumentSource.manual,
        uploaded_by=_founder_id(superuser_db),
        project_id=project_id,
        checksum=uuid.uuid4().hex,
    )
    superuser_db.add(document)
    conversation = Conversation(
        user_id=_founder_id(superuser_db),
        project_id=project_id,
        title="Unik konversation alpha",
    )
    superuser_db.add(conversation)
    superuser_db.commit()

    dashboard = client.get("/api/dashboard", headers=headers)
    assert dashboard.status_code == 200, dashboard.text
    body = dashboard.json()
    assert body["counts"]["active_projects"] >= 1
    assert body["counts"]["open_tasks"] >= 1
    assert body["counts"]["upcoming_events"] >= 1
    assert any(item["id"] == project_id for item in body["active_projects"])
    assert any(item["id"] == task["id"] for item in body["outstanding_tasks"])

    search_started = time.perf_counter()
    search = client.get("/api/search", params={"q": "alpha"}, headers=headers)
    search_ms = (time.perf_counter() - search_started) * 1000
    assert search.status_code == 200, search.text
    search_body = search.json()
    assert search_body["total"] >= 5
    assert {item["type"] for item in search_body["results"]} >= {
        "project",
        "task",
        "document",
        "conversation",
        "calendar_event",
    }

    # Stable pagination: the same query with limit/offset must not duplicate or skip rows.
    first_page = client.get(
        "/api/search", params={"q": "alpha", "limit": 2, "offset": 0}, headers=headers
    )
    second_page = client.get(
        "/api/search", params={"q": "alpha", "limit": 2, "offset": 2}, headers=headers
    )
    assert first_page.status_code == 200, first_page.text
    assert second_page.status_code == 200, second_page.text
    first_ids = {(item["type"], item["id"]) for item in first_page.json()["results"]}
    second_ids = {(item["type"], item["id"]) for item in second_page.json()["results"]}
    assert first_ids.isdisjoint(second_ids)
    assert first_page.json()["total"] == second_page.json()["total"]

    # Dashboard aggregation is bounded: one request must not issue one query per row.
    statements: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    dashboard_started = time.perf_counter()
    try:
        measured = client.get("/api/dashboard", headers=headers)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    dashboard_ms = (time.perf_counter() - dashboard_started) * 1000
    assert measured.status_code == 200, measured.text
    assert len(statements) <= 40, f"dashboard issued {len(statements)} SQL statements"
    print(
        f"PERF dashboard_sql_statements={len(statements)} "
        f"dashboard_ms={dashboard_ms:.1f} search_ms={search_ms:.1f}"
    )

    # A fresh TestClient represents a new application process: the same rows must still load.
    from app.main import app

    with TestClient(app, base_url="https://testserver") as fresh:
        fresh_login = fresh.post(
            "/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD}
        )
        assert fresh_login.status_code == 200, fresh_login.text
        fresh_headers = _headers(fresh_login.json()["csrf_token"])
        persisted_project = fresh.get(f"/api/projects/{project_id}", headers=fresh_headers)
        assert persisted_project.status_code == 200, persisted_project.text
        assert any(item["id"] == task["id"] for item in persisted_project.json()["tasks"])
        persisted_search = fresh.get("/api/search", params={"q": "alpha"}, headers=fresh_headers)
        assert persisted_search.status_code == 200, persisted_search.text
        assert persisted_search.json()["total"] >= 5

    other, _ = _other_client(make_verified_user)
    try:
        assert other.get("/api/search", params={"q": "alpha"}).status_code == 403
        assert other.get("/api/dashboard").status_code == 403
    finally:
        other.__exit__(None, None, None)
