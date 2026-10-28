"""Personal Recall Security Boundary — independent adversarial examination.

Candidate: 460e8ad8cbd40b7fb0d62702f28c14412688e298. DB-backed via the target's own
tests.conftest, so every guard / privilege / RLS policy under test is the REAL production
boundary. The authority substrate (owner key + grant) is built through the product's own
privileged services bound to the canonical Founder identity + a live access JTI.

RECALL STAYS INACTIVE: no ingestion, retrieval, snapshot or production activation is performed
— only the authority substrate needed to examine the DB guards. No changed identity is
certified by relying on any prior certification; everything is proven on this exact SHA.

Scope: none of the Recall DB-guard messages exercised here is asserted anywhere in tests/
(verified by grep); the existing personal_recall suite covers retrieval/indexing/snapshot
semantics, not this DB authority boundary.

  RC1  runtime cannot MINT a grant (privilege-denied AND guard defense-in-depth)
  RC2  runtime cannot CREATE/ROTATE an owner key (privilege-denied AND guard defense-in-depth)
  RC3  fail-closed: the founder-authority GUC alone is insufficient without current canonical
       founder authority
  RC4  identity binding: founder authority for the canonical owner cannot mint for another owner
  RC5  a grant's bindings are immutable; only one-way revocation is permitted (runtime may
       revoke, never widen, never un-revoke)
  RC6  grant/key are not deletable outside a governed erasure
  RC7  protected-data isolation: a foreign owner cannot see/mutate the grant or key, and the
       content tables FORCE owner-scoped RLS
  RC8  runtime cannot mutate an owner key at all outside erasure (column-denied AND guard)

Read-only examiner. No paid calls, no production mutation, loopback-only. first_cycle_go=false.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from _guards import _no_egress  # noqa: F401  (autouse egress guard)
from _harness import (
    CHUNKS,
    GRANTS,
    OWNER_KEYS,
    SOURCES,
    founder_authority_and_key,
    key_id_for,
    mint_grant,
    set_rls_user,
)


@pytest.fixture
def founder():
    owner, authority, jti = founder_authority_and_key()
    return {"owner": owner, "authority": authority, "jti": jti}


def _grant_is_live(superuser_db, grant_id) -> bool:
    set_rls_user(superuser_db, uuid.UUID(int=0))  # reset; superuser bypasses RLS anyway
    return superuser_db.execute(
        text(f"SELECT revoked_at IS NULL FROM {GRANTS} WHERE id=:i"), {"i": grant_id},
    ).scalar_one()


# ---------------------------------------------------------------------------
# RC1 — the runtime cannot mint a Recall grant: it lacks INSERT privilege, and even if a
#        future GRANT exposed INSERT the guard refuses the runtime identity.
# ---------------------------------------------------------------------------
def test_RC1_runtime_cannot_mint_grant(db_session, superuser_db, founder):
    owner = founder["owner"]

    # (a) no INSERT privilege for the runtime role
    set_rls_user(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError):
        db_session.execute(text(f"INSERT INTO {GRANTS} (owner_id) VALUES (:o)"), {"o": owner})
    db_session.rollback()

    # (b) defense in depth: temporarily grant INSERT, the guard still refuses mainai_app
    granted = False
    try:
        superuser_db.execute(text(f"GRANT INSERT ON {GRANTS} TO mainai_app"))
        superuser_db.commit()
        granted = True
        set_rls_user(db_session, owner, access_jti=founder["jti"])
        with pytest.raises(DBAPIError, match="runtime cannot mint Personal Recall authority"):
            db_session.execute(text(f"INSERT INTO {GRANTS} (owner_id) VALUES (:o)"), {"o": owner})
        db_session.rollback()
    finally:
        if granted:
            superuser_db.execute(text(f"REVOKE INSERT ON {GRANTS} FROM mainai_app"))
            superuser_db.commit()


# ---------------------------------------------------------------------------
# RC2 — the runtime cannot create or rotate an owner key (same two-layer fence).
# ---------------------------------------------------------------------------
def test_RC2_runtime_cannot_create_or_rotate_owner_key(db_session, superuser_db, founder):
    owner = founder["owner"]

    set_rls_user(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError):
        db_session.execute(text(f"INSERT INTO {OWNER_KEYS} (owner_id) VALUES (:o)"), {"o": owner})
    db_session.rollback()

    granted = False
    try:
        superuser_db.execute(text(f"GRANT INSERT ON {OWNER_KEYS} TO mainai_app"))
        superuser_db.commit()
        granted = True
        set_rls_user(db_session, owner, access_jti=founder["jti"])
        with pytest.raises(DBAPIError, match="runtime cannot create or rotate Personal Recall owner keys"):
            db_session.execute(text(f"INSERT INTO {OWNER_KEYS} (owner_id) VALUES (:o)"), {"o": owner})
        db_session.rollback()
    finally:
        if granted:
            superuser_db.execute(text(f"REVOKE INSERT ON {OWNER_KEYS} FROM mainai_app"))
            superuser_db.commit()


# ---------------------------------------------------------------------------
# RC3 — fail closed: a privileged caller setting the founder-authority GUC but WITHOUT current
#        canonical founder authority (no live access JTI) cannot mint a grant.
# ---------------------------------------------------------------------------
def test_RC3_grant_authority_guc_alone_is_insufficient(superuser_db, founder):
    owner = founder["owner"]
    # founder owner, authority GUC set, but no access-JTI context -> authority not current
    superuser_db.execute(text("SET LOCAL app.current_user_id = :o"), {"o": str(owner)})
    superuser_db.execute(text("SET LOCAL app.personal_recall_grant_authority = 'founder_authorized'"))
    with pytest.raises(DBAPIError, match="requires current canonical founder authority"):
        superuser_db.execute(text(f"INSERT INTO {GRANTS} (owner_id) VALUES (:o)"), {"o": owner})
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# RC4 — identity binding: canonical founder authority cannot mint a grant for a DIFFERENT owner.
# ---------------------------------------------------------------------------
def test_RC4_authority_cannot_mint_cross_owner(superuser_db, founder, make_verified_user):
    other, _ = make_verified_user()
    # Full canonical founder context, but target another owner's id.
    superuser_db.execute(text("SET LOCAL app.current_user_id = :o"), {"o": str(founder["owner"])})
    superuser_db.execute(text("SET LOCAL app.current_access_jti = :j"), {"j": founder["jti"]})
    superuser_db.execute(text("SET LOCAL app.personal_recall_grant_authority = 'founder_authorized'"))
    with pytest.raises(DBAPIError, match="requires current canonical founder authority"):
        superuser_db.execute(text(f"INSERT INTO {GRANTS} (owner_id) VALUES (:o)"), {"o": other.id})
    superuser_db.rollback()


# ---------------------------------------------------------------------------
# RC5 — a grant's bindings are immutable; only one-way revocation is permitted. The runtime may
#        revoke (revoked_at NULL->set) but may not widen can_retrieve/can_disclose nor un-revoke.
# ---------------------------------------------------------------------------
def test_RC5_grant_is_revoke_only_and_bindings_immutable(db_session, superuser_db, founder):
    owner = founder["owner"]

    # widening a live grant's disclosure is rejected
    widen = mint_grant(owner, founder["authority"], session_id="rc5-widen")
    set_rls_user(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="bindings are immutable; only revocation is permitted"):
        db_session.execute(
            text(f"UPDATE {GRANTS} SET can_disclose=NOT can_disclose WHERE id=:i"), {"i": widen})
    db_session.rollback()

    # one-way revocation IS allowed for the runtime
    revoke = mint_grant(owner, founder["authority"], session_id="rc5-revoke")
    set_rls_user(db_session, owner, access_jti=founder["jti"])
    res = db_session.execute(
        text(f"UPDATE {GRANTS} SET revoked_at=now() WHERE id=:i AND revoked_at IS NULL"),
        {"i": revoke})
    assert res.rowcount == 1
    db_session.commit()

    # un-revoking is rejected (not a one-way revocation)
    set_rls_user(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="bindings are immutable; only revocation is permitted"):
        db_session.execute(text(f"UPDATE {GRANTS} SET revoked_at=NULL WHERE id=:i"), {"i": revoke})
    db_session.rollback()

    assert _grant_is_live(superuser_db, widen) is True       # widen attempt left it live
    assert _grant_is_live(superuser_db, revoke) is False      # revocation stuck


# ---------------------------------------------------------------------------
# RC6 — grant and owner key are NOT deletable outside a governed erasure, even though the
#        runtime holds the DELETE privilege.
# ---------------------------------------------------------------------------
def test_RC6_grant_and_key_not_deletable_outside_erasure(db_session, founder):
    owner = founder["owner"]
    grant = mint_grant(owner, founder["authority"], session_id="rc6")
    kid = key_id_for(owner)

    set_rls_user(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="grants are revoked, not ordinarily deleted"):
        db_session.execute(text(f"DELETE FROM {GRANTS} WHERE id=:i"), {"i": grant})
    db_session.rollback()

    set_rls_user(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="owner keys are not ordinarily deletable"):
        db_session.execute(text(f"DELETE FROM {OWNER_KEYS} WHERE id=:i"), {"i": kid})
    db_session.rollback()


# ---------------------------------------------------------------------------
# RC7 — protected-data isolation: a foreign owner cannot see or mutate the grant/key, and the
#        content tables FORCE owner-scoped RLS.
# ---------------------------------------------------------------------------
def test_RC7_protected_data_is_owner_isolated(db_session, superuser_db, founder, make_verified_user):
    owner = founder["owner"]
    grant = mint_grant(owner, founder["authority"], session_id="rc7")
    attacker, _ = make_verified_user()

    set_rls_user(db_session, attacker.id, access_jti=str(uuid.uuid4()))
    assert db_session.execute(
        text(f"SELECT count(*) FROM {GRANTS} WHERE id=:i"), {"i": grant}).scalar_one() == 0
    assert db_session.execute(
        text(f"SELECT count(*) FROM {OWNER_KEYS} WHERE owner_id=:o"), {"o": owner}).scalar_one() == 0
    res = db_session.execute(
        text(f"UPDATE {GRANTS} SET revoked_at=now() WHERE id=:i"), {"i": grant})
    assert res.rowcount == 0                       # RLS hid the founder's grant from the attacker
    db_session.commit()
    assert _grant_is_live(superuser_db, grant) is True

    # the protected CONTENT tables force owner-scoped RLS (no ingestion performed)
    for table in (SOURCES, CHUNKS, GRANTS, OWNER_KEYS):
        forced = superuser_db.execute(
            text("SELECT relforcerowsecurity FROM pg_class WHERE relname=:t"), {"t": table},
        ).scalar_one()
        assert forced is True, table
        policies = superuser_db.execute(
            text("SELECT count(*) FROM pg_policies WHERE tablename=:t"), {"t": table},
        ).scalar_one()
        assert policies >= 1, table


# ---------------------------------------------------------------------------
# RC8 — the runtime cannot mutate an owner key at all outside a governed erasure: it is denied
#        the key-material column at the privilege layer, and the guard refuses the runtime
#        identity even on its granted (status, revoked_at) columns.
# ---------------------------------------------------------------------------
def test_RC8_runtime_cannot_mutate_owner_key(db_session, founder):
    owner = founder["owner"]
    kid = key_id_for(owner)

    # (a) key-material column is not granted to the runtime -> permission denied
    set_rls_user(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError):
        db_session.execute(
            text(f"UPDATE {OWNER_KEYS} SET wrapped_owner_key=:v WHERE id=:i"),
            {"v": b"x", "i": kid})
    db_session.rollback()

    # (b) even on a granted column, the guard refuses the runtime identity (not under erasure)
    set_rls_user(db_session, owner, access_jti=founder["jti"])
    with pytest.raises(DBAPIError, match="runtime cannot create or rotate Personal Recall owner keys"):
        db_session.execute(
            text(f"UPDATE {OWNER_KEYS} SET revoked_at=now() WHERE id=:i"), {"i": kid})
    db_session.rollback()
