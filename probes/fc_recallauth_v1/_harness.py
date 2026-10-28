"""Shared helpers for the Personal Recall Security Boundary examination.

Target: Codex candidate head `460e8ad8cbd40b7fb0d62702f28c14412688e298`. Subject: the DB
authority boundary for Personal Recall:

  * personal_recall_owner_keys  — guard personal_recall_key_guard (migration 0095): runtime
    cannot create/rotate; mutation needs current canonical founder authority; delete only
    under a governed erasure.
  * personal_recall_grants      — guard personal_recall_grant_guard (migration 0095): runtime
    cannot mint; creation needs founder-authority GUC AND current canonical founder authority
    bound to the owner; bindings immutable except one-way revocation; delete only under
    erasure.
  * personal_recall_sources / _chunks / _extractions — protected personal data; FORCE RLS
    owner isolation.

RECALL STAYS INACTIVE. This suite only constructs the authority SUBSTRATE (an owner key + a
grant) through the product's own privileged services in order to examine the DB guards; it
performs NO ingestion, retrieval, snapshot or production activation. Authority is bound to the
canonical Founder identity (FOUNDER_USER_ID) + a live access JTI, exactly as production login
establishes it. Read-only examiner; loopback only; no paid calls.
"""
from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timedelta

os.environ.setdefault("PERSONAL_RECALL_SYSTEM_KEK_B64", base64.b64encode(b"r" * 32).decode("ascii"))
os.environ.setdefault("PERSONAL_RECALL_SYSTEM_KEK_VERSION", "test-v1")

from sqlalchemy import text  # noqa: E402

OWNER_KEYS = "personal_recall_owner_keys"
GRANTS = "personal_recall_grants"
SOURCES = "personal_recall_sources"
CHUNKS = "personal_recall_chunks"


def set_rls_user(session, user_id, *, access_jti: str | None = None) -> None:
    session.execute(text("SET LOCAL app.current_user_id = :uid"), {"uid": str(user_id)})
    if access_jti is not None:
        session.execute(text("SET LOCAL app.current_access_jti = :jti"), {"jti": access_jti})


def ensure_founder_user():
    """Create (or normalize) the canonical Founder identity through the privileged boundary."""
    from app.db import SecurityKernelSessionLocal
    from app.founder import FOUNDER_USER_ID
    from app.models.user import User, UserRole
    from app.security import hash_password

    now = datetime.utcnow()
    with SecurityKernelSessionLocal() as authority_db:
        user = authority_db.get(User, FOUNDER_USER_ID)
        if user is None:
            user = User(
                id=FOUNDER_USER_ID, email="founder@lifeos.local",
                password_hash=hash_password("CorrectHorseBattery9!"), role=UserRole.founder,
                is_active=True, email_verified=True, email_verified_at=now,
                sessions_valid_after=now - timedelta(seconds=2),
            )
            authority_db.add(user)
        else:
            user.role = UserRole.founder
            user.is_active = True
            user.email_verified = True
            user.sessions_valid_after = now - timedelta(seconds=2)
        authority_db.commit()
    return FOUNDER_USER_ID


def founder_authority_and_key():
    """Establish canonical Founder authority + an owner key via the product services.

    Returns (FOUNDER_USER_ID, RecallFounderAuthority, access_jti). Uses the privileged
    SecurityKernelSessionLocal exactly as production login does."""
    from app.db import SecurityKernelSessionLocal
    from app.founder import FOUNDER_USER_ID
    from app.models.refresh_token import RefreshToken
    from app.personal_recall.production_crypto import load_system_kek_from_env
    from app.personal_recall.production_ingestion import (
        RecallFounderAuthority,
        get_or_create_owner_key,
        recall_founder_authority_from_user,
    )
    from app.models.user import User

    ensure_founder_user()
    issued_at = datetime.utcnow()
    access_jti = str(uuid.uuid4())
    with SecurityKernelSessionLocal() as authority_db:
        authority_db.add(RefreshToken(
            user_id=FOUNDER_USER_ID, family_id=uuid.uuid4(),
            token_hash=uuid.uuid4().hex + uuid.uuid4().hex, access_jti=access_jti,
            csrf_token=uuid.uuid4().hex + uuid.uuid4().hex, created_at=issued_at,
            expires_at=issued_at + timedelta(hours=1),
        ))
        authority_db.commit()
        set_rls_user(authority_db, FOUNDER_USER_ID, access_jti=access_jti)
        get_or_create_owner_key(
            authority_db, owner_id=FOUNDER_USER_ID, system_kek=load_system_kek_from_env(),
            authority=RecallFounderAuthority(owner_id=FOUNDER_USER_ID, access_jti=access_jti,
                                             actor="founder_session"),
        )
        authority_db.commit()
        user = authority_db.get(User, FOUNDER_USER_ID)
        authority = recall_founder_authority_from_user(user=user, access_jti=access_jti)
    return FOUNDER_USER_ID, authority, access_jti


def mint_grant(owner_id, authority, *, session_id="exam-session", purpose="founder_file_ingestion"):
    """Create a legitimate Recall grant through the product service (privileged session)."""
    from app.db import SecurityKernelSessionLocal
    from app.personal_recall.production_ingestion import create_recall_grant

    with SecurityKernelSessionLocal() as authority_db:
        set_rls_user(authority_db, owner_id, access_jti=authority.access_jti)
        row = create_recall_grant(
            authority_db, owner_id=owner_id, session_id=session_id, purpose=purpose,
            resource_classes=("file",), disclosure_level="snippet", can_disclose=True,
            authority=authority,
        )
        authority_db.commit()
        grant_id = row.id
    return grant_id


def key_id_for(owner_id):
    from app.db import SecurityKernelSessionLocal

    with SecurityKernelSessionLocal() as authority_db:
        set_rls_user(authority_db, owner_id)
        return authority_db.execute(
            text(f"SELECT id FROM {OWNER_KEYS} WHERE owner_id=:o"), {"o": str(owner_id)},
        ).scalar_one()
