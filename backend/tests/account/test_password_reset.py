import re
from datetime import datetime, timedelta

from app.models.password_reset_token import PasswordResetToken
from app.models.refresh_token import RefreshToken
from app.models.user import User


def _extract_token(body: str) -> str:
    return re.search(r"token=(\S+)", body).group(1)


def test_forgot_password_is_neutral_for_known_and_unknown_email(client, make_verified_user, sent_emails):
    make_verified_user(email="known@example.com")

    known = client.post("/api/auth/forgot-password", json={"email": "known@example.com"})
    unknown = client.post("/api/auth/forgot-password", json={"email": "unknown@example.com"})

    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json()
    assert len(sent_emails) == 1  # only the known account actually gets an email
    assert sent_emails[0]["to"] == "known@example.com"


def test_reset_with_valid_token_changes_password_and_allows_new_login(client, make_verified_user, sent_emails):
    user, old_password = make_verified_user(email="reset@example.com")
    client.post("/api/auth/forgot-password", json={"email": "reset@example.com"})
    token = _extract_token(sent_emails[0]["body"])

    new_password = "BrandNewPassword789!"
    res = client.post("/api/auth/reset-password", json={"token": token, "new_password": new_password})
    assert res.status_code == 200

    old_login = client.post("/api/auth/login", json={"email": "reset@example.com", "password": old_password})
    assert old_login.status_code == 401

    new_login = client.post("/api/auth/login", json={"email": "reset@example.com", "password": new_password})
    assert new_login.status_code == 200


def test_reset_token_is_single_use(client, make_verified_user, sent_emails):
    make_verified_user(email="onetime@example.com")
    client.post("/api/auth/forgot-password", json={"email": "onetime@example.com"})
    token = _extract_token(sent_emails[0]["body"])

    first = client.post("/api/auth/reset-password", json={"token": token, "new_password": "FirstNewPassword1!"})
    assert first.status_code == 200

    second = client.post("/api/auth/reset-password", json={"token": token, "new_password": "SecondNewPassword2!"})
    assert second.status_code == 400


def test_expired_reset_token_rejected(client, db_session, make_verified_user, sent_emails):
    make_verified_user(email="expiredreset@example.com")
    client.post("/api/auth/forgot-password", json={"email": "expiredreset@example.com"})
    token = _extract_token(sent_emails[0]["body"])

    row = db_session.query(PasswordResetToken).join(User).filter(User.email == "expiredreset@example.com").first()
    row.expires_at = datetime.utcnow() - timedelta(hours=1)
    db_session.commit()

    res = client.post("/api/auth/reset-password", json={"token": token, "new_password": "SomeNewPassword1!"})
    assert res.status_code == 400


def test_weak_new_password_rejected(client, make_verified_user, sent_emails):
    make_verified_user(email="weakreset@example.com")
    client.post("/api/auth/forgot-password", json={"email": "weakreset@example.com"})
    token = _extract_token(sent_emails[0]["body"])

    res = client.post("/api/auth/reset-password", json={"token": token, "new_password": "short1"})
    assert res.status_code == 400


def test_reset_revokes_all_refresh_tokens_for_the_user(client, db_session, make_verified_user, sent_emails):
    user, password = make_verified_user(email="revokeme@example.com")
    client.post("/api/auth/login", json={"email": "revokeme@example.com", "password": password})
    assert db_session.query(RefreshToken).filter_by(user_id=user.id, revoked_at=None).count() >= 1

    client.post("/api/auth/forgot-password", json={"email": "revokeme@example.com"})
    token = _extract_token(sent_emails[0]["body"])
    client.post("/api/auth/reset-password", json={"token": token, "new_password": "AfterResetPassword1!"})

    still_active = db_session.query(RefreshToken).filter_by(user_id=user.id, revoked_at=None).count()
    assert still_active == 0


def test_new_registration_issues_only_one_valid_reset_token_at_a_time(client, make_verified_user, sent_emails):
    make_verified_user(email="tokenrotate@example.com")
    client.post("/api/auth/forgot-password", json={"email": "tokenrotate@example.com"})
    first_token = _extract_token(sent_emails[0]["body"])

    client.post("/api/auth/forgot-password", json={"email": "tokenrotate@example.com"})
    second_token = _extract_token(sent_emails[1]["body"])

    stale = client.post("/api/auth/reset-password", json={"token": first_token, "new_password": "StalePassword12!"})
    assert stale.status_code == 400

    fresh = client.post("/api/auth/reset-password", json={"token": second_token, "new_password": "FreshPassword123!"})
    assert fresh.status_code == 200
