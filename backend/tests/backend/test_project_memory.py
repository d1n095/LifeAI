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

from app.models.project_memory import NoteKind, NoteStatus, ProjectCheckpointNote
from app.project_memory import (
    add_note,
    create_checkpoint,
    generate_resumption_brief,
    get_latest_checkpoint,
    list_checkpoints,
    list_notes,
    read_checkpoint_brief,
    resolve_note,
)

FOUNDER_EMAIL = "founder@lifeos.local"
FOUNDER_PASSWORD = "TestFounderPassword123!"


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
    # 4 = "Öppna PR:er" plus the blocker/decision/next_step sections, all empty
    assert brief.count("(inga)") == 4
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
