"""STEG 7 of the Founder Knowledge Studio work order: "råa databasfel" — a raw DB
exception raised inside a new /api/library route must never reach the HTTP response body.
Uses a dedicated TestClient with raise_server_exceptions=False (the default `client` fixture
in conftest.py has it True, which is right for every other test — an unhandled exception
should fail the TEST loudly, not silently become a 500 — but is exactly wrong for this one
test, which needs to see what a REAL deployed server would actually send back on an
unhandled 500)."""

import psycopg2
import pytest
from fastapi.testclient import TestClient

from app.main import app

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


@pytest.fixture
def raw_error_client(sent_emails):
    # Depends on conftest.py's sent_emails fixture (fakes outbound mail) and the autouse
    # _test_database/_clean_tables fixtures for the real Postgres backing it — same
    # dependencies the `client` fixture has, just with raise_server_exceptions off.
    with TestClient(app, base_url="https://testserver", raise_server_exceptions=False) as c:
        yield c


def test_raw_db_exception_in_library_list_never_leaks_into_the_response(raw_error_client, monkeypatch):
    login = raw_error_client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert login.status_code == 200

    secret_detail = "FATAL: password authentication failed for user \"mainai_app\" at host db-internal.private:5432"

    def _boom(db, owner_id):
        raise psycopg2.OperationalError(secret_detail)

    monkeypatch.setattr("app.routers.library._visible_document_query", _boom)

    res = raw_error_client.get("/api/library")
    assert res.status_code == 500

    body_text = res.text
    assert secret_detail not in body_text
    assert "psycopg2" not in body_text
    assert "Traceback" not in body_text
    assert "db-internal.private" not in body_text
