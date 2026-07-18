import re
from datetime import datetime, timedelta

from app.models.email_verification_token import EmailVerificationToken
from app.models.user import User


def _extract_token(body: str) -> str:
    return re.search(r"token=(\S+)", body).group(1)


def test_valid_token_verifies_and_is_single_use(client, sent_emails):
    client.post("/api/auth/register", json={"email": "verifyme@example.com", "password": "GoodPassword1234!", "website": ""})
    token = _extract_token(sent_emails[0]["body"])

    first = client.post("/api/auth/verify-email", json={"token": token})
    assert first.status_code == 200

    second = client.post("/api/auth/verify-email", json={"token": token})
    assert second.status_code == 400


def test_unknown_token_rejected(client):
    res = client.post("/api/auth/verify-email", json={"token": "this-token-was-never-issued"})
    assert res.status_code == 400


def test_expired_token_rejected(client, db_session, sent_emails):
    client.post("/api/auth/register", json={"email": "expired@example.com", "password": "GoodPassword1234!", "website": ""})
    token = _extract_token(sent_emails[0]["body"])

    row = db_session.query(EmailVerificationToken).join(User).filter(User.email == "expired@example.com").first()
    row.expires_at = datetime.utcnow() - timedelta(hours=1)
    db_session.commit()

    res = client.post("/api/auth/verify-email", json={"token": token})
    assert res.status_code == 400

    user = db_session.query(User).filter_by(email="expired@example.com").first()
    assert user.email_verified is False


def test_resend_verification_is_neutral_and_works_for_unverified_accounts(client, sent_emails):
    client.post("/api/auth/register", json={"email": "resend@example.com", "password": "GoodPassword1234!", "website": ""})
    first_token = _extract_token(sent_emails[0]["body"])

    resend_unknown = client.post("/api/auth/resend-verification", json={"email": "never-registered@example.com"})
    resend_known = client.post("/api/auth/resend-verification", json={"email": "resend@example.com"})
    assert resend_unknown.status_code == resend_known.status_code == 202
    assert resend_unknown.json() == resend_known.json()

    assert len(sent_emails) == 2
    second_token = _extract_token(sent_emails[1]["body"])
    assert second_token != first_token

    res = client.post("/api/auth/verify-email", json={"token": second_token})
    assert res.status_code == 200


def test_resend_verification_for_already_verified_account_sends_nothing(client, make_verified_user, sent_emails):
    make_verified_user(email="alreadyverified@example.com")
    res = client.post("/api/auth/resend-verification", json={"email": "alreadyverified@example.com"})
    assert res.status_code == 202
    assert len(sent_emails) == 0
