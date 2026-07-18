import re

from app.models.user import User


def _extract_token(body: str) -> str:
    return re.search(r"token=(\S+)", body).group(1)


def test_register_creates_unverified_user_and_sends_verification_email(client, db_session, sent_emails):
    res = client.post(
        "/api/auth/register", json={"email": "New.User@Example.com", "password": "GoodPassword1234!", "website": ""}
    )
    assert res.status_code == 202
    # Response is deliberately neutral — no confirmation of what happened, just that a mail
    # *might* have been sent. See test_no_enumeration_via_duplicate_registration below.
    assert "detail" in res.json()

    user = db_session.query(User).filter_by(email="new.user@example.com").first()
    assert user is not None  # normalized (lowercased) on write
    assert user.email_verified is False

    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "new.user@example.com"
    assert "token=" in sent_emails[0]["body"]


def test_cannot_log_in_before_verifying(client):
    client.post("/api/auth/register", json={"email": "unverified@example.com", "password": "GoodPassword1234!", "website": ""})
    res = client.post("/api/auth/login", json={"email": "unverified@example.com", "password": "GoodPassword1234!"})
    assert res.status_code == 403


def test_weak_password_rejected_with_specific_message(client, db_session):
    res = client.post("/api/auth/register", json={"email": "weak@example.com", "password": "short1", "website": ""})
    assert res.status_code == 400
    assert "tecken" in res.json()["detail"]
    assert db_session.query(User).filter_by(email="weak@example.com").first() is None


def test_no_enumeration_via_duplicate_registration(client, db_session, sent_emails):
    first = client.post(
        "/api/auth/register", json={"email": "dup@example.com", "password": "GoodPassword1234!", "website": ""}
    )
    second = client.post(
        "/api/auth/register", json={"email": "dup@example.com", "password": "AnotherPassword5678!", "website": ""}
    )
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert db_session.query(User).filter_by(email="dup@example.com").count() == 1


def test_reregistering_unverified_account_issues_fresh_token_and_invalidates_old_one(client, sent_emails):
    client.post("/api/auth/register", json={"email": "retry@example.com", "password": "GoodPassword1234!", "website": ""})
    first_token = _extract_token(sent_emails[0]["body"])

    client.post("/api/auth/register", json={"email": "retry@example.com", "password": "GoodPassword1234!", "website": ""})
    second_token = _extract_token(sent_emails[1]["body"])

    assert first_token != second_token

    old_verify = client.post("/api/auth/verify-email", json={"token": first_token})
    assert old_verify.status_code == 400

    new_verify = client.post("/api/auth/verify-email", json={"token": second_token})
    assert new_verify.status_code == 200


def test_reregistering_already_verified_account_does_not_resend(client, db_session, sent_emails, make_verified_user):
    make_verified_user(email="already@example.com")
    res = client.post(
        "/api/auth/register", json={"email": "already@example.com", "password": "GoodPassword1234!", "website": ""}
    )
    assert res.status_code == 202
    assert len(sent_emails) == 0
    # No second account, and the original password is untouched.
    assert db_session.query(User).filter_by(email="already@example.com").count() == 1


def test_honeypot_field_silently_drops_the_submission(client, db_session, sent_emails):
    res = client.post(
        "/api/auth/register",
        json={"email": "bot@example.com", "password": "GoodPassword1234!", "website": "http://spam.example"},
    )
    assert res.status_code == 202
    assert db_session.query(User).filter_by(email="bot@example.com").first() is None
    assert len(sent_emails) == 0


def test_honeypot_response_is_identical_to_a_normal_registration_response(client):
    normal = client.post(
        "/api/auth/register", json={"email": "normal@example.com", "password": "GoodPassword1234!", "website": ""}
    )
    bot = client.post(
        "/api/auth/register",
        json={"email": "bot2@example.com", "password": "GoodPassword1234!", "website": "spam"},
    )
    assert normal.status_code == bot.status_code == 202
    assert normal.json() == bot.json()
