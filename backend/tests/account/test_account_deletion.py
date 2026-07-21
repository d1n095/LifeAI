from app.models.conversation import Conversation, Message
from app.models.document import ActiveTruthStatus, Document, DocumentSource
from app.models.document_chunk import DocumentChunk
from app.models.import_job import ImportJob, ImportJobStatus
from app.models.knowledge_version import KnowledgeVersion
from app.models.project import Project
from app.models.refresh_token import RefreshToken
from app.models.source_relationship import RelationshipType, SourceRelationship
from app.models.user import User


def _login_and_get_csrf(client, email: str, password: str) -> str:
    res = client.post("/api/auth/login", json={"email": email, "password": password})
    assert res.status_code == 200
    return res.json()["csrf_token"]


def test_export_includes_own_data_only(client, db_session, superuser_db, make_verified_user):
    user, password = make_verified_user(email="exportme@example.com")
    _login_and_get_csrf(client, "exportme@example.com", password)

    # Created directly against the DB, not via POST /api/projects: that route is
    # founder-only now (see app/deps.py's require_founder) — this test is about what
    # /api/account/export includes for a logged-in user, not about who may create projects.
    superuser_db.add(Project(name="shared project", status="active", created_by=user.id))
    superuser_db.commit()

    res = client.get("/api/account/export")
    assert res.status_code == 200
    body = res.json()
    assert body["account"]["email"] == "exportme@example.com"
    assert "conversations" in body
    assert "audit_log" in body
    # Shared company data (projects/tasks/documents) is deliberately out of scope — see
    # app/routers/account.py.
    assert "projects" not in body


def test_export_includes_own_knowledge_sources_versions_and_relationships(client, superuser_db, make_verified_user):
    """DEL 11 of the Founder Knowledge Studio work order: export must include source
    metadata, document versions, and relationships — not just conversations/audit_log.
    Also confirms a second user's material never leaks into this export (isolation)."""
    user, password = make_verified_user(email="exportknowledge@example.com")
    other, _ = make_verified_user(email="exportknowledge-other@example.com")
    _login_and_get_csrf(client, "exportknowledge@example.com", password)

    doc_a = Document(
        title="Källa A",
        source=DocumentSource.upload,
        uploaded_by=user.id,
        classification="architecture",
        active_truth_status=ActiveTruthStatus.historical,
        checksum="a" * 64,
    )
    doc_b = Document(title="Källa B", source=DocumentSource.upload, uploaded_by=user.id, checksum="b" * 64)
    other_doc = Document(title="Någon annans källa", source=DocumentSource.upload, uploaded_by=other.id, checksum="c" * 64)
    superuser_db.add_all([doc_a, doc_b, other_doc])
    superuser_db.commit()

    superuser_db.add(
        KnowledgeVersion(source_id=doc_a.id, owner_id=user.id, version_number=1, checksum="a" * 64, extraction_version="extract-v1")
    )
    superuser_db.add(
        SourceRelationship(
            owner_id=user.id, from_source_id=doc_b.id, to_source_id=doc_a.id, relationship_type=RelationshipType.supersedes
        )
    )
    superuser_db.add(ImportJob(owner_id=user.id, status=ImportJobStatus.completed, source_filename="paket.zip", succeeded_count=2))
    superuser_db.commit()

    res = client.get("/api/account/export")
    assert res.status_code == 200
    body = res.json()

    source_ids = {s["id"] for s in body["knowledge_sources"]}
    assert str(doc_a.id) in source_ids
    assert str(doc_b.id) in source_ids
    assert str(other_doc.id) not in source_ids  # isolation: never another founder's material

    source_a = next(s for s in body["knowledge_sources"] if s["id"] == str(doc_a.id))
    assert source_a["active_truth_status"] == "historical"
    assert len(source_a["versions"]) == 1
    assert source_a["versions"][0]["extraction_version"] == "extract-v1"

    assert len(body["knowledge_source_relationships"]) == 1
    assert body["knowledge_source_relationships"][0]["relationship_type"] == "supersedes"

    assert len(body["knowledge_import_jobs"]) == 1
    assert body["knowledge_import_jobs"][0]["succeeded_count"] == 2


def test_wrong_password_does_not_delete(client, db_session, make_verified_user):
    user, password = make_verified_user(email="wrongpassdelete@example.com")
    csrf = _login_and_get_csrf(client, "wrongpassdelete@example.com", password)

    res = client.request("DELETE", "/api/account", json={"password": "not-the-real-password"}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 403
    assert db_session.query(User).filter_by(email="wrongpassdelete@example.com").count() == 1


def test_correct_password_deletes_permanently_and_scrubs_shared_data(client, db_session, superuser_db, make_verified_user):
    user, password = make_verified_user(email="deleteme@example.com")
    user_id = user.id
    csrf = _login_and_get_csrf(client, "deleteme@example.com", password)

    # Created directly against the DB, not via POST /api/projects (founder-only now) — this
    # test is about account-deletion's data-scrubbing behavior, not project creation.
    project = Project(name="orphaned project", status="active", created_by=user_id)
    superuser_db.add(project)
    superuser_db.commit()
    project_id = project.id

    res = client.request("DELETE", "/api/account", json={"password": password}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200

    assert db_session.query(User).filter_by(id=user_id).first() is None
    assert db_session.query(RefreshToken).filter_by(user_id=user_id).count() == 0

    # Shared company data survives, attribution scrubbed rather than deleted.
    project = db_session.query(Project).filter_by(id=project_id).first()
    assert project is not None
    assert project.created_by is None

    # The session is dead — no lingering access.
    assert client.get("/api/auth/me").status_code == 401


def test_deleted_account_conversations_are_gone(client, superuser_db, make_verified_user):
    """Uses superuser_db (bypasses RLS) to check: db_session (the restricted runtime role)
    would report zero rows for the OTHER user too, since it has no app.current_user_id set
    for this ad-hoc test session — that would be a false pass, not proof of real deletion."""
    user, password = make_verified_user(email="deleteconvos@example.com")
    user_id = user.id
    csrf = _login_and_get_csrf(client, "deleteconvos@example.com", password)

    conversation = Conversation(user_id=user_id, title="to be deleted")
    superuser_db.add(conversation)
    superuser_db.commit()
    conversation_id = conversation.id  # captured now — the row (and this object) won't survive the delete below
    superuser_db.add(Message(conversation_id=conversation_id, role="user", content="hej"))
    superuser_db.commit()

    client.request("DELETE", "/api/account", json={"password": password}, headers={"X-CSRF-Token": csrf})

    assert superuser_db.query(Conversation).filter_by(user_id=user_id).count() == 0
    assert superuser_db.query(Message).filter_by(conversation_id=conversation_id).count() == 0


def test_login_after_deletion_fails_with_generic_message(client, make_verified_user):
    user, password = make_verified_user(email="goneuser@example.com")
    csrf = _login_and_get_csrf(client, "goneuser@example.com", password)
    client.request("DELETE", "/api/account", json={"password": password}, headers={"X-CSRF-Token": csrf})

    res = client.post("/api/auth/login", json={"email": "goneuser@example.com", "password": password})
    assert res.status_code == 401


def test_deletion_is_rolled_back_atomically_on_mid_transaction_failure(
    client, db_session, superuser_db, make_verified_user, monkeypatch
):
    """Forces a failure partway through the deletion sequence (after some deletes/updates
    have already been issued against the session, before the final commit) and verifies the
    whole transaction rolls back cleanly — no half-deleted account, matching
    docs/SECURITY_BLOCKERS.md's transactional-deletion guarantee."""
    user, password = make_verified_user(email="rollbackme@example.com")
    user_id = user.id
    csrf = _login_and_get_csrf(client, "rollbackme@example.com", password)

    conversation = Conversation(user_id=user_id, title="should survive the rollback")
    superuser_db.add(conversation)
    superuser_db.commit()

    # Created directly against the DB, not via POST /api/projects (founder-only now) — see
    # the comment in test_correct_password_deletes_permanently_and_scrubs_shared_data above.
    project = Project(name="should stay attributed", status="active", created_by=user_id)
    superuser_db.add(project)
    superuser_db.commit()
    project_id = project.id

    from sqlalchemy.orm import Session as SASession

    original_delete = SASession.delete

    def _failing_delete(self, instance):
        if isinstance(instance, User):
            # Everything else (conversations, refresh tokens, ...) has already been queued
            # for deletion on this Session by this point — this is where the real endpoint
            # finally deletes the User row itself, right before commit.
            raise RuntimeError("simulated database failure during account deletion")
        return original_delete(self, instance)

    monkeypatch.setattr(SASession, "delete", _failing_delete)

    res = client.request("DELETE", "/api/account", json={"password": password}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 500

    monkeypatch.undo()

    # Nothing was actually removed — the whole transaction rolled back.
    assert db_session.query(User).filter_by(id=user_id).first() is not None
    assert superuser_db.query(Conversation).filter_by(id=conversation.id).first() is not None
    assert db_session.query(RefreshToken).filter_by(user_id=user_id).count() >= 1

    project = db_session.query(Project).filter_by(id=project_id).first()
    assert project is not None
    assert project.created_by == user_id  # NOT scrubbed — the update was rolled back too

    # The account is still fully usable afterward — the failure didn't corrupt state.
    me = client.get("/api/auth/me")
    assert me.status_code == 200


def test_deletion_removes_documents_and_knowledge_data_outright(client, superuser_db, make_verified_user):
    """Regression test: migration 0006 (Founder Knowledge Studio v1) made `documents`
    RLS-protected and owner-scoped, which means the old anonymize-uploaded_by-to-NULL
    behavior this endpoint used to have would now be rejected outright by the
    documents_isolation policy's WITH CHECK (NULL never matches any current_user_id) —
    see app/routers/account.py's delete_account(). Confirms both that account deletion still
    succeeds (doesn't crash under the new RLS policy) AND that documents/chunks/versions/
    import jobs are genuinely gone afterward, not silently left as orphaned, permanently
    RLS-hidden rows."""
    user, password = make_verified_user(email="deleteme-with-docs@example.com")
    user_id = user.id
    csrf = _login_and_get_csrf(client, "deleteme-with-docs@example.com", password)

    from app.config import get_settings

    embedding_dim = get_settings().embedding_dim

    document = Document(title="Mitt dokument", source=DocumentSource.upload, uploaded_by=user_id)
    superuser_db.add(document)
    superuser_db.commit()
    document_id = document.id

    superuser_db.add(
        DocumentChunk(
            document_id=document_id, owner_id=user_id, chunk_index=0, text="chunk", embedding=[0.1] * embedding_dim
        )
    )
    superuser_db.add(
        KnowledgeVersion(source_id=document_id, owner_id=user_id, version_number=1, checksum="c" * 64, extraction_version="v1")
    )
    superuser_db.add(ImportJob(owner_id=user_id, status=ImportJobStatus.completed, source_filename="test.zip"))
    superuser_db.commit()

    res = client.request("DELETE", "/api/account", json={"password": password}, headers={"X-CSRF-Token": csrf})
    assert res.status_code == 200, res.text

    assert superuser_db.query(Document).filter_by(id=document_id).first() is None
    assert superuser_db.query(DocumentChunk).filter_by(document_id=document_id).count() == 0
    assert superuser_db.query(KnowledgeVersion).filter_by(source_id=document_id).count() == 0
    assert superuser_db.query(ImportJob).filter_by(owner_id=user_id).count() == 0
