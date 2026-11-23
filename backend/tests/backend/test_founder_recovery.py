"""Security regression tests for the secure founder account recovery (Phase 2).

Recovery is a TWO-CHANNEL chain: CLI initiation (server shell + recovery code) then a
single-use emailed link to the NEW address. These tests pin the security properties:
fail-closed arming, code verification, founder-row pinning, single-use hashed tokens,
identity preservation (id/role/password never move), session/token revocation on success,
neutral HTTP refusals, and no secret material anywhere it must not be.
"""

import hashlib
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import app.founder_recovery as fr
import app.routers.founder_recovery as fr_router
from app.config import Settings
from app.founder import FOUNDER_USER_ID
from app.models.audit import AuditLog
from app.models.founder_email_change import FounderEmailChangeRequest
from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole
from app.security import generate_refresh_token, hash_refresh_token, hash_password

OLD_EMAIL = "founder@old-strato.example"
NEW_EMAIL = "dennis.torildsson@proton.me"
RECOVERY_CODE = "recovery-code-0123456789abcdef-plaintext"
CODE_SHA256 = hashlib.sha256(RECOVERY_CODE.encode()).hexdigest()


def make_settings(**over) -> Settings:
    base = dict(
        environment="development",
        founder_recovery_enabled=True,
        founder_recovery_code_sha256=CODE_SHA256,
        founder_recovery_token_expire_hours=1,
    )
    base.update(over)
    return Settings(**base)


@compiles(UUID, "sqlite")
def _uuid_sqlite(t, c, **kw):
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(t, c, **kw):
    return "JSON"


class CapturedMail:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, to: str, subject: str, body_text: str) -> None:
        self.sent.append((to, subject, body_text))

    def token_from_last_link(self) -> str:
        body = self.sent[-1][2]
        marker = "token="
        idx = body.index(marker)
        return body[idx + len(marker):].split()[0].strip()


@pytest.fixture
def env(monkeypatch):
    """SQLite-backed founder row + captured mail + armed settings."""
    from app.db import Base

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()

    founder = User(
        id=FOUNDER_USER_ID,
        email=OLD_EMAIL,
        password_hash=hash_password("correct horse battery staple 1"),
        role=UserRole.founder,
        is_active=True,
        email_verified=True,
    )
    db.add(founder)
    db.commit()

    settings = make_settings()
    monkeypatch.setattr(fr, "get_settings", lambda: settings)
    monkeypatch.setattr(fr_router, "settings", settings)
    mail = CapturedMail()
    monkeypatch.setattr(fr, "send_email", mail)

    yield db, mail, settings, monkeypatch
    db.close()
    engine.dispose()


def outstanding_requests(db) -> list[FounderEmailChangeRequest]:
    return db.query(FounderEmailChangeRequest).filter_by(used_at=None).all()


def initiate(db, mail, settings, new_email=NEW_EMAIL, code=RECOVERY_CODE):
    fr.initiate_founder_email_change(db, new_email=new_email, presented_code=code, settings=settings)


# ---------------------------------------------------------------------------
# Fail-closed arming and code verification
# ---------------------------------------------------------------------------

def test_recovery_disabled_by_default_refuses(monkeypatch):
    from app.db import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    fr_default = Settings()  # founder_recovery_enabled defaults to False
    assert fr_default.founder_recovery_enabled is False
    db = sessionmaker(bind=engine)()
    with pytest.raises(fr.FounderRecoveryError, match="avstängd"):
        fr.initiate_founder_email_change(
            db, new_email=NEW_EMAIL, presented_code=RECOVERY_CODE,
            settings=make_settings(founder_recovery_enabled=False),
        )
    assert db.query(FounderEmailChangeRequest).count() == 0


def test_initiate_without_configured_code_refuses(env):
    db, mail, settings, _mp = env
    with pytest.raises(fr.FounderRecoveryError, match="ingen återställningskod"):
        fr.initiate_founder_email_change(
            db, new_email=NEW_EMAIL, presented_code=RECOVERY_CODE,
            settings=make_settings(founder_recovery_code_sha256=""),
        )
    assert mail.sent == []


def test_wrong_code_refuses_and_sends_nothing(env):
    db, mail, settings, _mp = env
    with pytest.raises(fr.FounderRecoveryError, match="koden är fel"):
        initiate(db, mail, settings, code="wrong-code")
    assert mail.sent == []
    assert db.query(FounderEmailChangeRequest).count() == 0


def test_non_pinned_founder_row_refused(env):
    """Even a role=founder row with a different PRIMARY KEY is refused — recovery is pinned
    to the exact FOUNDER_USER_ID row, mirroring require_founder()."""
    db, mail, settings, _mp = env
    impostor = User(id=uuid.UUID(int=999), email="impostor@example.com",
                    password_hash="x", role=UserRole.founder, is_active=True)
    db.add(impostor)
    db.commit()
    db.delete(db.get(User, FOUNDER_USER_ID))
    db.commit()
    with pytest.raises(fr.FounderRecoveryError, match="återställningsbart"):
        initiate(db, mail, settings)
    assert mail.sent == []


def test_identical_new_email_refused(env):
    db, mail, settings, _mp = env
    with pytest.raises(fr.FounderRecoveryError, match="identisk"):
        initiate(db, mail, settings, new_email=OLD_EMAIL)
    assert mail.sent == []


# ---------------------------------------------------------------------------
# Initiate: one outstanding request, hashed token only, email to the NEW address
# ---------------------------------------------------------------------------

def test_initiate_creates_single_request_emails_new_address(env):
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    reqs = outstanding_requests(db)
    assert len(reqs) == 1
    req = reqs[0]
    assert req.new_email == NEW_EMAIL
    assert req.old_email == OLD_EMAIL
    assert len(req.token_hash) == 64 and req.token_hash.isalnum() or all(c in "0123456789abcdef" for c in req.token_hash)
    # plaintext token is NOT stored anywhere on the row
    row_str = f"{req.token_hash}{req.new_email}{req.old_email}"
    assert RECOVERY_CODE not in row_str
    # mail went ONLY to the new address
    assert [to for to, _, _ in mail.sent] == [NEW_EMAIL]
    # audit trail exists
    actions = [a.action for a in db.query(AuditLog).all()]
    assert "founder_email_recovery_initiated" in actions


def test_reinitiate_invalidates_previous_request(env):
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    first_token = mail.token_from_last_link()
    initiate(db, mail, settings)  # second initiation
    second_token = mail.token_from_last_link()
    assert first_token != second_token
    with pytest.raises(fr.FounderRecoveryError, match="ogiltig"):
        fr.confirm_founder_email_change(db, token_plain=first_token, settings=settings)
    # the newest one still works — proven in the confirm tests below


# ---------------------------------------------------------------------------
# Confirm: identity preservation, revocation, single-use
# ---------------------------------------------------------------------------

def _make_session(db, user) -> None:
    refresh_plain = generate_refresh_token()
    db.add(RefreshToken(
        user_id=user.id, family_id=uuid.uuid4(), token_hash=hash_refresh_token(refresh_plain),
        access_jti=f"jti-{uuid.uuid4()}", csrf_token="csrf-value-for-tests",
        expires_at=datetime.utcnow() + timedelta(days=1),
    ))
    db.commit()
    return refresh_plain


def test_confirm_happy_path_preserves_identity_and_revokes_sessions(env):
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    token = mail.token_from_last_link()
    user = db.get(User, FOUNDER_USER_ID)

    old_password_hash = user.password_hash
    old_id = user.id
    old_role = user.role
    baseline = user.sessions_valid_after
    _make_session(db, user)
    live_refresh = db.query(RefreshToken).filter_by(user_id=user.id).first()

    result = fr.confirm_founder_email_change(db, token_plain=token, settings=settings)
    assert result == NEW_EMAIL

    db.refresh(user)
    assert user.email == NEW_EMAIL
    assert user.id == old_id                        # same row, same primary key
    assert user.role == old_role                    # founder role preserved
    assert user.password_hash == old_password_hash  # credentials untouched
    assert user.email_verified is True
    assert user.email_verified_at is not None

    # every session revoked: baseline bumped + refresh row marked revoked
    assert user.sessions_valid_after > baseline
    db.refresh(live_refresh)
    assert live_refresh.revoked_at is not None

    # outstanding recovery + password-reset tokens all invalidated
    assert outstanding_requests(db) == []
    assert db.query(PasswordResetToken).filter_by(user_id=user.id, used_at=None).count() == 0

    # audit + confirmation mail to the new address
    actions = [a.action for a in db.query(AuditLog).all()]
    assert "founder_email_recovery_completed" in actions
    assert [to for to, _, _ in mail.sent] == [NEW_EMAIL, NEW_EMAIL]


def test_confirm_rejects_unknown_token(env):
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    before = db.get(User, FOUNDER_USER_ID).email
    with pytest.raises(fr.FounderRecoveryError, match="ogiltig"):
        fr.confirm_founder_email_change(db, token_plain="0" * 43 + "x", settings=settings)
    assert db.get(User, FOUNDER_USER_ID).email == before


def test_confirm_is_single_use(env):
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    token = mail.token_from_last_link()
    fr.confirm_founder_email_change(db, token_plain=token, settings=settings)
    with pytest.raises(fr.FounderRecoveryError, match="ogiltig"):
        fr.confirm_founder_email_change(db, token_plain=token, settings=settings)


def test_confirm_refused_when_feature_disabled_after_initiation(env):
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    token = mail.token_from_last_link()
    disarmed = make_settings(founder_recovery_enabled=False)
    with pytest.raises(fr.FounderRecoveryError, match="avstängd"):
        fr.confirm_founder_email_change(db, token_plain=token, settings=disarmed)
    assert db.get(User, FOUNDER_USER_ID).email == OLD_EMAIL


def test_confirm_refused_when_account_email_changed_since_initiation(env):
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    token = mail.token_from_last_link()
    user = db.get(User, FOUNDER_USER_ID)
    user.email = "someone-else-changed@example.com"
    db.add(user)
    db.commit()
    with pytest.raises(fr.FounderRecoveryError, match="ändrats sedan begäran"):
        fr.confirm_founder_email_change(db, token_plain=token, settings=settings)


def test_expired_token_refused(env):
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    req = outstanding_requests(db)[0]
    req.expires_at = datetime.utcnow() - timedelta(seconds=1)
    db.add(req)
    db.commit()
    token = mail.token_from_last_link()
    with pytest.raises(fr.FounderRecoveryError, match="ogiltig"):
        fr.confirm_founder_email_change(db, token_plain=token, settings=settings)


# ---------------------------------------------------------------------------
# HTTP surface: neutral refusals, no secrets in responses
# ---------------------------------------------------------------------------

@pytest.fixture
def http_client(env, monkeypatch):
    from app.db import Base as _Base  # noqa: F401 — tables already created by env fixture

    db, mail, settings, mp = env
    app = FastAPI()
    app.include_router(fr_router.router)
    app.state.limiter = __import__("app.limiter", fromlist=["limiter"]).limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.dependency_overrides[fr_router.get_db] = lambda: db
    return TestClient(app, raise_server_exceptions=False), db, mail, settings


def test_http_confirm_garbage_token_is_neutral_400(http_client):
    client, _db, _mail, _settings = http_client
    resp = client.post("/api/auth/founder-recovery/confirm", json={"token": "garbage-token-value-1234"})
    assert resp.status_code == 400
    assert "ogiltig" in resp.json()["detail"]
    assert OLD_EMAIL not in resp.text and NEW_EMAIL not in resp.text
    assert "password_hash" not in resp.text


def test_http_confirm_success_shape(http_client):
    client, db, mail, settings = http_client
    initiate(db, mail, settings)
    token = mail.token_from_last_link()
    resp = client.post("/api/auth/founder-recovery/confirm", json={"token": token})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "email_recovered" and body["email"] == NEW_EMAIL
    assert "password" not in body
