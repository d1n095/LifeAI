"""Runtime incident persistence, grouping, sanitization and owner API."""

from sqlalchemy.orm import sessionmaker

from app.incidents.service import (
    analyze_incident,
    record_incident,
    record_incident_standalone,
    sanitize_details,
)
from app.models.runtime_incident import RuntimeIncident
from app.models.user import User

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


def _login(client) -> str:
    response = client.post(
        "/api/auth/login",
        json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_incident_groups_repeats_and_survives_a_new_session(client, superuser_db):
    founder = superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).one()
    first = record_incident(
        superuser_db,
        owner_id=founder.id,
        error_code="provider_timeout",
        component="chat.provider",
        severity="high",
        summary="Chat provider misslyckades",
        correlation_id="corr-1",
        details={"provider": "openai", "model": "gpt-test", "error_category": "unreachable"},
    )
    superuser_db.commit()
    second = record_incident(
        superuser_db,
        owner_id=founder.id,
        error_code="provider_timeout",
        component="chat.provider",
        severity="critical",
        summary="Chat provider misslyckades",
        correlation_id="corr-2",
        details={"provider": "openai", "model": "gpt-test", "error_category": "unreachable"},
    )
    superuser_db.commit()

    assert first.id == second.id
    assert second.occurrence_count == 2
    assert second.severity == "critical"

    from app.db import migration_engine

    fresh = sessionmaker(bind=migration_engine)()
    try:
        rows = fresh.query(RuntimeIncident).filter_by(owner_id=founder.id).all()
        assert len(rows) == 1
        assert rows[0].occurrence_count == 2
        assert rows[0].analysis["known_cause"] == "provider_transport_or_capacity"
    finally:
        fresh.close()


def test_standalone_incident_is_committed_outside_the_caller_transaction(client, superuser_db):
    from app.request_context import current_user_id

    founder = superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).one()
    # Simulate a previous request's identity leaking through the contextvar. The helper must
    # scope RLS to the incident's owner, not inherit that stale request identity.
    token = current_user_id.set("00000000-0000-0000-0000-000000000099")
    try:
        row = record_incident_standalone(
            owner_id=founder.id,
            error_code="standalone_probe",
            component="test.incident",
            severity="high",
            summary="Standalone probe",
            correlation_id="standalone-corr",
        )
    finally:
        current_user_id.reset(token)
    assert row is not None

    superuser_db.expire_all()
    found = superuser_db.query(RuntimeIncident).filter_by(id=row.id).one()
    assert found.error_code == "standalone_probe"
    assert found.occurrence_count == 1


def test_incident_details_drop_secrets_and_content():
    clean = sanitize_details(
        {
            "provider": "openai",
            "model": "gpt-test",
            "api_key": "sk-secret",
            "prompt": "private text",
            "message": "private reply",
            "authorization": "Bearer secret",
            "nested": {"content": "secret"},
            "job_id": "job-1",
        }
    )
    assert clean == {"provider": "openai", "model": "gpt-test", "job_id": "job-1"}


def test_incident_analysis_has_safe_recovery_without_auto_action():
    analysis, options = analyze_incident(
        error_code="provider_timeout", component="workforce.provider_worker", severity="high"
    )
    assert analysis["automatic_action"] == "none"
    assert analysis["known_cause"] == "provider_transport_or_capacity"
    assert all(option["requires_owner"] is True for option in options)


def test_incident_api_is_owner_scoped_and_can_be_acknowledged(client, superuser_db):
    founder = superuser_db.query(User).filter_by(email=FOUNDER_EMAIL).one()
    row = record_incident(
        superuser_db,
        owner_id=founder.id,
        error_code="worker_unexpected",
        component="worker.mainai_job",
        severity="high",
        summary="Worker misslyckades med ett MainAI-jobb",
        correlation_id="job-123",
    )
    superuser_db.commit()

    headers = {"X-CSRF-Token": _login(client)}
    listed = client.get("/api/incidents", headers=headers)
    assert listed.status_code == 200, listed.text
    ids = [item["id"] for item in listed.json()]
    assert str(row.id) in ids

    acknowledged = client.post(f"/api/incidents/{row.id}/acknowledge", headers=headers)
    assert acknowledged.status_code == 200, acknowledged.text
    assert acknowledged.json()["status"] == "acknowledged"

    resolved = client.post(f"/api/incidents/{row.id}/resolve", headers=headers)
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["status"] == "resolved"
