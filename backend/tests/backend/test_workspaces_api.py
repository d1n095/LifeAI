"""Life Workspaces API: persistent project composition, owner scoping and soft retention.

These tests use the real restricted runtime role and the real PostgreSQL schema (see
tests/conftest.py), so the workspace joins exercise the same RLS/ownership boundaries as
production rather than a mocked repository.
"""

import uuid
from datetime import datetime, timedelta

from app.config import get_settings
from app.models.conversation import Conversation
from app.models.document import Document, DocumentSource
from app.models.mainai_execution import (
    MainAICheckpoint,
    MainAIGoal,
    MainAIGoalRiskLevel,
    MainAIPlan,
    MainAIPlanStatus,
    MainAITask,
    MainAITaskEvent,
    MainAITaskEventType,
    MainAITaskStatus,
)
from app.models.project import Project
from app.models.user import User
from app.models.workspace import WorkspaceSyncRun, WorkspaceSyncStatus
from app.providers.base import ChatResult
from app.providers.openai_provider import OpenAIProvider

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _headers(csrf: str) -> dict:
    return {"X-CSRF-Token": csrf}


def _founder_id(superuser_db) -> uuid.UUID:
    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def _make_document(superuser_db, *, owner_id, title, project_id=None) -> Document:
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        project_id=project_id,
        checksum=uuid.uuid4().hex,
    )
    superuser_db.add(document)
    superuser_db.commit()
    return document


def test_workspace_lifecycle_scoped_collections_and_context(client, superuser_db):
    csrf = _login(client)
    headers = _headers(csrf)

    created = client.post(
        "/api/workspaces",
        json={
            "name": "Rapportpipeline",
            "description": "Gör rapportflödet reproducerbart.",
            "purpose": "Leverera en granskningsbar rapport varje vecka.",
            "goals": ["Automatisera insamlingen", "Behåll spårbarhet"],
            "status": "active",
            "settings": {"timezone": "Europe/Stockholm"},
        },
        headers=headers,
    )
    assert created.status_code == 200, created.text
    workspace = created.json()
    workspace_id = workspace["id"]
    assert workspace["purpose"].startswith("Leverera")
    assert workspace["goals"] == ["Automatisera insamlingen", "Behåll spårbarhet"]

    task = client.post(
        f"/api/workspaces/{workspace_id}/tasks",
        json={"title": "Samla in veckodata", "status": "todo", "priority": "high"},
        headers=headers,
    )
    assert task.status_code == 200, task.text
    assert task.json()["project_id"] == workspace_id

    decision = client.post(
        f"/api/workspaces/{workspace_id}/decisions",
        json={
            "title": "Veckovis publicering",
            "decision": "Rapporten publiceras varje fredag.",
            "rationale": "Grundaren vill ha en fast granskningsrytm.",
            "status": "accepted",
        },
        headers=headers,
    )
    assert decision.status_code == 200, decision.text
    assert decision.json()["status"] == "accepted"

    founder_id = _founder_id(superuser_db)
    source = _make_document(superuser_db, owner_id=founder_id, title="Veckorapport 2026-11-21")
    associated = client.post(
        f"/api/workspaces/{workspace_id}/documents/{source.id}",
        headers=headers,
    )
    assert associated.status_code == 200, associated.text
    assert associated.json()["project_id"] == workspace_id

    knowledge = client.get(f"/api/workspaces/{workspace_id}/knowledge")
    assert knowledge.status_code == 200, knowledge.text
    assert [item["id"] for item in knowledge.json()] == [str(source.id)]

    detail = client.get(f"/api/workspaces/{workspace_id}")
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["id"] == workspace_id
    assert [item["title"] for item in body["tasks"]] == ["Samla in veckodata"]
    assert [item["title"] for item in body["decisions"]] == ["Veckovis publicering"]
    assert [item["id"] for item in body["sources"]] == [str(source.id)]
    assert body["context"]["objectives"][0].startswith("Leverera")
    assert body["context"]["outstanding_tasks"][0]["title"] == "Samla in veckodata"
    assert body["context"]["recent_changes"]

    updated = client.patch(
        f"/api/workspaces/{workspace_id}",
        json={"name": "Rapportpipeline v2", "goals": ["Automatisera", "Mät kvalitet"]},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["goals"] == ["Automatisera", "Mät kvalitet"]

    archived = client.post(f"/api/workspaces/{workspace_id}/archive", headers=headers)
    assert archived.status_code == 200, archived.text
    assert archived.json()["archived_at"] is not None
    assert all(item["id"] != workspace_id for item in client.get("/api/workspaces").json())
    assert any(
        item["id"] == workspace_id
        for item in client.get("/api/workspaces?include_archived=true").json()
    )

    restored = client.post(f"/api/workspaces/{workspace_id}/restore", headers=headers)
    assert restored.status_code == 200, restored.text
    assert restored.json()["archived_at"] is None

    deleted = client.delete(f"/api/workspaces/{workspace_id}", headers=headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["retained"] is True
    assert client.get(f"/api/workspaces/{workspace_id}").status_code == 404
    assert superuser_db.query(Project).filter_by(id=uuid.UUID(workspace_id)).one().deleted_at is not None


def test_workspace_owner_scope_hides_foreign_project_and_document(client, superuser_db, make_verified_user):
    csrf = _login(client)
    headers = _headers(csrf)
    other_user, _ = make_verified_user(email="workspace-other@example.com")

    foreign = Project(
        name="Annan ägares workspace",
        created_by=other_user.id,
        status="active",
        goals=[],
        settings={},
        last_activity_at=datetime.utcnow(),
    )
    superuser_db.add(foreign)
    superuser_db.commit()

    assert client.get(f"/api/workspaces/{foreign.id}").status_code == 404
    assert client.patch(
        f"/api/workspaces/{foreign.id}", json={"name": "kapad"}, headers=headers
    ).status_code == 404
    assert client.post(f"/api/workspaces/{foreign.id}/archive", headers=headers).status_code == 404

    own = client.post(
        "/api/workspaces",
        json={"name": "Egen workspace", "status": "active"},
        headers=headers,
    ).json()
    foreign_document = _make_document(
        superuser_db, owner_id=other_user.id, title="Annan ägares källa"
    )
    assert client.post(
        f"/api/workspaces/{own['id']}/documents/{foreign_document.id}", headers=headers
    ).status_code == 404


def test_workspace_conversation_is_persisted_with_the_workspace_link(client):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Samtalsworkspace", "status": "active"},
        headers=headers,
    ).json()

    conversation = client.post(
        f"/api/workspaces/{workspace['id']}/conversations", headers=headers
    )
    assert conversation.status_code == 200, conversation.text
    conversation_body = conversation.json()
    assert conversation_body["project_id"] == workspace["id"]

    listed = client.get(f"/api/workspaces/{workspace['id']}/conversations")
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [conversation_body["id"]]

    detail = client.get(f"/api/conversations/{conversation_body['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["project_id"] == workspace["id"]


def test_chat_binds_new_conversation_to_workspace_and_scopes_retrieval(
    client, superuser_db, monkeypatch
):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Chatt-workspace", "status": "active"},
        headers=headers,
    ).json()
    other_workspace = client.post(
        "/api/workspaces",
        json={"name": "Annat chatt-workspace", "status": "active"},
        headers=headers,
    ).json()

    retrieval_calls: list[dict] = []

    async def _fake_retrieve(_db, _owner_id, _query, **kwargs):
        retrieval_calls.append(kwargs)
        return []

    async def _fake_chat(self, messages, model, **kwargs):
        return ChatResult(
            content="Workspace-svar.",
            provider="openai",
            model=model,
            raw_usage={"prompt_tokens": 5, "completion_tokens": 2},
        )

    monkeypatch.setattr("app.routers.chat.retrieve_context", _fake_retrieve)
    monkeypatch.setattr(OpenAIProvider, "chat", _fake_chat)
    monkeypatch.setattr(get_settings(), "chat_fallback_order", "")

    response = client.post(
        "/api/chat",
        json={"message": "Vad vet workspaceet?", "project_id": workspace["id"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    conversation = superuser_db.query(Conversation).filter_by(id=uuid.UUID(body["conversation_id"])).one()
    assert conversation.project_id == uuid.UUID(workspace["id"])
    assert retrieval_calls and retrieval_calls[0]["project_id"] == uuid.UUID(workspace["id"])

    # A conversation bound to one workspace cannot be silently re-used for another one.
    conflict = client.post(
        "/api/chat",
        json={
            "message": "Fortsätt i fel workspace",
            "conversation_id": body["conversation_id"],
            "project_id": other_workspace["id"],
        },
        headers=headers,
    )
    assert conflict.status_code == 409, conflict.text


def test_workspace_pagination_and_optimistic_version_conflict(client):
    csrf = _login(client)
    headers = _headers(csrf)
    created_ids = []
    for index in range(3):
        response = client.post(
            "/api/workspaces",
            json={"name": f"Pagination {index}", "status": "active"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        created_ids.append(response.json()["id"])

    first_page = client.get("/api/workspaces?limit=2&offset=0").json()
    second_page = client.get("/api/workspaces?limit=2&offset=2").json()
    assert len(first_page) == 2
    assert len(second_page) == 1
    assert {item["id"] for item in first_page}.isdisjoint({item["id"] for item in second_page})
    assert {item["id"] for item in first_page + second_page} == set(created_ids)

    workspace = client.get(f"/api/workspaces/{created_ids[0]}").json()
    version = workspace["version"]
    updated = client.patch(
        f"/api/workspaces/{created_ids[0]}",
        json={"name": "Pagination uppdaterad", "expected_version": version},
        headers=headers,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"] == version + 1

    stale = client.patch(
        f"/api/workspaces/{created_ids[0]}",
        json={"name": "Ska inte skrivas", "expected_version": version},
        headers=headers,
    )
    assert stale.status_code == 409, stale.text
    assert client.get(f"/api/workspaces/{created_ids[0]}").json()["name"] == "Pagination uppdaterad"


def test_workspace_sync_is_idempotent_and_skips_unchanged_entities(client, superuser_db):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Sync workspace", "status": "active"},
        headers=headers,
    ).json()
    founder_id = _founder_id(superuser_db)
    source = _make_document(superuser_db, owner_id=founder_id, title="Sync-källa")
    assert client.post(
        f"/api/workspaces/{workspace['id']}/documents/{source.id}", headers=headers
    ).status_code == 200

    key = uuid.uuid4().hex
    first = client.post(
        f"/api/workspaces/{workspace['id']}/sync",
        json={"idempotency_key": key},
        headers=headers,
    )
    assert first.status_code == 200, first.text
    run = first.json()
    assert run["status"] == "completed"
    assert run["progress_total"] >= 2
    assert run["progress_current"] == run["progress_total"]
    assert any(item["entity_type"] == "document" for item in run["items"])

    replay = client.post(
        f"/api/workspaces/{workspace['id']}/sync",
        json={"idempotency_key": key},
        headers=headers,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json()["id"] == run["id"]
    assert len(client.get(f"/api/workspaces/{workspace['id']}/sync").json()) == 1

    unchanged = client.post(
        f"/api/workspaces/{workspace['id']}/sync",
        json={"idempotency_key": uuid.uuid4().hex},
        headers=headers,
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["status"] == "completed"
    assert unchanged.json()["result"]["unchanged"] is True
    assert all(item["status"] == "skipped" for item in unchanged.json()["items"])


def test_workspace_sync_failure_retry_and_stale_rejection(client, monkeypatch):
    import app.routers.workspaces as workspace_router

    csrf = _login(client)
    headers = _headers(csrf)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Sync retry", "status": "active"},
        headers=headers,
    ).json()

    original = workspace_router._apply_sync_entity

    def fail(*args, **kwargs):
        raise RuntimeError("simulated sync failure")

    monkeypatch.setattr(workspace_router, "_apply_sync_entity", fail)
    failed = client.post(
        f"/api/workspaces/{workspace['id']}/sync",
        json={"idempotency_key": uuid.uuid4().hex},
        headers=headers,
    )
    assert failed.status_code == 200, failed.text
    failed_run = failed.json()
    assert failed_run["status"] == "failed"
    assert failed_run["error_category"] == "sync_failed"

    monkeypatch.setattr(workspace_router, "_apply_sync_entity", original)
    retried = client.post(
        f"/api/workspaces/{workspace['id']}/sync/{failed_run['id']}/retry",
        headers=headers,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "completed"
    assert retried.json()["attempt"] == 2

    # A failed snapshot cannot be replayed after the workspace changes.
    monkeypatch.setattr(workspace_router, "_apply_sync_entity", fail)
    stale_failed = client.post(
        f"/api/workspaces/{workspace['id']}/sync",
        json={"idempotency_key": uuid.uuid4().hex},
        headers=headers,
    ).json()
    monkeypatch.setattr(workspace_router, "_apply_sync_entity", original)
    assert stale_failed["status"] == "failed"

    changed = client.patch(
        f"/api/workspaces/{workspace['id']}",
        json={"name": "Sync retry changed"},
        headers=headers,
    )
    assert changed.status_code == 200, changed.text
    stale_retry = client.post(
        f"/api/workspaces/{workspace['id']}/sync/{stale_failed['id']}/retry",
        headers=headers,
    )
    assert stale_retry.status_code == 409, stale_retry.text


def test_workspace_sync_reconciles_interrupted_run(client, superuser_db):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Sync interrupted", "status": "active"},
        headers=headers,
    ).json()
    founder_id = _founder_id(superuser_db)
    interrupted = WorkspaceSyncRun(
        project_id=uuid.UUID(workspace["id"]),
        owner_id=founder_id,
        idempotency_key="interrupted-run",
        status=WorkspaceSyncStatus.running,
        snapshot_hash="0" * 64,
        snapshot_version=1,
        progress_total=1,
        started_at=datetime.utcnow() - timedelta(minutes=30),
        heartbeat_at=datetime.utcnow() - timedelta(minutes=30),
    )
    superuser_db.add(interrupted)
    superuser_db.commit()

    new_run = client.post(
        f"/api/workspaces/{workspace['id']}/sync",
        json={"idempotency_key": uuid.uuid4().hex},
        headers=headers,
    )
    assert new_run.status_code == 200, new_run.text
    assert new_run.json()["status"] == "completed"
    superuser_db.refresh(interrupted)
    assert interrupted.status == WorkspaceSyncStatus.failed
    assert interrupted.error_category == "interrupted"


def test_workspace_supervised_tasks_include_real_evidence_and_owner_scope(
    client, superuser_db, make_verified_user
):
    csrf = _login(client)
    headers = _headers(csrf)
    workspace = client.post(
        "/api/workspaces",
        json={"name": "Supervised workspace", "status": "active"},
        headers=headers,
    ).json()
    goal = client.post(
        "/api/mainai/execution/goals",
        json={
            "title": "Granska workspace",
            "original_instruction": "Granska workspace.",
            "project_id": workspace["id"],
        },
        headers=headers,
    )
    assert goal.status_code == 201, goal.text
    goal_body = goal.json()
    assert goal_body["project_id"] == workspace["id"]

    founder_id = _founder_id(superuser_db)
    plan = MainAIPlan(
        goal_id=uuid.UUID(goal_body["id"]),
        owner_id=founder_id,
        version=1,
        status=MainAIPlanStatus.active,
        rationale="testplan",
        created_by="founder",
    )
    superuser_db.add(plan)
    superuser_db.flush()
    task = MainAITask(
        goal_id=uuid.UUID(goal_body["id"]),
        plan_id=plan.id,
        owner_id=founder_id,
        description="Granska källorna",
        task_type="read_only_audit",
        status=MainAITaskStatus.ready,
        priority=1,
        risk_level=MainAIGoalRiskLevel.low,
        approval_required=True,
        verification_plan=[{"kind": "targeted_tests", "target": "tests/backend"}],
        attempts=0,
        max_attempts=3,
    )
    superuser_db.add(task)
    superuser_db.flush()
    superuser_db.add(
        MainAITaskEvent(
            task_id=task.id,
            owner_id=founder_id,
            event_type=MainAITaskEventType.verification_passed,
            detail={"targeted_tests": "passed"},
        )
    )
    superuser_db.add(
        MainAICheckpoint(
            goal_id=uuid.UUID(goal_body["id"]),
            task_id=task.id,
            owner_id=founder_id,
            plan_version=1,
            executor_state={"step": "verified"},
            test_status="passed",
            ci_status="not_run",
        )
    )
    superuser_db.commit()

    supervised = client.get(f"/api/workspaces/{workspace['id']}/supervised-tasks")
    assert supervised.status_code == 200, supervised.text
    body = supervised.json()
    assert [item["id"] for item in body["goals"]] == [goal_body["id"]]
    assert len(body["tasks"]) == 1
    assert body["tasks"][0]["task"]["id"] == str(task.id)
    assert body["tasks"][0]["verification_evidence"][0]["event_type"] == "verification_passed"
    assert body["tasks"][0]["checkpoints"][0]["test_status"] == "passed"

    approved = client.post(
        f"/api/mainai/execution/tasks/{task.id}/approve", headers=headers
    )
    assert approved.status_code == 200, approved.text
    approved_body = client.get(f"/api/workspaces/{workspace['id']}/supervised-tasks").json()
    assert approved_body["tasks"][0]["approval_granted"] is True

    cancelled = client.post(f"/api/mainai/execution/tasks/{task.id}/cancel", headers=headers)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    other_user, _ = make_verified_user(email="workspace-supervised-other@example.com")
    foreign_goal = MainAIGoal(
        owner_id=other_user.id,
        project_id=uuid.UUID(workspace["id"]),
        title="Annan ägares goal",
        original_instruction="Ska inte synas",
        status="pending",
        risk_level=MainAIGoalRiskLevel.low,
        approval_policy="standard_repo_work",
        created_by="other",
    )
    superuser_db.add(foreign_goal)
    superuser_db.commit()
    scoped = client.get(f"/api/workspaces/{workspace['id']}/supervised-tasks").json()
    assert all(item["id"] != str(foreign_goal.id) for item in scoped["goals"])
