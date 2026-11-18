"""The audit trail cannot be rewritten, not even by the superuser connection.

`AuditLog` was documented as immutable and was not: nothing at the storage layer stopped an UPDATE
or a DELETE. That matters more here than anywhere else in the system, because the audit trail is
what an investigation relies on after a compromise. An agent that can reach the database through an
inherited credential could erase the record of its own actions and leave a clean-looking history.

The trigger added in migration 0079 is tested through the SUPERUSER session on purpose: if the
guarantee depended on the application role's grants, a role change or a new credential path would
silently remove it. This asserts the property survives the strongest connection available.
"""
import uuid

import pytest
from sqlalchemy import text as sqltext
from sqlalchemy.exc import DBAPIError

from app.models.audit import AuditLog


def _seed(superuser_db) -> uuid.UUID:
    row = AuditLog(id=uuid.uuid4(), action="security_control_tampered", entity_type="policy", detail="original")
    superuser_db.add(row)
    superuser_db.commit()
    return row.id


def test_an_audit_row_cannot_be_updated_even_by_superuser(superuser_db):
    audit_id = _seed(superuser_db)

    with pytest.raises(DBAPIError) as raised:
        superuser_db.execute(
            sqltext("UPDATE audit_log SET detail = 'rewritten' WHERE id = :id"), {"id": str(audit_id)}
        )
        superuser_db.commit()
    superuser_db.rollback()

    assert "append-only" in str(raised.value), f"the refusal must be the trigger, not an incidental error: {raised.value}"
    superuser_db.expire_all()
    assert superuser_db.get(AuditLog, audit_id).detail == "original"


def test_an_audit_row_cannot_be_deleted_even_by_superuser(superuser_db):
    audit_id = _seed(superuser_db)

    with pytest.raises(DBAPIError) as raised:
        superuser_db.execute(sqltext("DELETE FROM audit_log WHERE id = :id"), {"id": str(audit_id)})
        superuser_db.commit()
    superuser_db.rollback()

    assert "append-only" in str(raised.value)
    superuser_db.expire_all()
    assert superuser_db.get(AuditLog, audit_id) is not None, "history must still be there"


def test_appending_a_new_fact_is_still_allowed(superuser_db):
    """The rule is 'no rewriting', not 'no writing' — a trail you cannot append to is useless."""
    _seed(superuser_db)
    superuser_db.add(AuditLog(id=uuid.uuid4(), action="login_succeeded", detail="founder@lifeos.local"))
    superuser_db.commit()
    assert superuser_db.query(AuditLog).count() >= 2
