"""Project detail contract: one project, only its own tasks, and only the founder's own live
knowledge sources. The source query is deliberately checked with a foreign-owner row and a
soft-deleted row in the same project — a global library response rendered as project-scoped
would pass a weaker "the expected row exists" assertion while still leaking the wrong rows.
"""

import uuid
from datetime import datetime

from app.models.document import Document, DocumentSource
from app.models.user import User

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200, res.text
    return res.json()["csrf_token"]


def _founder_id(superuser_db) -> uuid.UUID:
    return superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).first().id


def _make_document(superuser_db, *, owner_id, project_id, title, deleted=False) -> Document:
    document = Document(
        title=title,
        source=DocumentSource.upload,
        uploaded_by=owner_id,
        project_id=project_id,
        checksum=uuid.uuid4().hex,
        deleted_at=datetime.utcnow() if deleted else None,
    )
    superuser_db.add(document)
    superuser_db.commit()
    return document


def test_project_detail_returns_only_its_tasks_and_owner_visible_sources(
    client, superuser_db, make_verified_user
):
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    project = client.post(
        "/api/projects",
        json={"name": "Projekt detalj", "description": "En riktig projektvy", "status": "active"},
        headers=headers,
    )
    assert project.status_code == 200, project.text
    project_body = project.json()
    project_id = uuid.UUID(project_body["id"])

    other_project = client.post(
        "/api/projects",
        json={"name": "Annat projekt", "status": "active"},
        headers=headers,
    )
    assert other_project.status_code == 200, other_project.text

    own_task = client.post(
        "/api/projects/tasks",
        json={"title": "Uppgift i projektet", "project_id": project_body["id"], "status": "todo"},
        headers=headers,
    )
    assert own_task.status_code == 200, own_task.text
    other_task = client.post(
        "/api/projects/tasks",
        json={"title": "Uppgift i annat projekt", "project_id": other_project.json()["id"]},
        headers=headers,
    )
    assert other_task.status_code == 200, other_task.text
    unassigned_task = client.post(
        "/api/projects/tasks",
        json={"title": "Uppgift utan projekt"},
        headers=headers,
    )
    assert unassigned_task.status_code == 200, unassigned_task.text

    founder_id = _founder_id(superuser_db)
    other_user, _ = make_verified_user(email="project-detail-other@example.com")
    live_source = _make_document(
        superuser_db, owner_id=founder_id, project_id=project_id, title="Live källa"
    )
    _make_document(
        superuser_db, owner_id=founder_id, project_id=project_id, title="Raderad källa", deleted=True
    )
    _make_document(
        superuser_db, owner_id=other_user.id, project_id=project_id, title="Annan ägares källa"
    )

    response = client.get(f"/api/projects/{project_body['id']}")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["id"] == project_body["id"]
    assert body["name"] == "Projekt detalj"
    assert [task["title"] for task in body["tasks"]] == ["Uppgift i projektet"]
    assert [source["id"] for source in body["sources"]] == [str(live_source.id)]


def test_project_detail_is_founder_only(client):
    response = client.get(f"/api/projects/{uuid.uuid4()}")
    assert response.status_code in (401, 403)


def test_project_detail_unknown_project_is_404(client):
    _login(client)
    response = client.get(f"/api/projects/{uuid.uuid4()}")
    assert response.status_code == 404
