"""Adversarial security regression tests for Blueprint approval integrity (migration 0071).

Closes two P1 findings with DATABASE-enforced guarantees (not the Python service alone):

P1-1 the runtime role could forge approval / modify approved content / delete a protected record.
P1-2 concurrent Founder approvals were not transaction-safe.

Two enforcement layers, each tested directly:
  * a BEFORE UPDATE/DELETE guard trigger (`trg_blueprint_drafts_approval_guard`) that fires for
    EVERY writer — proven here against the superuser connection, which bypasses RLS and holds
    full DML, so a pass means the trigger (not a privilege or RLS) is what stops the attack;
  * grant-level narrowing (`scripts/security/s1a_privilege_policy.py` → blueprint_drafts =
    SELECT,INSERT only) so in production the runtime role cannot UPDATE/DELETE at all and may
    only EXECUTE the owner-scoped SECURITY DEFINER `blueprint_approve_draft` — proven by applying
    the real policy and asserting `mainai_app` is refused.
The approval path itself (`blueprint_approve_draft`) takes `SELECT ... FOR UPDATE` then re-checks
status inside the lock, which is what makes concurrent approvals safe.
"""

from __future__ import annotations

import uuid

import psycopg2
import psycopg2.errors
import pytest
from sqlalchemy import text as sa_text
from sqlalchemy.exc import DBAPIError, InternalError, ProgrammingError

from app.blueprint.service import approve_blueprint_draft, generate_blueprint_draft
from app.config import get_settings
from app.models.blueprint_draft import BlueprintDraft
from app.models.document import Document
from app.models.user import User


def _user(db) -> User:
    u = User(email=f"bpsec-{uuid.uuid4()}@example.com", password_hash="x", email_verified=True)
    db.add(u)
    db.flush()
    return u


def _doc(db, owner_id) -> Document:
    d = Document(title="orig", uploaded_by=owner_id, checksum=uuid.uuid4().hex, original_filename="orig.txt")
    db.add(d)
    db.flush()
    return d


def _synth(title, query, hits):
    return f"DRAFT: {title} ({len(hits)} sources)"


def _make_draft(superuser_db) -> tuple[uuid.UUID, uuid.UUID]:
    """Create an owner + a draft via the real generator; set app.current_user_id so the
    owner-scoped approval function can resolve the owner. Returns (owner_id, draft_id)."""
    owner = _user(superuser_db)
    doc = _doc(superuser_db, owner.id)
    draft = generate_blueprint_draft(
        superuser_db, owner_id=owner.id, title=f"BP-{uuid.uuid4().hex[:6]}",
        source_hits=[{"document_id": doc.id, "text": "decision X", "similarity": 0.9}],
        synthesizer=_synth, model_provider="ollama", model_name="qwen2.5:0.5b",
    )
    superuser_db.commit()
    superuser_db.execute(sa_text(f"SET app.current_user_id = '{owner.id}'"))
    return owner.id, draft.id


# --------------------------------------------------------------------------------------
# Layer 1 — guard trigger (fires for every writer; proven vs the full-DML superuser session)
# --------------------------------------------------------------------------------------

def test_approval_via_controlled_path_succeeds(superuser_db):
    owner_id, draft_id = _make_draft(superuser_db)
    approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft_id, approved_by=str(owner_id))
    superuser_db.commit()
    row = superuser_db.get(BlueprintDraft, draft_id)
    superuser_db.refresh(row)
    assert row.status == "approved"
    assert row.approved_by == str(owner_id) and row.approved_at is not None
    assert row.provenance.get("pending_founder_approval") is False
    assert row.provenance.get("approved_by_founder") is True


def test_approved_draft_body_is_immutable(superuser_db):
    owner_id, draft_id = _make_draft(superuser_db)
    approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft_id, approved_by=str(owner_id))
    superuser_db.commit()
    with pytest.raises((DBAPIError, InternalError, ProgrammingError)):
        superuser_db.execute(sa_text("UPDATE blueprint_drafts SET body = 'tampered' WHERE id = :i"), {"i": str(draft_id)})
        superuser_db.flush()
    superuser_db.rollback()
    row = superuser_db.get(BlueprintDraft, draft_id)
    assert row.body != "tampered"


def test_approved_draft_cannot_be_deleted(superuser_db):
    owner_id, draft_id = _make_draft(superuser_db)
    approve_blueprint_draft(superuser_db, owner_id=owner_id, draft_id=draft_id, approved_by=str(owner_id))
    superuser_db.commit()
    with pytest.raises((DBAPIError, InternalError, ProgrammingError)):
        superuser_db.execute(sa_text("DELETE FROM blueprint_drafts WHERE id = :i"), {"i": str(draft_id)})
        superuser_db.flush()
    superuser_db.rollback()
    assert superuser_db.get(BlueprintDraft, draft_id) is not None


def test_cannot_forge_approval_without_audit_fields(superuser_db):
    _owner_id, draft_id = _make_draft(superuser_db)
    with pytest.raises((DBAPIError, InternalError, ProgrammingError)):
        superuser_db.execute(sa_text("UPDATE blueprint_drafts SET status = 'approved' WHERE id = :i"), {"i": str(draft_id)})
        superuser_db.flush()
    superuser_db.rollback()
    assert superuser_db.get(BlueprintDraft, draft_id).status == "draft"


def test_cannot_tamper_content_while_approving(superuser_db):
    _owner_id, draft_id = _make_draft(superuser_db)
    with pytest.raises((DBAPIError, InternalError, ProgrammingError)):
        superuser_db.execute(
            sa_text(
                "UPDATE blueprint_drafts SET status='approved', approved_at=now(), "
                "approved_by='forger', body='tampered' WHERE id = :i"
            ),
            {"i": str(draft_id)},
        )
        superuser_db.flush()
    superuser_db.rollback()
    row = superuser_db.get(BlueprintDraft, draft_id)
    assert row.status == "draft" and row.body != "tampered"


# --------------------------------------------------------------------------------------
# Layer 1 — transaction-safe approval (P1-2): FOR UPDATE serializes concurrent approvals
# --------------------------------------------------------------------------------------

def test_concurrent_approvals_are_serialized_and_only_one_wins(superuser_db):
    owner_id, draft_id = _make_draft(superuser_db)
    superuser_db.commit()
    dsn = get_settings().database_url

    def _open():
        c = psycopg2.connect(dsn)
        c.autocommit = False
        cur = c.cursor()
        cur.execute("SET app.current_user_id = %s", (str(owner_id),))
        return c, cur

    # Connection A takes the row lock and approves, but does NOT commit yet.
    ca, cura = _open()
    cura.execute("SELECT blueprint_approve_draft(%s, %s)", (str(draft_id), "A"))

    # Connection B tries to approve the same draft while A holds the lock. With a short
    # lock_timeout it must fail to acquire the row lock (proving the approvals serialize on the
    # FOR UPDATE, not interleave) rather than silently double-approve.
    cb, curb = _open()
    curb.execute("SET lock_timeout = '750ms'")
    b_blocked = False
    try:
        curb.execute("SELECT blueprint_approve_draft(%s, %s)", (str(draft_id), "B"))
    except (psycopg2.errors.LockNotAvailable, psycopg2.errors.QueryCanceled):
        b_blocked = True
    cb.rollback()
    cb.close()
    assert b_blocked, "second concurrent approval was not serialized behind the row lock"

    ca.commit()
    ca.close()

    # A fresh approval attempt now sees status='approved' and is rejected (not re-approved).
    cc, curc = _open()
    rejected = False
    try:
        curc.execute("SELECT blueprint_approve_draft(%s, %s)", (str(draft_id), "C"))
    except psycopg2.Error:
        rejected = True
    cc.rollback()
    cc.close()
    assert rejected, "a second approval after commit must be rejected (not in draft status)"

    superuser_db.expire_all()
    row = superuser_db.get(BlueprintDraft, draft_id)
    assert row.status == "approved" and row.approved_by == "A"


# --------------------------------------------------------------------------------------
# Layer 2 — production grant-level narrowing: mainai_app cannot UPDATE/DELETE at all
# --------------------------------------------------------------------------------------

def _apply_policy():
    import importlib.util
    from pathlib import Path

    policy_path = Path(__file__).resolve().parents[2] / "scripts" / "security" / "s1a_privilege_policy.py"
    spec = importlib.util.spec_from_file_location("s1a_privilege_policy", policy_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    settings = get_settings()
    conn = psycopg2.connect(settings.database_url)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SELECT current_user")
        owner = cur.fetchone()[0]
        errors = module.apply_privilege_policy(cur, expected_owner=owner, require_complete=True, mutate=True)
    conn.commit()
    conn.close()
    return errors


@pytest.fixture
def restore_privileges():
    yield
    conn = psycopg2.connect(get_settings().database_url)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mainai_app")
    conn.close()


def test_runtime_role_loses_update_and_delete_on_blueprint_drafts(restore_privileges):
    errors = _apply_policy()
    assert errors == [], f"privilege policy reported errors: {errors}"
    conn = psycopg2.connect(get_settings().database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT has_table_privilege('mainai_app', 'public.blueprint_drafts', 'UPDATE')")
            assert cur.fetchone()[0] is False, "mainai_app must not hold UPDATE on blueprint_drafts"
            cur.execute("SELECT has_table_privilege('mainai_app', 'public.blueprint_drafts', 'DELETE')")
            assert cur.fetchone()[0] is False, "mainai_app must not hold DELETE on blueprint_drafts"
            cur.execute("SELECT has_table_privilege('mainai_app', 'public.blueprint_drafts', 'SELECT')")
            assert cur.fetchone()[0] is True
            cur.execute("SELECT has_table_privilege('mainai_app', 'public.blueprint_drafts', 'INSERT')")
            assert cur.fetchone()[0] is True
            cur.execute("SELECT has_function_privilege('mainai_app', 'blueprint_approve_draft(uuid, text)', 'EXECUTE')")
            assert cur.fetchone()[0] is True, "mainai_app must keep EXECUTE on the approval function"
    finally:
        conn.close()


def test_runtime_role_direct_write_is_refused_by_privilege(restore_privileges):
    _apply_policy()
    appconn = psycopg2.connect(get_settings().app_database_url)
    appconn.autocommit = True
    try:
        with appconn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("UPDATE blueprint_drafts SET status = 'approved'")
        with appconn.cursor() as cur:
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                cur.execute("DELETE FROM blueprint_drafts")
    finally:
        appconn.close()


def test_runtime_role_cannot_spoof_owner_guc_and_forge_approval(
    superuser_db, restore_privileges
):
    """A raw mainai_app session must not become Founder authority by setting a custom GUC."""
    owner_id, draft_id = _make_draft(superuser_db)
    superuser_db.commit()
    errors = _apply_policy()
    assert errors == [], f"privilege policy reported errors: {errors}"

    appconn = psycopg2.connect(get_settings().app_database_url)
    appconn.autocommit = True
    try:
        with appconn.cursor() as cur:
            cur.execute(
                "SELECT set_config('app.current_user_id', %s, false)",
                (str(owner_id),),
            )
            with pytest.raises(
                psycopg2.Error,
                match="unauthorized|founder|permission|identity",
            ):
                cur.execute(
                    "SELECT blueprint_approve_draft(%s, %s)",
                    (str(draft_id), "forged-by-runtime-role"),
                )
    finally:
        appconn.close()

    superuser_db.expire_all()
    row = superuser_db.get(BlueprintDraft, draft_id)
    assert row.status == "draft"
    assert row.approved_at is None
    assert row.approved_by is None
