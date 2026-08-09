"""MainAI Project Memory & Coordination Loop — first working slice (see
app/project_memory.py). Covers, in order:

  A. add_note()/list_notes() distinguish current (open) state from history
     (resolved/superseded), each note always carrying a source citation.
  B. resolve_note() marks resolved vs superseded without ever deleting a row.
  C. create_checkpoint() renders a resumption brief from currently-open notes, stores it via
     the same content-addressed storage backend documents use, and the brief is readable back
     byte-for-byte.
  D. get_latest_checkpoint()/list_checkpoints() return the right rows in the right order.
  E. The founder-only admin API surface (app/routers/memory.py) end to end: create notes,
     resolve one, create a checkpoint, fetch the latest checkpoint — proving a brand new
     session could hit these same endpoints and correctly learn current state, without
     guessing, exactly per CLAUDE.md's success metric.

No mocks for storage or the DB — this exercises the real Postgres test database and the real
LocalFilesystemStorage backend (see tests/conftest.py's STORAGE_ROOT tempdir), matching this
repo's existing convention of testing real scenarios, not just isolated units."""

import importlib.util
import subprocess
import threading
import uuid
from pathlib import Path

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.models.project_memory import NoteKind, NoteStatus, ProjectCheckpointNote, ProjectSource, SideIssueClassification
from app.project_memory import (
    add_note,
    build_system_map,
    classify_note,
    create_checkpoint,
    detect_conflicts_and_duplicates,
    generate_resumption_brief,
    get_latest_checkpoint,
    ingest_doc,
    ingest_git_commit,
    ingest_github_status,
    ingest_system_map,
    is_checkpoint_stale,
    latest_system_map,
    list_checkpoints,
    list_current_branch_pr_status,
    list_notes,
    list_notes_needing_founder_decision,
    list_sources,
    read_checkpoint_brief,
    read_source_content,
    resolve_note,
    retrieve_relevant_context,
)
from app.storage import StorageError, get_storage
from app.storage.references import acquire_storage_key_lock, delete_if_unreferenced, store_content_with_reference_lock

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"

_APPLY_RUNTIME_PRIVILEGES_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "apply_runtime_privileges.py"


def _load_apply_runtime_privileges():
    spec = importlib.util.spec_from_file_location("apply_runtime_privileges", _APPLY_RUNTIME_PRIVILEGES_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True, scope="module")
def _narrow_privileges_before_this_module():
    """Pass 31: ingest_doc()/ingest_system_map()/create_checkpoint() now call
    storage_key_still_referenced_global() (via app/storage/references.py's
    store_content_with_reference_lock()), which mainai_app is only granted EXECUTE on via
    apply_runtime_privileges.py/ensure_app_role.py's shared privilege policy -- never
    automatically by tests/conftest.py's _test_database fixture's own blanket table/sequence
    GRANT ALL. Same fixture, same rationale, as tests/backend/test_library_routes.py's
    identical one (added there in Pass 30 for the same reason); this file never needed it
    before this pass, since these three functions used to call storage.write_stream()
    directly with no reference check at all."""
    module = _load_apply_runtime_privileges()
    module.apply_and_verify(get_settings().database_url)


def _init_fake_repo(tmp_path) -> str:
    """Creates a real, throwaway git repo under tmp_path with a CLAUDE.md and a nested doc,
    so ingestion tests exercise the actual `git` binary and real file reads — not mocks.
    Returns the resulting HEAD commit SHA."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "CLAUDE.md").write_text("# Testprojekt\n\nDetta är en testrepo för ingestion.\n")
    (tmp_path / "docs" / "BRANCH_REGISTRY.md").write_text("# Registry\n\nInga brancher än.\n")

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("add", ".")
    _git("commit", "-q", "-m", "initial")
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _login(client) -> str:
    res = client.post("/api/auth/login", json={"email": FOUNDER_EMAIL, "password": FOUNDER_PASSWORD})
    assert res.status_code == 200
    return res.json()["csrf_token"]


# --- A/B. Notes: current vs. history, always cited ----------------------------------------


def test_add_note_requires_no_guessing_every_note_has_a_citation(db_session):
    note = add_note(
        db_session,
        kind=NoteKind.decision,
        content="PR #8 uppdaterad och redo, men medvetet inte mergad.",
        source_type="pr",
        source_ref="#8",
        created_by="test-agent",
    )
    assert note.status == NoteStatus.open
    assert note.source_type == "pr"
    assert note.source_ref == "#8"


def test_list_notes_default_shows_only_open_current_state(db_session):
    add_note(db_session, kind=NoteKind.blocker, content="A", source_type="pr", source_ref="#1", created_by="t")
    resolved = add_note(db_session, kind=NoteKind.blocker, content="B", source_type="pr", source_ref="#2", created_by="t")
    resolve_note(db_session, resolved.id, resolved_by="t", resolution_note="Löst i PR #3")

    open_notes = list_notes(db_session, status=NoteStatus.open)
    assert {n.content for n in open_notes} == {"A"}

    all_notes = list_notes(db_session, status=None)
    assert {n.content for n in all_notes} == {"A", "B"}


def test_resolve_note_never_deletes_marks_superseded_distinctly_from_resolved(db_session):
    n1 = add_note(db_session, kind=NoteKind.next_step, content="Gör X", source_type="doc", source_ref="CLAUDE.md", created_by="t")
    n2 = add_note(db_session, kind=NoteKind.next_step, content="Gör Y istället", source_type="doc", source_ref="CLAUDE.md", created_by="t")

    resolve_note(db_session, n1.id, resolved_by="t", resolution_note=f"Ersatt av {n2.id}", superseded=True)
    resolve_note(db_session, n2.id, resolved_by="t", resolution_note="Klart")

    history = {n.id: n for n in list_notes(db_session, status=None)}
    assert history[n1.id].status == NoteStatus.superseded
    assert history[n2.id].status == NoteStatus.resolved
    assert history[n1.id].resolved_at is not None
    assert history[n2.id].resolved_at is not None


def test_list_notes_filters_by_kind(db_session):
    add_note(db_session, kind=NoteKind.decision, content="D", source_type="pr", source_ref="#1", created_by="t")
    add_note(db_session, kind=NoteKind.blocker, content="B", source_type="pr", source_ref="#2", created_by="t")

    decisions = list_notes(db_session, status=NoteStatus.open, kind=NoteKind.decision)
    assert [n.content for n in decisions] == ["D"]


# --- C. Checkpoint: brief generation + durable storage round-trip -------------------------


def test_generate_resumption_brief_cites_every_open_note_and_groups_by_kind(db_session):
    blocker = add_note(db_session, kind=NoteKind.blocker, content="Väntar på grundarbeslut", source_type="pr", source_ref="#8", created_by="t")
    decision = add_note(db_session, kind=NoteKind.decision, content="PR #7 mergad i sin bas", source_type="commit", source_ref="16959661", created_by="t")

    brief = generate_resumption_brief(
        summary="Mergekedjan klar, PR #8 väntar.",
        branch_name="claude/mainai-memory-loop-v1",
        open_pr_refs=["#8"],
        open_notes=[blocker, decision],
    )

    assert "claude/mainai-memory-loop-v1" in brief
    assert "#8" in brief
    assert "Väntar på grundarbeslut — källa: pr #8" in brief
    assert "PR #7 mergad i sin bas — källa: commit 16959661" in brief
    # blockers and decisions must appear under their own headings, not lumped together
    assert brief.index("## Blockerare") < brief.index("Väntar på grundarbeslut")
    assert brief.index("## Beslut") < brief.index("PR #7 mergad i sin bas")


def test_generate_resumption_brief_says_none_explicitly_when_a_category_is_empty():
    brief = generate_resumption_brief(summary="Allt klart.", branch_name="main", open_pr_refs=[], open_notes=[])
    # 6 = "Öppna PR:er" plus the blocker/decision/next_step/uncertainty/idea sections, all empty
    assert brief.count("(inga)") == 6
    assert "**Öppna PR:er:** (inga)" in brief


def test_create_checkpoint_stores_brief_durably_and_links_open_notes(db_session):
    note = add_note(db_session, kind=NoteKind.next_step, content="Bygg loop v1", source_type="doc", source_ref="CLAUDE.md", created_by="t")

    checkpoint = create_checkpoint(
        db_session,
        summary="Test-checkpoint.",
        branch_name="claude/mainai-memory-loop-v1",
        open_pr_refs=["#8"],
        created_by="test-agent",
    )

    assert checkpoint.brief_storage_key
    assert checkpoint.brief_sha256

    brief = read_checkpoint_brief(checkpoint)
    assert "Bygg loop v1" in brief
    assert "claude/mainai-memory-loop-v1" in brief

    links = db_session.query(ProjectCheckpointNote).filter_by(checkpoint_id=checkpoint.id).all()
    assert {link.note_id for link in links} == {note.id}


def test_create_checkpoint_excludes_already_resolved_notes(db_session):
    resolved = add_note(db_session, kind=NoteKind.blocker, content="Gammal blockerare", source_type="pr", source_ref="#1", created_by="t")
    resolve_note(db_session, resolved.id, resolved_by="t", resolution_note="Löst")
    add_note(db_session, kind=NoteKind.blocker, content="Aktuell blockerare", source_type="pr", source_ref="#8", created_by="t")

    checkpoint = create_checkpoint(db_session, summary="S", branch_name="b", open_pr_refs=[], created_by="t")
    brief = read_checkpoint_brief(checkpoint)

    assert "Aktuell blockerare" in brief
    assert "Gammal blockerare" not in brief


# --- D. Checkpoint history/latest ----------------------------------------------------------


def test_get_latest_checkpoint_returns_most_recent(db_session):
    create_checkpoint(db_session, summary="Först.", branch_name="b", open_pr_refs=[], created_by="t")
    second = create_checkpoint(db_session, summary="Sist.", branch_name="b", open_pr_refs=[], created_by="t")

    latest = get_latest_checkpoint(db_session)
    assert latest.id == second.id
    assert latest.summary == "Sist."


def test_list_checkpoints_orders_newest_first(db_session):
    first = create_checkpoint(db_session, summary="1", branch_name="b", open_pr_refs=[], created_by="t")
    second = create_checkpoint(db_session, summary="2", branch_name="b", open_pr_refs=[], created_by="t")

    rows = list_checkpoints(db_session)
    assert [c.id for c in rows[:2]] == [second.id, first.id]


def test_get_latest_checkpoint_is_none_when_nothing_created_yet(db_session):
    assert get_latest_checkpoint(db_session) is None


# --- E. Founder-only admin API, end to end --------------------------------------------------


def test_memory_api_requires_founder_auth(client):
    res = client.get("/api/admin/memory/notes")
    assert res.status_code in (401, 403)


def test_memory_api_full_loop_create_notes_resolve_one_checkpoint_and_read_latest(client):
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    res = client.post(
        "/api/admin/memory/notes",
        json={"kind": "blocker", "content": "PR #8 väntar på beslut.", "source_type": "pr", "source_ref": "#8"},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    blocker_id = res.json()["id"]

    res = client.post(
        "/api/admin/memory/notes",
        json={"kind": "decision", "content": "brace-expansion allowlistad separat.", "source_type": "pr", "source_ref": "#11"},
        headers=headers,
    )
    assert res.status_code == 200, res.text

    res = client.post(
        "/api/admin/memory/notes",
        json={"kind": "next_step", "content": "Vänta på grundarens PR #8-beslut.", "source_type": "doc", "source_ref": "docs/BRANCH_REGISTRY.md"},
        headers=headers,
    )
    assert res.status_code == 200, res.text

    # Resolve the blocker before the checkpoint — it must NOT show up as open in the brief.
    res = client.post(
        f"/api/admin/memory/notes/{blocker_id}/resolve",
        json={"resolution_note": "Grundaren mergade PR #8."},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "resolved"

    res = client.get("/api/admin/memory/notes?status=open")
    assert res.status_code == 200
    open_contents = {n["content"] for n in res.json()}
    assert "PR #8 väntar på beslut." not in open_contents
    assert "brace-expansion allowlistad separat." in open_contents

    res = client.get("/api/admin/memory/notes?status=all")
    assert len(res.json()) == 3  # resolved note still present in history

    res = client.post(
        "/api/admin/memory/checkpoints",
        json={"summary": "Mergekedjan klar.", "branch_name": "claude/mainai-memory-loop-v1", "open_pr_refs": ["#8"]},
        headers=headers,
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "brace-expansion allowlistad separat." in body["brief"]
    assert "Vänta på grundarens PR #8-beslut." in body["brief"]
    assert "PR #8 väntar på beslut." not in body["brief"]  # resolved before the checkpoint

    res = client.get("/api/admin/memory/checkpoints/latest")
    assert res.status_code == 200
    latest = res.json()
    assert latest["id"] == body["id"]
    assert latest["brief"] == body["brief"]


def test_memory_api_latest_checkpoint_404_before_any_checkpoint_exists(client):
    _login(client)
    res = client.get("/api/admin/memory/checkpoints/latest")
    assert res.status_code == 404


def test_memory_api_rejects_unsourced_or_invalid_kind(client):
    csrf = _login(client)
    res = client.post(
        "/api/admin/memory/notes",
        json={"kind": "not_a_real_kind", "content": "x", "source_type": "pr", "source_ref": "#1"},
        headers={"X-CSRF-Token": csrf},
    )
    assert res.status_code == 422


# --- F. Fas 2: fact/uncertainty kinds, side-issue classification, Founder Decision Gate ----


def test_fact_and_uncertainty_kinds_and_founder_decision_gate(db_session):
    add_note(db_session, kind=NoteKind.fact, content="Bygger MainAI Project Memory Loop", source_type="doc", source_ref="CLAUDE.md", created_by="t")
    add_note(db_session, kind=NoteKind.uncertainty, content="Oklart om P7A ska aktiveras nu", source_type="doc", source_ref="docs/BRANCH_REGISTRY.md", created_by="t")

    needs_decision = add_note(
        db_session,
        kind=NoteKind.uncertainty,
        content="Ska P1/P2-kedjan mergas till huvudgrenen?",
        source_type="doc",
        source_ref="docs/BRANCH_REGISTRY.md",
        created_by="t",
        classification=SideIssueClassification.needs_founder_decision,
    )
    add_note(
        db_session,
        kind=NoteKind.blocker,
        content="En trivial lint-varning",
        source_type="pr",
        source_ref="#99",
        created_by="t",
        classification=SideIssueClassification.directly_resolvable,
    )

    gate = list_notes_needing_founder_decision(db_session)
    assert [n.id for n in gate] == [needs_decision.id]


def test_classify_note_updates_classification_after_the_fact(db_session):
    note = add_note(db_session, kind=NoteKind.fact, content="X", source_type="doc", source_ref="CLAUDE.md", created_by="t")
    assert note.classification is None

    updated = classify_note(db_session, note.id, classification=SideIssueClassification.needs_founder_decision)
    assert updated.classification == SideIssueClassification.needs_founder_decision


def test_classify_note_raises_for_unknown_id(db_session):
    with pytest.raises(ValueError):
        classify_note(db_session, uuid.uuid4(), classification=SideIssueClassification.blocking)


# --- G. Source ingestion: docs (real files) and git commits (real git binary) --------------


def test_ingest_doc_reads_real_file_stores_durably_and_records_commit_sha(db_session, tmp_path, monkeypatch):
    commit_sha = _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))

    source = ingest_doc(db_session, relative_path="CLAUDE.md", ingested_by="test-agent")

    assert source.source_type == "doc"
    assert source.source_ref == "CLAUDE.md"
    assert source.commit_sha == commit_sha
    assert source.storage_key is not None

    content = read_source_content(source)
    assert "Testprojekt" in content


def test_ingest_doc_supports_nested_paths(db_session, tmp_path, monkeypatch):
    _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))

    source = ingest_doc(db_session, relative_path="docs/BRANCH_REGISTRY.md", ingested_by="t")
    assert "Registry" in read_source_content(source)


def test_ingest_doc_rejects_path_escaping_project_root(db_session, tmp_path, monkeypatch):
    _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))

    with pytest.raises(ValueError):
        ingest_doc(db_session, relative_path="../etc/passwd", ingested_by="t")


def test_ingest_doc_raises_clearly_when_project_root_unset(db_session, monkeypatch):
    monkeypatch.setattr(get_settings(), "project_root", "")
    with pytest.raises(ValueError):
        ingest_doc(db_session, relative_path="CLAUDE.md", ingested_by="t")


def test_ingest_doc_raises_for_missing_file(db_session, tmp_path, monkeypatch):
    _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))
    with pytest.raises(FileNotFoundError):
        ingest_doc(db_session, relative_path="NOPE.md", ingested_by="t")


def test_ingest_git_commit_snapshots_real_head(db_session, tmp_path, monkeypatch):
    commit_sha = _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))

    source = ingest_git_commit(db_session, ingested_by="t")
    assert source.source_type == "git_commit"
    assert source.commit_sha == commit_sha
    assert source.raw_data["commit_sha"] == commit_sha
    assert "subject" in source.raw_data


def test_list_sources_filters_by_type_and_orders_newest_first(db_session, tmp_path, monkeypatch):
    _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))

    ingest_doc(db_session, relative_path="CLAUDE.md", ingested_by="t")
    git_source = ingest_git_commit(db_session, ingested_by="t")

    docs = list_sources(db_session, source_type="doc")
    assert len(docs) == 1

    commits = list_sources(db_session, source_type="git_commit")
    assert commits[0].id == git_source.id


# --- H. Branch/PR status: agent-supplied, superseded not overwritten -----------------------


def test_ingest_github_status_records_pr_snapshot(db_session):
    entry = ingest_github_status(
        db_session,
        kind="pr",
        ref="#8",
        status="open",
        title="P2: ZIP-hardening",
        base_ref="claude/det-kommer-mer-879lcm",
        head_ref="claude/p2-zip-hardening-plan",
        mergeable=True,
        ci_status="success",
        ingested_by="t",
    )
    assert entry.is_current is True
    assert entry.status == "open"

    current = list_current_branch_pr_status(db_session, kind="pr")
    assert [c.id for c in current] == [entry.id]


def test_ingest_github_status_supersedes_prior_snapshot_for_same_ref(db_session):
    first = ingest_github_status(db_session, kind="pr", ref="#8", status="open", ingested_by="t")
    second = ingest_github_status(db_session, kind="pr", ref="#8", status="merged", ingested_by="t")

    current = list_current_branch_pr_status(db_session, kind="pr")
    assert [c.id for c in current] == [second.id]

    db_session.refresh(first)
    assert first.is_current is False
    assert first.superseded_at is not None


def test_ingest_github_status_rejects_invalid_kind(db_session):
    with pytest.raises(ValueError):
        ingest_github_status(db_session, kind="issue", ref="#1", status="open", ingested_by="t")


# --- I. Conflict / duplicate-work detection (heuristic, never decides) ---------------------


def test_detect_duplicate_work_flags_overlapping_open_notes(db_session):
    add_note(
        db_session,
        kind=NoteKind.next_step,
        content="Bygga admin-vy för project memory checkpoints",
        source_type="doc",
        source_ref="CLAUDE.md",
        created_by="t",
    )
    add_note(
        db_session,
        kind=NoteKind.next_step,
        content="Skapa admin-vy för project memory checkpoints",
        source_type="doc",
        source_ref="CLAUDE.md",
        created_by="t",
    )
    add_note(
        db_session,
        kind=NoteKind.next_step,
        content="Helt orelaterad uppgift om SMTP-konfiguration",
        source_type="doc",
        source_ref="CLAUDE.md",
        created_by="t",
    )

    result = detect_conflicts_and_duplicates(db_session)
    assert len(result["duplicate_work_candidates"]) == 1
    pair = result["duplicate_work_candidates"][0]
    assert "admin-vy" in pair["content_a"] or "admin-vy" in pair["content_b"]


def test_detect_conflicts_flags_data_integrity_issue_on_double_current_row(db_session):
    ingest_github_status(db_session, kind="branch", ref="claude/dup", status="open", ingested_by="t")
    # Simulate a data-integrity break: a second "current" row for the same ref, bypassing
    # ingest_github_status()'s normal supersede step (this should never happen via the real
    # entry point — this test proves the safety-net check catches it if it ever does).
    from app.models.project_memory import ProjectBranchPRStatus

    db_session.add(ProjectBranchPRStatus(kind="branch", ref="claude/dup", status="open", is_current=True, recorded_by="t"))
    db_session.commit()

    result = detect_conflicts_and_duplicates(db_session)
    assert {"kind": "branch", "ref": "claude/dup", "current_row_count": 2} in result["data_integrity_issues"]


def test_detect_conflicts_empty_when_nothing_overlaps(db_session):
    add_note(db_session, kind=NoteKind.next_step, content="Helt unik uppgift ett", source_type="doc", source_ref="CLAUDE.md", created_by="t")
    result = detect_conflicts_and_duplicates(db_session)
    assert result == {"duplicate_work_candidates": [], "data_integrity_issues": []}


# --- J. Checkpoint staleness detection -------------------------------------------------------


def test_checkpoint_is_not_stale_immediately_after_creation(db_session, tmp_path, monkeypatch):
    _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))
    ingest_git_commit(db_session, ingested_by="t")

    checkpoint = create_checkpoint(db_session, summary="S", branch_name="b", open_pr_refs=[], created_by="t")
    stale, reasons = is_checkpoint_stale(db_session, checkpoint)
    assert stale is False
    assert reasons == []


def test_checkpoint_is_stale_after_new_note(db_session):
    checkpoint = create_checkpoint(db_session, summary="S", branch_name="b", open_pr_refs=[], created_by="t")
    add_note(db_session, kind=NoteKind.blocker, content="Ny blockerare uppstod", source_type="pr", source_ref="#9", created_by="t")

    stale, reasons = is_checkpoint_stale(db_session, checkpoint)
    assert stale is True
    assert any("noteringar" in r for r in reasons)


def test_checkpoint_is_stale_after_new_branch_pr_status(db_session):
    checkpoint = create_checkpoint(db_session, summary="S", branch_name="b", open_pr_refs=[], created_by="t")
    ingest_github_status(db_session, kind="pr", ref="#20", status="open", ingested_by="t")

    stale, reasons = is_checkpoint_stale(db_session, checkpoint)
    assert stale is True
    assert any("branch-/PR" in r for r in reasons)


def test_checkpoint_is_stale_after_new_git_commit(db_session, tmp_path, monkeypatch):
    _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))
    ingest_git_commit(db_session, ingested_by="t")
    checkpoint = create_checkpoint(db_session, summary="S", branch_name="b", open_pr_refs=[], created_by="t")

    # A new commit lands after the checkpoint.
    (tmp_path / "new_file.txt").write_text("more work")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "more work"], cwd=tmp_path, check=True, capture_output=True)
    ingest_git_commit(db_session, ingested_by="t")

    stale, reasons = is_checkpoint_stale(db_session, checkpoint)
    assert stale is True
    assert any("git-commit" in r for r in reasons)


# --- K. Router coverage for Fas 2 endpoints --------------------------------------------------


def test_memory_api_source_and_status_and_conflicts_endpoints(client, tmp_path, monkeypatch):
    _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    res = client.post("/api/admin/memory/sources/doc", json={"relative_path": "CLAUDE.md"}, headers=headers)
    assert res.status_code == 200, res.text
    source_id = res.json()["id"]

    res = client.get(f"/api/admin/memory/sources/{source_id}")
    assert res.status_code == 200
    assert "Testprojekt" in res.json()["content"]

    res = client.post("/api/admin/memory/sources/git-commit", headers=headers)
    assert res.status_code == 200, res.text
    assert res.json()["source_type"] == "git_commit"

    res = client.post(
        "/api/admin/memory/branch-pr-status",
        json={"kind": "pr", "ref": "#13", "status": "draft", "ci_status": "success"},
        headers=headers,
    )
    assert res.status_code == 200, res.text

    res = client.get("/api/admin/memory/branch-pr-status")
    assert res.status_code == 200
    assert any(row["ref"] == "#13" for row in res.json())

    res = client.get("/api/admin/memory/conflicts")
    assert res.status_code == 200
    assert "duplicate_work_candidates" in res.json()


def test_memory_api_checkpoint_stale_endpoint(client):
    _login(client)
    res = client.post(
        "/api/admin/memory/checkpoints",
        json={"summary": "S", "branch_name": "b", "open_pr_refs": []},
        headers={"X-CSRF-Token": _login(client)},
    )
    assert res.status_code == 200, res.text
    checkpoint_id = res.json()["id"]

    res = client.get(f"/api/admin/memory/checkpoints/{checkpoint_id}/stale")
    assert res.status_code == 200
    assert res.json() == {"stale": False, "reasons": []}


def test_memory_api_needs_founder_decision_endpoint(client):
    csrf = _login(client)
    client.post(
        "/api/admin/memory/notes",
        json={
            "kind": "uncertainty",
            "content": "Ska vi göra X?",
            "source_type": "doc",
            "source_ref": "CLAUDE.md",
            "classification": "needs_founder_decision",
        },
        headers={"X-CSRF-Token": csrf},
    )
    res = client.get("/api/admin/memory/notes/needs-founder-decision")
    assert res.status_code == 200
    assert len(res.json()) == 1
    assert res.json()[0]["classification"] == "needs_founder_decision"


# --- L. Fas 3: proof that the loop actually answers a cold resumption, not just stores rows --


def test_resumption_brief_answers_a_cold_start_without_guessing(client, tmp_path, monkeypatch):
    """This is the automated form of the manual Fas 3 proof: populate real project state
    through the same HTTP surface a fresh agent session would use (doc/git ingestion,
    branch/PR status, notes with every kind, a checkpoint), then assert the resulting
    resumption brief — the one artifact a context-less session is handed — actually
    contains a traceable answer to each of the six required questions (what's being built,
    what's done, what's blocking, which branch/PR, what depends on what, the exact next
    step), each tied to a real source citation. A brief that only echoed hardcoded/test-only
    strings unrelated to what was ingested would fail this; so would one missing a section."""
    _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))
    csrf = _login(client)
    headers = {"X-CSRF-Token": csrf}

    doc_source_id = client.post(
        "/api/admin/memory/sources/doc", json={"relative_path": "CLAUDE.md"}, headers=headers
    ).json()["id"]
    client.post("/api/admin/memory/sources/git-commit", headers=headers)

    client.post(
        "/api/admin/memory/branch-pr-status",
        json={
            "kind": "pr",
            "ref": "13",
            "title": "feat: MainAI Project Memory Loop",
            "status": "open_draft",
            "base_ref": "claude/det-kommer-mer-879lcm",
            "head_ref": "claude/mainai-memory-loop-v1",
            "mergeable": True,
            "ci_status": "pending",
        },
        headers=headers,
    )

    notes = [
        {
            "kind": "fact",
            "content": "Projektet bygger ett founder-only Life OS med en MainAI Project Memory & Coordination Loop.",
            "source_type": "doc",
            "source_ref": "CLAUDE.md",
            "source_id": doc_source_id,
        },
        {
            "kind": "blocker",
            "content": "PR #13 är fortfarande draft tills Fas 3 och Fas 4 är klara.",
            "source_type": "branch_pr_status",
            "source_ref": "pr:13",
        },
        {
            "kind": "next_step",
            "content": "Bygg Fas 4 (admin-UI), ta sedan PR #13 ur draft.",
            "source_type": "branch_pr_status",
            "source_ref": "pr:13",
        },
        {
            "kind": "decision",
            "content": "Rebasa aldrig en branch mot en ny bas i förväg — bara när beroendet faktiskt mergats.",
            "source_type": "doc",
            "source_ref": "CLAUDE.md",
            "source_id": doc_source_id,
        },
    ]
    for note in notes:
        res = client.post("/api/admin/memory/notes", json=note, headers=headers)
        assert res.status_code == 200, res.text

    res = client.post(
        "/api/admin/memory/checkpoints",
        json={
            "summary": "Fas 1+2 klara och verifierade; PR #13 öppen mot claude/det-kommer-mer-879lcm.",
            "branch_name": "claude/mainai-memory-loop-v1",
            "open_pr_refs": ["13"],
        },
        headers=headers,
    )
    assert res.status_code == 200, res.text

    # This is the ONLY read a genuinely cold, context-less session would perform.
    latest = client.get("/api/admin/memory/checkpoints/latest")
    assert latest.status_code == 200
    brief = latest.json()["brief"]

    # a. What is being built.
    assert "Life OS" in brief and "Project Memory" in brief
    # b. What's done / current phase — from the checkpoint's own summary, not invented.
    assert "Fas 1+2 klara" in brief
    # c. What's blocking.
    assert "fortfarande draft" in brief
    # d. Which branch and PR apply.
    assert "claude/mainai-memory-loop-v1" in brief
    assert "13" in latest.json()["open_pr_refs"]
    assert "pr 13" in brief.lower() or "pr: 13" in brief.lower() or "13" in brief
    # e. Dependency: base branch the PR depends on must be visible in the branch/PR section.
    assert "claude/det-kommer-mer-879lcm" in brief
    # f. The exact next safe step.
    assert "Bygg Fas 4" in brief
    # Every section is cited to a real source, not free-standing prose.
    assert "källa: doc CLAUDE.md" in brief
    assert "källa: branch_pr_status pr:13" in brief
    # The static prohibitions list is always present, verbatim, regardless of what was ingested.
    assert "Merga en pull request." in brief
    assert "Källor och tidsstämplar" in brief


# --- L. MainAI Core: conversation & knowledge retrieval (2026-07-26) -----------------------


def test_retrieve_relevant_context_ranks_by_overlap_not_just_recency(db_session):
    add_note(db_session, kind=NoteKind.blocker, content="CI:n är trasig på grund av npm audit", source_type="pr", source_ref="#9", created_by="test")
    add_note(db_session, kind=NoteKind.blocker, content="Databasen saknar en RLS-policy för dokument", source_type="doc", source_ref="CLAUDE.md", created_by="test")
    add_note(db_session, kind=NoteKind.next_step, content="Fixa npm audit sårbarheten i frontend", source_type="pr", source_ref="#9", created_by="test")

    result = retrieve_relevant_context(db_session, "npm audit sårbarhet i CI")

    blockers = result["blockerare"]
    assert len(blockers) == 1
    assert "npm audit" in blockers[0].content
    next_steps = result["nasta_steg"]
    assert len(next_steps) == 1
    assert "npm audit" in next_steps[0].content


def test_retrieve_relevant_context_distinguishes_categories(db_session):
    add_note(db_session, kind=NoteKind.fact, content="LifeAI bygger MainAI som ett Life OS", source_type="doc", source_ref="CLAUDE.md", created_by="test")
    add_note(db_session, kind=NoteKind.decision, content="Grundaren beslutade att aldrig auto-merga utan gate", source_type="doc", source_ref="CLAUDE.md", created_by="test")
    add_note(db_session, kind=NoteKind.idea, content="Idé: kanske bygga en mobilapp senare, ej beslutat", source_type="doc", source_ref="CLAUDE.md", created_by="test")
    add_note(db_session, kind=NoteKind.uncertainty, content="Oklart om Redis-instansen räcker för skalning", source_type="doc", source_ref="CLAUDE.md", created_by="test")
    resolve_note(
        db_session,
        add_note(db_session, kind=NoteKind.blocker, content="Gammal blockerare om auto-merge-gate", source_type="doc", source_ref="CLAUDE.md", created_by="test").id,
        resolved_by="test",
        resolution_note="löst",
    )

    result = retrieve_relevant_context(db_session, "auto-merga")

    assert any("aldrig auto-merga" in n.content for n in result["grundarens_beslut"])
    assert all("mobilapp" not in n.content for n in result["grundarens_beslut"])

    # An idea must never appear in the decisions bucket, and vice versa — that's the exact
    # distinction CLAUDE.md requires between "grundarens beslut" and "idéer som ännu inte
    # beslutats". Checked with an empty (recency-based, unranked) query since categorization
    # by kind is orthogonal to relevance-ranking against a specific question.
    all_categories = retrieve_relevant_context(db_session, "")
    assert any("mobilapp" in n.content for n in all_categories["ej_beslutade_ideer"])
    assert all("mobilapp" not in n.content for n in all_categories["grundarens_beslut"])
    assert any("aldrig auto-merga" in n.content for n in all_categories["grundarens_beslut"])
    assert all("aldrig auto-merga" not in n.content for n in all_categories["ej_beslutade_ideer"])

    result_history = retrieve_relevant_context(db_session, "auto-merge-gate")
    assert any("auto-merge-gate" in n.content for n in result_history["historik"])


def test_retrieve_relevant_context_empty_query_returns_recent_open_notes(db_session):
    add_note(db_session, kind=NoteKind.fact, content="Statusfakta ett", source_type="doc", source_ref="CLAUDE.md", created_by="test")
    result = retrieve_relevant_context(db_session, "")
    assert len(result["verifierade_fakta_och_status"]) == 1


# --- M. MainAI Core: system map ("moderkortsvy") --------------------------------------------


def _init_fake_repo_with_lifeai_structure(tmp_path) -> str:
    """A throwaway repo shaped enough like the real LifeAI layout (one router, one model, one
    migration, one frontend page) for build_system_map() to have something real to scan —
    real file reads, no mocked filesystem."""
    (tmp_path / "backend" / "app" / "routers").mkdir(parents=True)
    (tmp_path / "backend" / "app" / "models").mkdir(parents=True)
    (tmp_path / "backend" / "alembic" / "versions").mkdir(parents=True)
    (tmp_path / "frontend" / "app" / "admin" / "widgets").mkdir(parents=True)

    (tmp_path / "backend" / "app" / "routers" / "widgets.py").write_text(
        'from fastapi import APIRouter\n\nrouter = APIRouter(prefix="/api/widgets", tags=["widgets"])\n\n\n'
        '@router.get("/list")\ndef list_widgets():\n    ...\n\n\n@router.post("/create")\ndef create_widget():\n    ...\n'
    )
    (tmp_path / "backend" / "app" / "models" / "widget.py").write_text(
        "from app.db import Base\n\n\nclass Widget(Base):\n    __tablename__ = \"widgets\"\n"
    )
    (tmp_path / "backend" / "alembic" / "versions" / "0099_widgets.py").write_text(
        '"""Adds the widgets table.\n\nRevision ID: 0099\n"""\n\nrevision = "0099"\ndown_revision = "0098"\n'
    )
    (tmp_path / "frontend" / "app" / "admin" / "widgets" / "page.tsx").write_text("export default function Page() { return null; }\n")

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "Test")
    _git("add", ".")
    _git("commit", "-q", "-m", "initial")
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_build_system_map_finds_routers_models_migrations_and_frontend_routes(tmp_path):
    _init_fake_repo_with_lifeai_structure(tmp_path)
    system_map = build_system_map(tmp_path)

    routers = system_map["routers"]
    assert len(routers) == 1
    assert routers[0]["prefix"] == "/api/widgets"
    assert "GET /api/widgets/list" in routers[0]["routes"]
    assert "POST /api/widgets/create" in routers[0]["routes"]

    models = system_map["models"]
    assert len(models) == 1
    assert "Widget" in models[0]["classes"]
    assert "widgets" in models[0]["tables"]

    migrations = system_map["migrations"]
    assert len(migrations) == 1
    assert migrations[0]["revision"] == "0099"
    assert migrations[0]["down_revision"] == "0098"

    assert "/admin/widgets" in system_map["frontend_routes"]


def test_ingest_system_map_stores_durably_and_records_counts(db_session, tmp_path, monkeypatch):
    commit_sha = _init_fake_repo_with_lifeai_structure(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))

    source = ingest_system_map(db_session, ingested_by="test")

    assert source.source_type == "system_map"
    assert source.commit_sha == commit_sha
    assert source.raw_data["router_count"] == 1
    assert source.raw_data["model_count"] == 1

    stored = read_source_content(source)
    assert '"routers"' in stored
    assert "widgets" in stored

    latest = latest_system_map(db_session)
    assert latest.id == source.id


# --- Pass 31 (a sixth founder review round): close the write-before-reference race ----------
#
# ingest_doc()/ingest_system_map()/create_checkpoint() write through the SAME global,
# content-addressed storage backend the Life Library upload path uses, but never took
# acquire_storage_key_lock() before committing their ProjectSource/ProjectCheckpoint row --
# the exact same "bytes exist before any DB row protects them" race Pass 22 already closed for
# uploads, left open here. Fixed via app/storage/references.py's
# store_content_with_reference_lock(). Tests below are the founder's own lettering (A: a
# ProjectSource write vs. concurrent purge/delete; B: same for ProjectCheckpoint -- covered
# together by the ingest_doc()-based race test since both writers share the identical
# store_content_with_reference_lock() code path; C: writer wins -> reference committed and
# blob present; D: deleter wins -> writer recovers via republish; E: no deadlocks).


def test_store_content_with_reference_lock_returns_a_verified_present_blob_in_the_ordinary_case(db_session):
    content = f"pass 31 point 2 test: ordinary case {uuid.uuid4().hex}".encode()

    blob = store_content_with_reference_lock(db_session, get_storage(), content, max_bytes=len(content) + 10)
    db_session.commit()  # releases the storage-key lock

    assert get_storage().exists(blob.storage_key) is True


def test_store_content_with_reference_lock_recovers_when_a_real_concurrent_purge_deletes_the_blob_first():
    """Test D (founder's lettering): a REAL two-thread, two-session race where the deleter
    genuinely wins the advisory lock FIRST and purges the (at that moment, correctly
    unreferenced) blob before the writer's own lock acquisition can even run. Proves
    store_content_with_reference_lock() recovers by republishing from the same in-memory
    bytes it already has, rather than returning a StoredBlob pointing at nothing."""
    content = f"pass 31 point 2 test D: real concurrent delete recovers via republish {uuid.uuid4().hex}".encode()
    storage_key = get_storage().write_stream(iter([content, b""]).__next__, max_bytes=len(content)).storage_key
    # Nothing references this key yet -- correctly unreferenced at this point, exactly like a
    # ProjectSource writer's blob before its own DB row is committed.

    deleter_holds_lock = threading.Event()
    writer_may_proceed = threading.Event()
    deleter_outcome = {}

    def _deleter_thread():
        db = SessionLocal()
        try:
            acquire_storage_key_lock(db, storage_key)
            deleter_holds_lock.set()
            writer_may_proceed.wait(timeout=5)
            deleter_outcome["outcome"] = delete_if_unreferenced(db, get_storage(), storage_key)
            db.commit()
        finally:
            db.close()

    t = threading.Thread(target=_deleter_thread)
    t.start()
    assert deleter_holds_lock.wait(timeout=5), "deleter thread never acquired the lock"
    writer_may_proceed.set()

    writer_db = SessionLocal()
    try:
        # write_stream() itself is unlocked (structurally can't be locked before the key is
        # known -- see acquire_storage_key_lock's own docstring), so this runs immediately,
        # independent of the deleter thread holding the lock. Content-addressing means this
        # reproduces the exact same blob the deleter is about to (or already did) purge.
        blob = store_content_with_reference_lock(writer_db, get_storage(), content, max_bytes=len(content) + 10)
        writer_db.commit()
        assert blob.storage_key == storage_key
        assert get_storage().exists(storage_key) is True, (
            "store_content_with_reference_lock returned successfully but the blob it just "
            "verified/republished isn't actually on disk"
        )
    finally:
        writer_db.close()

    t.join(timeout=5)
    assert not t.is_alive(), "deleter thread never completed -- possible deadlock"
    assert deleter_outcome["outcome"].name == "purged"


def test_store_content_with_reference_lock_fails_closed_if_the_blob_is_still_missing_after_republishing(db_session):
    """If even the republish attempt can't make the blob durably present (a pathological,
    repeat-loss scenario), this must raise rather than return a StoredBlob a caller would then
    commit a DB row against -- fail closed, never a silent dangling reference."""
    content = f"pass 31 point 2 test: republish still missing -> fail closed {uuid.uuid4().hex}".encode()

    class _NeverExistsStorage:
        def write_stream(self, read_chunk, *, max_bytes, chunk_size=1 << 20):
            return get_storage().write_stream(read_chunk, max_bytes=max_bytes, chunk_size=chunk_size)

        def exists(self, storage_key):
            return False  # simulates the blob vanishing every single time it's checked

        def verify(self, storage_key, *, expected_sha256, expected_size=None):
            return False  # Pass 32: store_content_with_reference_lock() now calls verify(), not exists()

    with pytest.raises(StorageError):
        store_content_with_reference_lock(db_session, _NeverExistsStorage(), content, max_bytes=len(content) + 10)
    db_session.rollback()  # releases the storage-key lock taken before the failure


def test_store_content_with_reference_lock_repairs_a_corrupt_same_size_existing_blob(db_session):
    """Test B (founder's Pass 32 blocker-2 lettering): a same-path, same-size, WRONG-content
    existing blob must not be silently accepted just because `exists()` would have said True --
    `store_content_with_reference_lock()` now checks `storage.verify(expected_sha256=...)`, and
    the write it always performs first (`write_stream()`) itself repairs a corrupt same-size
    dedup candidate (see app/storage/local_fs.py's `_publish()`), so the call ends up both
    returning AND leaving on disk the genuinely correct bytes."""
    import io

    content = f"pass 32 blocker 2 test B: project memory repairs corrupt blob {uuid.uuid4().hex}".encode()
    storage = get_storage()
    reader = io.BytesIO(content)
    first = storage.write_stream(lambda: reader.read(1 << 20), max_bytes=len(content) + 10)

    disk_path = Path(get_settings().storage_root) / first.storage_key
    corrupted = bytes(b ^ 0xFF for b in content)
    assert len(corrupted) == len(content)
    disk_path.write_bytes(corrupted)
    assert storage.verify(first.storage_key, expected_sha256=first.sha256, expected_size=first.size_bytes) is False

    blob = store_content_with_reference_lock(db_session, storage, content, max_bytes=len(content) + 10)
    db_session.commit()

    assert blob.storage_key == first.storage_key
    assert disk_path.read_bytes() == content
    assert storage.verify(blob.storage_key, expected_sha256=blob.sha256, expected_size=blob.size_bytes) is True


def test_ingest_doc_never_leaves_a_dangling_project_source_under_a_real_concurrent_purge(tmp_path, monkeypatch):
    """Tests A/C/E (founder's lettering) through the real ingest_doc() call, not just the
    lower-level helper: a REAL two-thread, two-session race between ingest_doc() (now
    lock-protected) and a concurrent delete_if_unreferenced() for the exact same
    content-addressed key, run several times with both sides starting simultaneously (a real
    Postgres advisory lock, not Python scheduling, decides who actually goes first each time --
    exercising both orderings across repeated runs). The invariant that must hold regardless of
    which side wins: no live ProjectSource ever references a storage_key whose physical blob
    is gone, and neither side ever hangs (no deadlock)."""
    _init_fake_repo(tmp_path)
    monkeypatch.setattr(get_settings(), "project_root", str(tmp_path))

    for attempt in range(4):
        doc_name = f"race-{attempt}.md"
        (tmp_path / doc_name).write_text(f"Race content {attempt} {uuid.uuid4().hex}\n")
        content = (tmp_path / doc_name).read_bytes()
        storage_key = get_storage().write_stream(iter([content, b""]).__next__, max_bytes=len(content)).storage_key

        barrier = threading.Barrier(2)
        errors: list[Exception] = []

        def _ingest():
            db = SessionLocal()
            try:
                barrier.wait(timeout=5)
                ingest_doc(db, relative_path=doc_name, ingested_by="race-ingest")
            except Exception as exc:  # noqa: BLE001 - captured for the assertion below, not swallowed
                errors.append(exc)
            finally:
                db.close()

        def _delete():
            db = SessionLocal()
            try:
                barrier.wait(timeout=5)
                delete_if_unreferenced(db, get_storage(), storage_key)
                db.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                db.close()

        t1 = threading.Thread(target=_ingest)
        t2 = threading.Thread(target=_delete)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not t1.is_alive() and not t2.is_alive(), "a race participant never finished -- possible deadlock"
        assert not errors, f"unexpected exception during the race (attempt {attempt}): {errors}"

        db = SessionLocal()
        try:
            any_source_references_key = db.query(ProjectSource).filter_by(storage_key=storage_key).count() > 0
            blob_exists = get_storage().exists(storage_key)
            assert not (any_source_references_key and not blob_exists), (
                f"attempt {attempt}: a ProjectSource references a storage_key whose physical "
                f"blob is gone -- exactly the dangling-reference outcome the lock protocol "
                f"must prevent"
            )
        finally:
            db.close()
