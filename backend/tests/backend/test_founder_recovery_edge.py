"""P3 edge-case verification for the founder recovery — delivery failures, post-initiate
state changes, invalidation completeness, and migration/model parity.

Complements test_founder_recovery.py (arming, codes, identity, replay, expiry, HTTP
neutrality) and test_auth_concurrency.py (concurrent confirmation). The recovery CODE is a
static provisioning secret and deliberately does not expire; what expires is the single-use
TOKEN (covered here and in the main suite).
"""

import hashlib
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.founder_recovery as fr
from app.config import Settings
from app.founder import FOUNDER_USER_ID
from app.models.audit import AuditLog
from app.models.founder_email_change import FounderEmailChangeRequest
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole
from app.security import generate_refresh_token, hash_refresh_token, hash_password

OLD_EMAIL = "founder@old-strato.example"
NEW_EMAIL = "dennis.torildsson@proton.me"
CODE = "edge-suite-recovery-code-plaintext"
CODE_SHA = hashlib.sha256(CODE.encode()).hexdigest()

@compiles(UUID, "sqlite")
def _uuid_sqlite(t, c, **kw):
    return "CHAR(32)"


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(t, c, **kw):
    return "JSON"


class Mail:
    """Configurable mail double: records sends, returns a chosen delivery status, can raise."""

    def __init__(self, delivered=True, raise_on_send=False):
        self.sent = []
        self.delivered = delivered
        self.raise_on_send = raise_on_send

    def __call__(self, to, subject, body):
        if self.raise_on_send:
            raise RuntimeError("simulated delivery-layer crash")
        self.sent.append((to, subject, body))
        return self.delivered

    def token(self):
        body = self.sent[-1][2]
        return body[body.index("token=") + 6:].split()[0].strip()


@pytest.fixture
def env(monkeypatch):
    from app.db import Base

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, expire_on_commit=False)()
    db.add(User(id=FOUNDER_USER_ID, email=OLD_EMAIL, password_hash=hash_password("edge-suite-password-1!"),
                role=UserRole.founder, is_active=True, email_verified=True))
    db.commit()
    settings = Settings(environment="development", founder_recovery_enabled=True,
                        founder_recovery_code_sha256=CODE_SHA, founder_recovery_token_expire_hours=1)
    monkeypatch.setattr(fr, "get_settings", lambda: settings)
    mail = Mail()
    monkeypatch.setattr(fr, "send_email", mail)
    yield db, mail, settings, monkeypatch
    db.close()
    engine.dispose()


def initiate(db, mail, settings, **kw):
    return fr.initiate_founder_email_change(db, new_email=kw.get("new_email", NEW_EMAIL),
                                            presented_code=kw.get("code", CODE), settings=settings)


# ---------------------------------------------------------------------------
# Email delivery unavailability
# ---------------------------------------------------------------------------

def test_delivery_failure_is_audited_and_cli_truth_preserved(env):
    """SMTP outage: request created (re-initiate remains possible), audit says
    delivery_failed, initiate reports False — the operator is never told a lie."""
    db, mail, settings, _mp = env
    mail.delivered = False
    assert initiate(db, mail, settings) is False
    details = [a.detail for a in db.query(AuditLog).filter_by(action="founder_email_recovery_initiated")]
    assert details == ["email_change delivery_failed"]
    # the request still exists and expires on its own; re-initiate replaces it
    assert db.query(FounderEmailChangeRequest).filter_by(used_at=None).count() == 1
    mail.delivered = True
    assert initiate(db, mail, settings) is True
    assert db.query(FounderEmailChangeRequest).filter_by(used_at=None).count() == 1  # old one invalidated


def test_delivery_layer_crash_leaves_no_zombie_request(env, monkeypatch):
    """Even if the delivery layer raises despite its never-raise contract, initiate
    surfaces a clean refusal and leaves NO outstanding request behind."""
    db, mail, settings, _mp = env
    mail.raise_on_send = True
    with pytest.raises(fr.FounderRecoveryError, match="leverans"):
        initiate(db, mail, settings)
    assert db.query(FounderEmailChangeRequest).filter_by(used_at=None).count() == 0
    actions = [a.action for a in db.query(AuditLog).all()]
    assert "founder_email_recovery_delivery_failed" in actions


def test_confirmation_mail_failure_does_not_roll_back_the_change(env):
    """The identity change and revocations are committed BEFORE the confirmation mail; a
    delivery outage afterwards is audited but never rolls back a completed recovery."""
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    token = mail.token()
    mail.delivered = False  # confirmation mail will "fail"
    assert fr.confirm_founder_email_change(db, token_plain=token, settings=settings) == NEW_EMAIL
    user = db.get(User, FOUNDER_USER_ID)
    assert user.email == NEW_EMAIL and user.email_verified is True
    actions = [a.action for a in db.query(AuditLog).all()]
    assert "founder_email_recovery_confirmation_mail" in actions
    failed = [a for a in db.query(AuditLog).all() if a.action == "founder_email_recovery_confirmation_mail"]
    assert failed and "delivery_failed" in failed[-1].detail


# ---------------------------------------------------------------------------
# State changes between initiate and confirm
# ---------------------------------------------------------------------------

def test_confirm_refused_if_founder_deactivated_after_initiation(env):
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    token = mail.token()
    user = db.get(User, FOUNDER_USER_ID)
    user.is_active = False
    db.add(user)
    db.commit()
    with pytest.raises(fr.FounderRecoveryError, match="återställningsbart"):
        fr.confirm_founder_email_change(db, token_plain=token, settings=settings)
    assert db.get(User, FOUNDER_USER_ID).email == OLD_EMAIL


def test_invalidation_completeness_multi_family(env):
    """After confirm: EVERY refresh family dead, every outstanding verification/reset/
    recovery token dead — across multiple families, not just the first."""
    from app.models.password_reset_token import PasswordResetToken
    from app.models.email_verification_token import EmailVerificationToken
    from app.security import generate_opaque_token, hash_opaque_token

    db, mail, settings, _mp = env
    user = db.get(User, FOUNDER_USER_ID)
    for _ in range(3):  # three independent session families
        db.add(RefreshToken(user_id=user.id, family_id=uuid.uuid4(),
                            token_hash=hash_refresh_token(generate_refresh_token()),
                            access_jti=f"jti-{uuid.uuid4()}", csrf_token="c",
                            expires_at=datetime.utcnow() + timedelta(days=1)))
    db.add(PasswordResetToken(user_id=user.id, token_hash=hash_opaque_token(generate_opaque_token()),
                              expires_at=datetime.utcnow() + timedelta(hours=1)))
    db.add(EmailVerificationToken(user_id=user.id, token_hash=hash_opaque_token(generate_opaque_token()),
                                  expires_at=datetime.utcnow() + timedelta(hours=1)))
    db.commit()
    initiate(db, mail, settings)
    token = mail.token()
    fr.confirm_founder_email_change(db, token_plain=token, settings=settings)

    db.expire_all()
    live_refresh = db.query(RefreshToken).filter_by(user_id=user.id, revoked_at=None).count()
    assert live_refresh == 0, "every session family must be revoked"
    assert db.query(PasswordResetToken).filter_by(user_id=user.id, used_at=None).count() == 0
    assert db.query(EmailVerificationToken).filter_by(user_id=user.id, used_at=None).count() == 0
    assert db.query(FounderEmailChangeRequest).filter_by(user_id=user.id, used_at=None).count() == 0
    # account data preserved: the rows exist (revoked/used), they were not deleted
    assert db.query(RefreshToken).filter_by(user_id=user.id).count() == 3


def test_recovery_code_is_not_a_time_bomb_and_never_logged(env):
    """The recovery CODE is a static provisioning secret (no expiry by design — it is
    rotated by replacing FOUNDER_RECOVERY_CODE_SHA256). It must appear nowhere in the
    request rows or audit details."""
    db, mail, settings, _mp = env
    initiate(db, mail, settings)
    dump = ""
    for row in db.query(FounderEmailChangeRequest).all():
        dump += f"{row.new_email}{row.old_email}{row.token_hash}"
    for a in db.query(AuditLog).all():
        dump += str(a.detail or "")
    assert CODE not in dump and CODE_SHA not in dump


# ---------------------------------------------------------------------------
# Migration / model parity (0069 vs FounderEmailChangeRequest)
# ---------------------------------------------------------------------------

def test_migration_0069_matches_model_and_is_lineage_head():
    import re
    from pathlib import Path

    versions = Path(__file__).resolve().parents[2] / "alembic" / "versions"
    mig = (versions / "0069_founder_email_recovery.py").read_text()
    ddl = re.search(r"CREATE TABLE founder_email_change_requests \((.*?)\n        \)", mig, re.S).group(1)
    ddl_cols = dict(re.findall(r"^\s+(\w+) ([\w()]+)", ddl, re.M))

    cols = {c.name: c for c in FounderEmailChangeRequest.__table__.columns}
    for name in ("id", "user_id", "new_email", "old_email", "token_hash", "created_at", "expires_at", "used_at"):
        assert name in ddl_cols, f"migration lacks column {name}"
        assert name in cols, f"model lacks column {name}"
    assert ddl_cols["new_email"] == "varchar(320)" and cols["new_email"].type.length == 320
    assert ddl_cols["token_hash"] == "varchar(64)"
    assert "NOT NULL UNIQUE" in ddl or "token_hash varchar(64) NOT NULL UNIQUE" in ddl.replace("\n", " ")
    # user_id is owner-scoped with a FK that cascades
    assert "REFERENCES users(id) ON DELETE CASCADE" in ddl

    # lineage integrity: my tree's revisions form a single chain ending at 0069
    revs, downs = {}, set()
    for p in versions.glob("*.py"):
        text = p.read_text()
        m = re.search(r"^revision\s*[:=]\s*\"(\w+)\"", text, re.M)
        d = re.search(r"^down_revision\s*[:=]\s*\"(\w+)\"", text, re.M)
        if m:
            revs[m.group(1)] = p.name
            if d:
                downs.add(d.group(1))
    assert "0069" in revs and "0068" in downs
    heads = set(revs) - downs
    # The lineage must have EXACTLY ONE head — the newest migration. It was 0069 when
    # this test was written; 0070 (billing) / 0071 (ads) legitimately extend the chain.
    assert len(heads) == 1, f"lineage must have exactly one head, got {heads}"
    assert "0069" in revs and "0068" in downs, "recovery migration must stay chained 0068 -> 0069"
