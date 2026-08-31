from __future__ import annotations
import uuid
import pytest
from sqlalchemy import text as sa_text
from app.mainai_executive.dashboard import founder_executive_dashboard
from app.mainai_executive.retrieval_quality import run_retrieval_quality_suite
from app.models.user import User
from app.request_context import current_user_id as current_user_id_var

@pytest.fixture(autouse=True, scope="module")
def _priv():
    from app.db import migration_engine
    from app.rls import apply_mainai_execution_privileges
    apply_mainai_execution_privileges(migration_engine)

def test_retrieval_quality_suite(superuser_db):
    u = User(email=f"rq-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(u); superuser_db.flush()
    current_user_id_var.set(str(u.id))
    superuser_db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(u.id)})
    report = run_retrieval_quality_suite(superuser_db, owner_id=u.id)
    d = report.as_dict()
    assert d["failure_to_find_is_information"] is True
    assert d["hallucinated_match"] is False
    assert d["cases"] >= 8

def test_founder_dashboard(superuser_db):
    u = User(email=f"dash-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    superuser_db.add(u); superuser_db.flush()
    current_user_id_var.set(str(u.id))
    superuser_db.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(u.id)})
    dash = founder_executive_dashboard(superuser_db, owner_id=u.id)
    assert dash["chain_of_thought_exposed"] is False
    assert "WHAT_SHE_IS_DOING" in dash
    assert dash["authority_state"]["executive_holds_execution_authority"] is False
