"""Database authority for ordered account-erasure completion."""

from __future__ import annotations

from datetime import datetime, timedelta
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.account.reauth import create_account_erasure_reauth_receipt
from app.mainai_founder_boot.readiness import PERSONAL_RECALL_PRODUCTION_IDENTITY_FILES
from app.models.refresh_token import RefreshToken
from app.models.revoked_access_token import RevokedAccessToken


def test_completion_migration_is_in_personal_recall_production_identity():
    assert (
        "alembic/versions/0088_account_erasure_completion_phase.py"
        in PERSONAL_RECALL_PRODUCTION_IDENTITY_FILES
    )


def _set_context(session, owner_id, access_jti: str | None) -> None:
    session.execute(text("SELECT set_config('app.current_user_id', :owner_id, true)"), {"owner_id": str(owner_id)})
    session.execute(text("SELECT set_config('app.current_access_jti', :jti, true)"), {"jti": access_jti or ""})


def _begin_operation(session, make_verified_user):
    owner, password = make_verified_user()
    issued_at = datetime.utcnow()
    owner.sessions_valid_after = issued_at - timedelta(seconds=2)
    access_jti = str(uuid.uuid4())
    session.add(
        RefreshToken(
            user_id=owner.id,
            family_id=uuid.uuid4(),
            token_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            access_jti=access_jti,
            csrf_token=uuid.uuid4().hex + uuid.uuid4().hex,
            created_at=issued_at,
            expires_at=issued_at + timedelta(hours=1),
        )
    )
    session.commit()
    owner = session.get(type(owner), owner.id)
    receipt = create_account_erasure_reauth_receipt(
        session,
        user=owner,
        password=password,
        access_jti=access_jti,
    )
    _set_context(session, owner.id, access_jti)
    operation_id = session.execute(
        text("SELECT account_erasure_begin_operation(:owner_id, :receipt_id)"),
        {"owner_id": str(owner.id), "receipt_id": str(receipt.receipt_id)},
    ).scalar_one()
    session.commit()
    return owner.id, access_jti, operation_id


def _set_phase(session, owner_id, access_jti, operation_id, phase):
    _set_context(session, owner_id, access_jti)
    return session.execute(
        text("SELECT account_erasure_set_phase(:operation_id, :owner_id, :phase)"),
        {"operation_id": str(operation_id), "owner_id": str(owner_id), "phase": phase},
    ).scalar_one()


def _complete(session, owner_id, access_jti, operation_id):
    _set_context(session, owner_id, access_jti)
    return session.execute(
        text("SELECT account_erasure_complete_operation(:operation_id, :owner_id)"),
        {"operation_id": str(operation_id), "owner_id": str(owner_id)},
    ).scalar_one()


def _operation_state(superuser_db, operation_id):
    return superuser_db.execute(
        text("SELECT status, phase FROM account_erasure_operations WHERE operation_id = :operation_id"),
        {"operation_id": str(operation_id)},
    ).one()


def test_started_operation_cannot_complete(db_session, superuser_db, make_verified_user):
    owner_id, access_jti, operation_id = _begin_operation(db_session, make_verified_user)
    with pytest.raises(DBAPIError):
        _complete(db_session, owner_id, access_jti, operation_id)
    db_session.rollback()
    assert _operation_state(superuser_db, operation_id) == ("active", "started")


def test_personal_recall_phase_cannot_complete(db_session, superuser_db, make_verified_user):
    owner_id, access_jti, operation_id = _begin_operation(db_session, make_verified_user)
    assert _set_phase(db_session, owner_id, access_jti, operation_id, "personal_recall_erasure") is True
    db_session.commit()
    with pytest.raises(DBAPIError):
        _complete(db_session, owner_id, access_jti, operation_id)
    db_session.rollback()
    assert _operation_state(superuser_db, operation_id) == ("active", "personal_recall_erasure")


def test_personal_data_phase_can_complete_once(db_session, superuser_db, make_verified_user):
    owner_id, access_jti, operation_id = _begin_operation(db_session, make_verified_user)
    assert _set_phase(db_session, owner_id, access_jti, operation_id, "personal_recall_erasure") is True
    assert _set_phase(db_session, owner_id, access_jti, operation_id, "personal_data_erasure") is True
    assert _complete(db_session, owner_id, access_jti, operation_id) is True
    db_session.commit()
    assert _operation_state(superuser_db, operation_id) == ("completed", "completed")

    with pytest.raises(DBAPIError):
        _complete(db_session, owner_id, access_jti, operation_id)
    db_session.rollback()


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_terminal_operation_cannot_complete(
    db_session, superuser_db, make_verified_user, terminal_status
):
    owner_id, access_jti, operation_id = _begin_operation(db_session, make_verified_user)
    superuser_db.execute(
        text(
            "UPDATE account_erasure_operations "
            "SET status = :status, phase = 'personal_data_erasure' "
            "WHERE operation_id = :operation_id"
        ),
        {"status": terminal_status, "operation_id": str(operation_id)},
    )
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        _complete(db_session, owner_id, access_jti, operation_id)
    db_session.rollback()
    assert _operation_state(superuser_db, operation_id) == (terminal_status, "personal_data_erasure")


def test_completion_rejects_wrong_owner_and_operation(db_session, make_verified_user):
    owner_id, access_jti, operation_id = _begin_operation(db_session, make_verified_user)
    other, _ = make_verified_user()

    _set_context(db_session, other.id, access_jti)
    with pytest.raises(DBAPIError):
        db_session.execute(
            text("SELECT account_erasure_complete_operation(:operation_id, :owner_id)"),
            {"operation_id": str(operation_id), "owner_id": str(owner_id)},
        )
    db_session.rollback()

    with pytest.raises(DBAPIError):
        _complete(db_session, owner_id, access_jti, uuid.uuid4())
    db_session.rollback()


def test_completion_requires_current_unrevoked_session(
    db_session, superuser_db, make_verified_user
):
    owner_id, access_jti, operation_id = _begin_operation(db_session, make_verified_user)
    _set_phase(db_session, owner_id, access_jti, operation_id, "personal_recall_erasure")
    _set_phase(db_session, owner_id, access_jti, operation_id, "personal_data_erasure")
    db_session.commit()

    with pytest.raises(DBAPIError):
        _complete(db_session, owner_id, None, operation_id)
    db_session.rollback()

    superuser_db.add(
        RevokedAccessToken(jti=access_jti, expires_at=datetime.utcnow() + timedelta(minutes=10))
    )
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        _complete(db_session, owner_id, access_jti, operation_id)
    db_session.rollback()
    assert _operation_state(superuser_db, operation_id) == ("active", "personal_data_erasure")


def test_completion_rejects_session_made_stale_after_reauth(
    db_session, superuser_db, make_verified_user
):
    owner_id, access_jti, operation_id = _begin_operation(db_session, make_verified_user)
    _set_phase(db_session, owner_id, access_jti, operation_id, "personal_recall_erasure")
    _set_phase(db_session, owner_id, access_jti, operation_id, "personal_data_erasure")
    db_session.commit()

    superuser_db.execute(
        text("UPDATE users SET sessions_valid_after = now() + interval '1 second' WHERE id = :owner_id"),
        {"owner_id": str(owner_id)},
    )
    superuser_db.commit()
    with pytest.raises(DBAPIError):
        _complete(db_session, owner_id, access_jti, operation_id)
    db_session.rollback()
    assert _operation_state(superuser_db, operation_id) == ("active", "personal_data_erasure")


def test_phase_transition_cannot_skip_or_move_backward(db_session, superuser_db, make_verified_user):
    owner_id, access_jti, operation_id = _begin_operation(db_session, make_verified_user)
    with pytest.raises(DBAPIError):
        _set_phase(db_session, owner_id, access_jti, operation_id, "personal_data_erasure")
    db_session.rollback()
    assert _operation_state(superuser_db, operation_id) == ("active", "started")

    _set_phase(db_session, owner_id, access_jti, operation_id, "personal_recall_erasure")
    _set_phase(db_session, owner_id, access_jti, operation_id, "personal_data_erasure")
    db_session.commit()
    with pytest.raises(DBAPIError):
        _set_phase(db_session, owner_id, access_jti, operation_id, "personal_recall_erasure")
    db_session.rollback()
    assert _operation_state(superuser_db, operation_id) == ("active", "personal_data_erasure")


def test_failed_completion_rolls_back_phase_and_never_marks_completed(
    db_session, superuser_db, make_verified_user
):
    owner_id, access_jti, operation_id = _begin_operation(db_session, make_verified_user)
    _set_phase(db_session, owner_id, access_jti, operation_id, "personal_recall_erasure")
    with pytest.raises(DBAPIError):
        _complete(db_session, owner_id, access_jti, operation_id)
    db_session.rollback()
    assert _operation_state(superuser_db, operation_id) == ("active", "started")
