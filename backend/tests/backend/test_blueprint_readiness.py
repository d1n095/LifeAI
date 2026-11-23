"""Founder Alpha Area 3: the blueprint readiness cross-reference view.

Read-only aggregation over EXISTING evidence tables (documents, project_sources,
project_branch_pr_statuses, engineering_lessons, agent_tasks) — no new tables, no
second blueprint engine. The upstream blueprint_drafts stack joins here later via
source_document_ids.
"""

import uuid
import warnings
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models.agent_task  # noqa: F401
import app.models.document  # noqa: F401
import app.models.mainai_execution  # noqa: F401
import app.models.project_memory  # noqa: F401
from app.blueprint_readiness import collect_blueprint_readiness
from app.db import Base
from app.models.agent_task import AgentTask
from app.models.document import ActiveTruthStatus, Document, DocumentSource, KnowledgeClassification
from app.models.mainai_execution import EngineeringLesson, EngineeringLessonSeverity, EngineeringLessonStatus
from app.models.project_memory import ProjectBranchPRStatus, ProjectSource

warnings.filterwarnings("ignore")


@compiles(UUID, "sqlite")
def _uuid_sqlite(t, c, **kw):
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(t, c, **kw):
    return "JSON"


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    yield session
    session.close()
    engine.dispose()


def _doc(db, title, status=ActiveTruthStatus.active, classification=KnowledgeClassification.decisions):
    d = Document(title=title, classification=classification, active_truth_status=status,
                 content_preview="x", source=DocumentSource.upload)
    db.add(d)
    db.commit()
    return d


def test_readiness_joins_documents_code_lessons_and_tasks(db):
    _doc(db, "Krav: inloggning")
    _doc(db, "Gammalt beslut", status=ActiveTruthStatus.superseded)
    db.add(ProjectSource(source_type="github_pr", source_ref="#8", commit_sha="a" * 40,
                         ingested_by="agent", ingested_at=datetime.utcnow()))
    db.add(ProjectSource(source_type="git_commit", source_ref="HEAD", commit_sha="b" * 40,
                         ingested_by="agent", ingested_at=datetime.utcnow()))
    db.add(EngineeringLesson(status=EngineeringLessonStatus.active, problem="p", root_cause="r",
                             affected_component="auth.login", severity=EngineeringLessonSeverity.medium,
                             evidence="e", fix="f", regression_test="tests/test_login.py::test_x",
                             verification_status="verified", source_type="postmortem", source_ref="doc:1",
                             resolved_in="PR #8", general_rule="gr", first_seen_at=datetime.utcnow(),
                             created_by="grundare"))
    db.add(EngineeringLesson(status=EngineeringLessonStatus.active, problem="p2", root_cause="r2",
                             affected_component="auth.login", severity=EngineeringLessonSeverity.low,
                             evidence="e2", fix="f2", verification_status="unverified", general_rule="gr", first_seen_at=datetime.utcnow(), created_by="grundare",
                             source_type="postmortem", source_ref="doc:2"))
    db.add(AgentTask(title="Implementera inloggning", description="d", target_files=["src/auth.py"],
                     acceptance_criteria="AC", required_tests="tests/test_login.py", created_by="grundare"))
    db.commit()

    out = collect_blueprint_readiness(db)
    s = out["summary"]
    assert s["documents"] == 2
    assert s["documents_superseded_or_disputed"] == 1
    assert s["code_refs"] == 2
    assert s["active_lessons"] == 2
    assert s["unverified_lessons"] == 1
    assert s["open_next_actions"] == 1
    assert s["components_tracked"] == 1
    comp = out["component_index"]["auth.login"]
    assert comp["verdict_counts"] == {"verified": 1, "unverified": 1, "other": 0}
    assert comp["regression_tests"] == ["tests/test_login.py::test_x"]
    assert comp["resolved_in"] == ["PR #8"]
    assert out["next_actions"][0]["target_files"] == ["src/auth.py"]
    assert {d["truth_status"] for d in out["documents"]} == {"active", "superseded"}


def test_only_current_pr_snapshots_surface(db):
    db.add(ProjectBranchPRStatus(kind="pr", ref="#8", title="old", status="open",
                                 base_ref="main", head_ref="feat", ci_status="pending",
                                 is_current=False, recorded_at=datetime.utcnow() - timedelta(hours=1), recorded_by="agent"))
    db.add(ProjectBranchPRStatus(kind="pr", ref="#8", title="new", status="open",
                                 base_ref="main", head_ref="feat", ci_status="success",
                                 is_current=True, recorded_at=datetime.utcnow(), recorded_by="agent"))
    db.commit()
    out = collect_blueprint_readiness(db)
    assert len(out["pr_status"]) == 1
    assert out["pr_status"][0]["ci_status"] == "success"


def test_empty_database_yields_empty_sections_never_fabricated_links(db):
    out = collect_blueprint_readiness(db)
    assert out["summary"]["documents"] == 0 and out["code_refs"] == []
    assert out["component_index"] == {} and out["next_actions"] == []


def test_doc_source_refs_join_key_matches_upstream_blueprint_payload(db):
    """The attachment point for upstream blueprint_drafts.source_document_ids: every
    document entry carries its document_id in UUID string form."""
    d = _doc(db, "Krav")
    out = collect_blueprint_readiness(db)
    assert out["documents"][0]["document_id"] == str(d.id)
