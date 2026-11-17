"""P4 regression tests — auth concurrency and race conditions.

Proven properties:
1. Refresh rotation is ATOMIC single-use: replaying a rotated cookie kills the family
   (401), and two concurrent refreshes with the SAME cookie yield exactly ONE new session
   — never two (the pre-fix SELECT/UPDATE window would hand out two live session pairs).
2. Concurrent duplicate registration never 500s and never creates two users.
3. Founder-recovery confirmation is atomic single-use under concurrency.
"""

import threading
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

import app.routers.auth as auth_module
import app.routers.founder_recovery as fr_router
from app.config import Settings
from app.cookies import REFRESH_COOKIE
from app.db import get_db
from app.deps import CSRF_HEADER
from app.founder import FOUNDER_USER_ID
from app.models.user import User, UserRole
from app.security import hash_password


@compiles(UUID, "sqlite")
def _uuid_sqlite(t, c, **kw):
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(t, c, **kw):
    return "JSON"


STRONG_PASSWORD = "Correct-Horse-Battery-1!"


@pytest.fixture
def auth_env(monkeypatch):
    from app.db import Base
    import app.models.audit  # noqa: F401
    import app.models.email_verification_token  # noqa: F401
    import app.models.founder_email_change  # noqa: F401
    import app.models.password_reset_token  # noqa: F401
    import app.models.refresh_token  # noqa: F401
    import app.models.revoked_access_token  # noqa: F401

    # Shared-cache in-memory SQLite with a REAL connection per session — StaticPool's
    # single shared connection corrupts concurrent transactions (sessions interleave on
    # one connection) and cannot demonstrate the race. With per-request connections the
    # race behaves like production: the loser's conditional UPDATE evaluates after the
    # winner's commit and cleanly loses.
    engine = create_engine(
        f"sqlite:///file:lifeai_conc_{uuid.uuid4().hex}?mode=memory&cache=shared",
        connect_args={"check_same_thread": False, "uri": True, "timeout": 30},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    db = Session()
    settings = Settings(environment="development")
    monkeypatch.setattr(auth_module, "settings", settings)
    monkeypatch.setattr(fr_router, "settings", settings)

    user = User(
        id=FOUNDER_USER_ID,
        email="founder@old-strato.example",
        password_hash=hash_password(STRONG_PASSWORD),
        role=UserRole.founder,
        is_active=True,
        email_verified=True,
    )
    db.add(user)
    db.commit()

    app = FastAPI()
    app.include_router(auth_module.router)
    app.include_router(fr_router.router)
    app.state.limiter = __import__("app.limiter", fromlist=["limiter"]).limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.dependency_overrides[get_db] = lambda: Session()
    client = TestClient(app, raise_server_exceptions=False)

    yield client, db, settings
    db.close()
    engine.dispose()


def _login(client):
    resp = client.post(
        "/api/auth/login",
        json={"email": "founder@old-strato.example", "password": STRONG_PASSWORD},
    )
    assert resp.status_code == 200
    return resp.cookies.get(REFRESH_COOKIE), resp.json()["csrf_token"]


# ---------------------------------------------------------------------------
# 1. Rotation replay and the concurrency race
# ---------------------------------------------------------------------------

def test_refresh_replay_kills_family(auth_env):
    client, db, _settings = auth_env
    refresh_plain, csrf = _login(client)

    # Normal rotation: 200, cookies rotate.
    first = client.post("/api/auth/refresh", cookies={REFRESH_COOKIE: refresh_plain}, headers={CSRF_HEADER: csrf})
    assert first.status_code == 200

    # Re-present the ALREADY-ROTATED cookie: reuse detection → family dies.
    replay = client.post("/api/auth/refresh", cookies={REFRESH_COOKIE: refresh_plain}, headers={CSRF_HEADER: csrf})
    assert replay.status_code == 401
    assert "återkallats" in replay.json()["detail"]

    family_ids = [row.family_id for row in db.query(__import__("app.models.refresh_token", fromlist=["RefreshToken"]).RefreshToken).all()]
    assert family_ids, "a family should exist"
    rows = db.query(__import__("app.models.refresh_token", fromlist=["RefreshToken"]).RefreshToken).filter_by(family_id=family_ids[0]).all()
    assert rows, "family rows exist"
    assert all(r.revoked_at is not None for r in rows), "replay must kill the WHOLE family"


def test_concurrent_refresh_same_cookie_exactly_one_winner(auth_env):
    client, db, _settings = auth_env
    refresh_plain, csrf = _login(client)

    results = []

    def racer():
        r = client.post("/api/auth/refresh", cookies={REFRESH_COOKIE: refresh_plain}, headers={CSRF_HEADER: csrf})
        results.append(r.status_code)

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1, f"exactly one rotation may win, got {results}"
    assert results.count(401) == 1, f"the loser must be rejected as a replay, got {results}"


# ---------------------------------------------------------------------------
# 2. Concurrent duplicate registration
# ---------------------------------------------------------------------------

def test_concurrent_duplicate_registration_never_500s(auth_env):
    client, db, _settings = auth_env
    email = f"race-{uuid.uuid4().hex[:8]}@example.com"
    results = []

    def racer():
        r = client.post("/api/auth/register", json={"email": email, "password": STRONG_PASSWORD, "website": ""})
        results.append(r.status_code)

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 202 for code in results), f"duplicate registration must stay neutral, got {results}"
    user_cls = __import__("app.models.user", fromlist=["User"]).User
    assert db.query(user_cls).filter_by(email=email).count() == 1, "exactly one user row may exist"


# ---------------------------------------------------------------------------
# 3. Founder-recovery confirm atomicity
# ---------------------------------------------------------------------------

def test_recovery_confirm_concurrent_single_use(auth_env, monkeypatch):
    import hashlib

    import app.founder_recovery as fr

    client, db, settings = auth_env
    code = "another-recovery-code-plaintext"
    sha = hashlib.sha256(code.encode()).hexdigest()
    monkeypatch.setattr(fr, "get_settings", lambda: Settings(
        environment="development",
        founder_recovery_enabled=True,
        founder_recovery_code_sha256=sha,
        founder_recovery_token_expire_hours=1,
    ))

    class Captured:
        sent = []

        def __call__(self, to, subject, body):
            Captured.sent.append((to, subject, body))

        @staticmethod
        def token():
            body = Captured.sent[-1][2]
            idx = body.index("token=")
            return body[idx + 6:].split()[0].strip()

    monkeypatch.setattr(fr, "send_email", Captured())
    monkeypatch.setattr(fr_router, "get_db", lambda: db)
    fr.initiate_founder_email_change(db, new_email="dennis.torildsson@proton.me", presented_code=code, settings=Settings(
        environment="development",
        founder_recovery_enabled=True,
        founder_recovery_code_sha256=sha,
        founder_recovery_token_expire_hours=1,
    ))
    token = Captured.token()

    results = []

    def racer():
        r = client.post("/api/auth/founder-recovery/confirm", json={"token": token})
        results.append(r.status_code)

    threads = [threading.Thread(target=racer) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(200) == 1, f"single-use confirm must have exactly one winner, got {results}"
    assert results.count(400) == 1, f"the loser must get the neutral refusal, got {results}"
    db.expire_all()
    user = db.get(User, FOUNDER_USER_ID)
    assert user.email == "dennis.torildsson@proton.me"
