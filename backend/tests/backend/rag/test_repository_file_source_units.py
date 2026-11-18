"""S1D (migration 0072 on the 0069-composition integration branch; 0069 on its own PR branch,
docs/LIFE_PROJECT_KNOWLEDGE_INGESTION_DESIGN.md §4) — schema/trigger/
RLS/find-or-create coverage for `repository_file_source_units`. Real local Postgres, mirroring
tests/backend/rag/test_message_source_units.py's pattern exactly (RLS exercised for real, not
mocked) for the THIRD `memory_source_units` subtype.

Not covered here (deliberately out of scope for this isolated foundation PR, per the founder's
own "implement only an isolated, reviewable foundation" instruction): a backfill/ingestion job
that actually scans the repo and calls `get_or_create_repository_file_source_unit()` for real
files (that is Pass 3's own future wiring step, not this one), and anything past find-or-create
-- no InterpretationProposal/ProjectEntity/WorkCandidate is created from this layer at all.
"""

import threading
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.db import SessionLocal
from app.models.memory_source_unit import (
    LifecycleStatus,
    MemorySourceUnit,
    RepositoryFileSourceUnit,
    SnapshotStatus,
    SourceKind,
    SourceRole,
)
from app.models.user import User, UserRole
from app.rag.memory_source import (
    MemorySourceIdentityConflict,
    RepositoryFileSourceLocator,
    compute_content_hash,
    get_or_create_repository_file_source_unit,
)
from app.request_context import current_user_id as current_user_id_var
from app.security import hash_password

_REPO = "d1n095/LifeAI"
_COMMIT_A = "a" * 40
_COMMIT_B = "b" * 40


def _set_rls_user(session, owner_id) -> None:
    current_user_id_var.set(str(owner_id))
    session.execute(sa_text("SET LOCAL app.current_user_id = :uid"), {"uid": str(owner_id)})


def _make_user(session, email="rfsu-owner@example.com", *, role=UserRole.founder) -> User:
    user = User(email=email, password_hash=hash_password("Sup3rS3cret!"), role=role, email_verified=True)
    session.add(user)
    session.commit()
    return user


def _locator(owner_id, *, file_path="docs/AUTH_THREAT_MODEL.md", commit_sha=_COMMIT_A, content_text="# Threat model\n") -> RepositoryFileSourceLocator:
    return RepositoryFileSourceLocator(
        owner_id=owner_id,
        repo_identity=_REPO,
        file_path=file_path,
        commit_sha=commit_sha,
        observed_at=datetime.now(timezone.utc),
        content_text=content_text,
        snapshot_status=SnapshotStatus.exact,
    )


# --- identity key format ---------------------------------------------------------------


def test_identity_key_format():
    loc = _locator(uuid.uuid4())
    assert loc.identity_key == f"repository_file:{_REPO}:docs/AUTH_THREAT_MODEL.md:{_COMMIT_A}"


# --- find-or-create ----------------------------------------------------------------------


def test_get_or_create_creates_parent_and_subtype():
    session = SessionLocal()
    try:
        owner = _make_user(session)
        _set_rls_user(session, owner.id)

        msu_id = get_or_create_repository_file_source_unit(session, _locator(owner.id))
        session.commit()

        msu = session.get(MemorySourceUnit, msu_id)
        rfsu = session.get(RepositoryFileSourceUnit, msu_id)
        assert msu is not None
        assert msu.source_kind == SourceKind.repository_file
        assert msu.source_role == SourceRole.unknown
        assert msu.source_identity_key == f"repository_file:{_REPO}:docs/AUTH_THREAT_MODEL.md:{_COMMIT_A}"
        assert msu.snapshot_status == SnapshotStatus.exact
        assert msu.lifecycle_status == LifecycleStatus.active
        content_hash, hash_version = compute_content_hash("# Threat model\n")
        assert msu.content_hash == content_hash
        assert msu.content_hash_version == hash_version
        assert rfsu is not None
        assert rfsu.repo_identity == _REPO
        assert rfsu.file_path == "docs/AUTH_THREAT_MODEL.md"
        assert rfsu.commit_sha == _COMMIT_A
    finally:
        session.close()


def test_get_or_create_is_idempotent_no_duplicate_for_the_same_commit():
    session = SessionLocal()
    try:
        owner = _make_user(session, "idempotent-rfsu@example.com")
        _set_rls_user(session, owner.id)

        first_id = get_or_create_repository_file_source_unit(session, _locator(owner.id))
        session.commit()
        second_id = get_or_create_repository_file_source_unit(session, _locator(owner.id))
        session.commit()

        assert first_id == second_id
        count = session.execute(
            sa_text("SELECT count(*) FROM repository_file_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert count == 1
    finally:
        session.close()


def test_a_new_commit_of_the_same_file_creates_a_separate_immutable_row():
    """The whole point of identifying by commit_sha: git's own history IS the version
    history. A new commit touching the same file_path must produce a SECOND, independent
    memory_source_units row, never an update of the first -- "preserve previous versions and
    superseded decisions" falls out of this for free."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "new-commit-rfsu@example.com")
        _set_rls_user(session, owner.id)

        first_id = get_or_create_repository_file_source_unit(session, _locator(owner.id, commit_sha=_COMMIT_A, content_text="v1"))
        session.commit()
        second_id = get_or_create_repository_file_source_unit(
            session, _locator(owner.id, commit_sha=_COMMIT_B, content_text="v2")
        )
        session.commit()

        assert first_id != second_id
        count = session.execute(
            sa_text("SELECT count(*) FROM repository_file_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert count == 2
        # The original row is untouched -- still v1, still active.
        original = session.get(MemorySourceUnit, first_id)
        assert original.content_text == "v1"
        assert original.lifecycle_status == LifecycleStatus.active
    finally:
        session.close()


def test_get_or_create_real_concurrent_insert_converges():
    """Two threads racing to create the SAME (repo, file, commit) -- mirrors test_message_
    source_units.py's identical concurrency proof -- must converge on exactly one row."""
    setup_session = SessionLocal()
    try:
        owner = _make_user(setup_session, "concurrent-rfsu@example.com")
        owner_id = owner.id
    finally:
        setup_session.close()

    results: dict = {}
    errors: dict = {}

    def _worker(name):
        session = SessionLocal()
        try:
            _set_rls_user(session, owner_id)
            results[name] = get_or_create_repository_file_source_unit(session, _locator(owner_id))
            session.commit()
        except Exception as exc:  # noqa: BLE001 - captured and asserted on below
            errors[name] = exc
        finally:
            session.close()

    thread_a = threading.Thread(target=_worker, args=("a",))
    thread_b = threading.Thread(target=_worker, args=("b",))
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=15)
    thread_b.join(timeout=15)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert not errors, f"a worker raised unexpectedly: {errors}"
    assert results["a"] == results["b"]

    verify_session = SessionLocal()
    try:
        _set_rls_user(verify_session, owner_id)
        count = verify_session.execute(
            sa_text("SELECT count(*) FROM repository_file_source_units WHERE owner_id = :oid"), {"oid": str(owner_id)}
        ).scalar()
        assert count == 1
    finally:
        verify_session.close()


def test_get_or_create_detects_content_hash_mismatch_on_retry():
    """Unlike document/message locators, repository_file's identity_key already encodes every
    locator field (repo_identity, file_path, commit_sha) -- there is no OTHER field that could
    disagree while the identity key stays the same, so the only possible "same identity,
    different reality" conflict is the content itself. A caller claiming the SAME commit_sha
    produced different text (a caller bug, since a commit's content is immutable in git) must
    still be rejected, not silently accepted as a match."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "hash-mismatch-rfsu@example.com")
        _set_rls_user(session, owner.id)

        get_or_create_repository_file_source_unit(session, _locator(owner.id, content_text="original text"))
        session.commit()

        stale_locator = _locator(owner.id, content_text="a different text claiming the same commit_sha")
        with pytest.raises(MemorySourceIdentityConflict):
            get_or_create_repository_file_source_unit(session, stale_locator)
    finally:
        session.rollback()
        session.close()


# --- DB-level structural checks (ck_rfsu_*) -----------------------------------------------


def test_commit_sha_must_be_a_real_40_hex_character_sha_never_head_or_a_branch():
    """Structural, not conventional: ck_rfsu_commit_sha_format rejects anything that isn't
    exactly 40 lowercase hex characters -- "HEAD", a branch name, or a short SHA are all
    rejected at the database level, matching this session's own exact-SHA evidence
    discipline structurally rather than trusting every future caller to get it right."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "bad-sha@example.com")
        _set_rls_user(session, owner.id)

        for bad_sha in ("HEAD", "main", _COMMIT_A[:7], _COMMIT_A.upper()):
            with pytest.raises((IntegrityError, DBAPIError)):
                get_or_create_repository_file_source_unit(session, _locator(owner.id, commit_sha=bad_sha))
            session.rollback()
    finally:
        session.rollback()
        session.close()


def test_file_path_rejects_absolute_paths_and_parent_directory_escapes():
    session = SessionLocal()
    try:
        owner = _make_user(session, "bad-path@example.com")
        _set_rls_user(session, owner.id)

        for bad_path in ("/etc/passwd", "../../etc/passwd", "docs/../../etc/passwd"):
            with pytest.raises((IntegrityError, DBAPIError)):
                get_or_create_repository_file_source_unit(session, _locator(owner.id, file_path=bad_path))
            session.rollback()
    finally:
        session.rollback()
        session.close()


# --- DB-level trigger enforcement ---------------------------------------------------------


def test_trigger_rejects_source_role_other_than_unknown():
    """A caller bypassing app/rag/memory_source.py entirely (raw INSERT with source_role set
    to anything but 'unknown') must still be rejected at commit -- nothing at ingest time can
    vouch for a repository file's authorship, exactly like document_source_units."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "trigger-role@example.com")
        _set_rls_user(session, owner.id)

        content_hash, hash_version = compute_content_hash("text")
        msu = MemorySourceUnit(
            owner_id=owner.id, source_kind=SourceKind.repository_file,
            source_identity_key=f"repository_file:{_REPO}:docs/X.md:{_COMMIT_A}",
            source_role=SourceRole.founder,  # WRONG: must be unknown
            observed_at=datetime.now(timezone.utc), content_text="text",
            content_hash=content_hash, content_hash_version=hash_version, snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        session.flush()
        session.add(RepositoryFileSourceUnit(
            memory_source_id=msu.id, owner_id=owner.id, source_kind=SourceKind.repository_file,
            repo_identity=_REPO, file_path="docs/X.md", commit_sha=_COMMIT_A,
        ))
        with pytest.raises((IntegrityError, DBAPIError), match="source_role must be unknown"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_trigger_rejects_exact_snapshot_hash_mismatch():
    session = SessionLocal()
    try:
        owner = _make_user(session, "trigger-hash@example.com")
        _set_rls_user(session, owner.id)

        wrong_hash, hash_version = compute_content_hash("completely different text")
        msu = MemorySourceUnit(
            owner_id=owner.id, source_kind=SourceKind.repository_file,
            source_identity_key=f"repository_file:{_REPO}:docs/Y.md:{_COMMIT_A}",
            source_role=SourceRole.unknown, observed_at=datetime.now(timezone.utc),
            content_text="the real text", content_hash=wrong_hash, content_hash_version=hash_version,
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        session.flush()
        session.add(RepositoryFileSourceUnit(
            memory_source_id=msu.id, owner_id=owner.id, source_kind=SourceKind.repository_file,
            repo_identity=_REPO, file_path="docs/Y.md", commit_sha=_COMMIT_A,
        ))
        with pytest.raises((IntegrityError, DBAPIError), match="content_hash does not match"):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_direct_update_of_repository_file_source_unit_fields_rejected():
    session = SessionLocal()
    try:
        owner = _make_user(session, "immutable-update-rfsu@example.com")
        _set_rls_user(session, owner.id)

        msu_id = get_or_create_repository_file_source_unit(session, _locator(owner.id))
        session.commit()

        rfsu = session.get(RepositoryFileSourceUnit, msu_id)
        rfsu.commit_sha = _COMMIT_B
        # No match= regex, mirroring test_message_source_units.py's identical comment: a
        # narrowed-privilege ordering may reject this with "permission denied" before the
        # guard trigger ever runs -- an equally valid rejection, just a different layer.
        with pytest.raises((IntegrityError, DBAPIError)):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_direct_delete_of_repository_file_source_unit_rejected_outside_erasure():
    session = SessionLocal()
    try:
        owner = _make_user(session, "immutable-delete-rfsu@example.com")
        _set_rls_user(session, owner.id)

        msu_id = get_or_create_repository_file_source_unit(session, _locator(owner.id))
        session.commit()

        rfsu = session.get(RepositoryFileSourceUnit, msu_id)
        session.delete(rfsu)
        with pytest.raises((IntegrityError, DBAPIError)):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_exact_one_subtype_required_for_repository_file_source_kind():
    """S1A's exclusive-arc guarantee (migration 0019's deferred constraint trigger, widened by
    migration 0072 on the 0069-composition integration branch) applies identically to the new
    'repository_file' arm: a memory_source_units
    row with source_kind='repository_file' but no matching repository_file_source_units row
    must be rejected at commit."""
    session = SessionLocal()
    try:
        owner = _make_user(session, "exclusive-arc-rfsu@example.com")
        _set_rls_user(session, owner.id)

        content_hash, hash_version = compute_content_hash("orphan")
        msu = MemorySourceUnit(
            owner_id=owner.id, source_kind=SourceKind.repository_file,
            source_identity_key=f"repository_file:{_REPO}:docs/Z.md:{_COMMIT_A}",
            source_role=SourceRole.unknown, observed_at=datetime.now(timezone.utc),
            content_text="orphan", content_hash=content_hash, content_hash_version=hash_version,
            snapshot_status=SnapshotStatus.exact,
        )
        session.add(msu)
        # No matching repository_file_source_units row ever added.
        with pytest.raises((IntegrityError, DBAPIError), match="no matching repository_file_source_units row"):
            session.commit()
    finally:
        session.rollback()
        session.close()


# --- RLS -----------------------------------------------------------------------------------


def test_rls_owner_isolation_on_repository_file_source_units():
    session = SessionLocal()
    try:
        owner_a = _make_user(session, "rls-rfsu-a@example.com")
        owner_b = _make_user(session, "rls-rfsu-b@example.com")
        _set_rls_user(session, owner_a.id)
        get_or_create_repository_file_source_unit(session, _locator(owner_a.id))
        session.commit()

        _set_rls_user(session, owner_b.id)
        count = session.execute(sa_text("SELECT count(*) FROM repository_file_source_units")).scalar()
        assert count == 0
    finally:
        session.rollback()
        session.close()


def test_rls_default_deny_without_current_user_id():
    session = SessionLocal()
    try:
        owner = _make_user(session, "rls-rfsu-deny@example.com")
        _set_rls_user(session, owner.id)
        get_or_create_repository_file_source_unit(session, _locator(owner.id))
        session.commit()

        current_user_id_var.set(None)
        session.execute(sa_text("RESET app.current_user_id"))
        count = session.execute(sa_text("SELECT count(*) FROM repository_file_source_units")).scalar()
        assert count == 0
    finally:
        session.rollback()
        session.close()


# --- account erasure cascade -----------------------------------------------------------------


def test_erase_owner_memory_cascades_into_repository_file_source_units():
    session = SessionLocal()
    try:
        owner = _make_user(session, "erasure-rfsu@example.com")
        _set_rls_user(session, owner.id)
        get_or_create_repository_file_source_unit(session, _locator(owner.id))
        session.commit()

        session.execute(sa_text("SELECT erase_owner_memory(:oid)"), {"oid": str(owner.id)})
        session.commit()

        count = session.execute(
            sa_text("SELECT count(*) FROM repository_file_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert count == 0
        msu_count = session.execute(
            sa_text("SELECT count(*) FROM memory_source_units WHERE owner_id = :oid"), {"oid": str(owner.id)}
        ).scalar()
        assert msu_count == 0
    finally:
        session.rollback()
        session.close()
