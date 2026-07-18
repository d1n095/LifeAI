import uuid
from datetime import datetime, timezone

import jwt
import pytest

from app.security import (
    create_access_token,
    decode_access_token,
    generate_opaque_token,
    generate_refresh_token,
    hash_opaque_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def test_password_hash_is_argon2id():
    hashed = hash_password("CorrectHorseBattery9!")
    assert hashed.startswith("$argon2id$")


def test_password_hash_verifies_correct_password():
    hashed = hash_password("CorrectHorseBattery9!")
    assert verify_password("CorrectHorseBattery9!", hashed) is True


def test_password_hash_rejects_wrong_password():
    hashed = hash_password("CorrectHorseBattery9!")
    assert verify_password("WrongPassword123!", hashed) is False


def test_password_hash_is_salted_differently_each_time():
    a = hash_password("CorrectHorseBattery9!")
    b = hash_password("CorrectHorseBattery9!")
    assert a != b


def test_access_token_roundtrip_and_claims():
    user_id = uuid.uuid4()
    token, jti = create_access_token(user_id, "member")
    payload = decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["role"] == "member"
    assert payload["jti"] == jti
    assert "iat" in payload
    assert "exp" in payload


def test_access_token_iat_is_recent():
    token, _ = create_access_token(uuid.uuid4(), "member")
    payload = decode_access_token(token)
    issued_at = datetime.fromtimestamp(payload["iat"], tz=timezone.utc)
    assert (datetime.now(timezone.utc) - issued_at).total_seconds() < 5


def test_access_token_rejects_tampering():
    token, _ = create_access_token(uuid.uuid4(), "member")
    tampered = token[:-4] + "abcd"
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(tampered)


def test_refresh_token_is_high_entropy_and_unique():
    a = generate_refresh_token()
    b = generate_refresh_token()
    assert a != b
    assert len(a) > 40


def test_hash_refresh_token_is_deterministic_sha256():
    token = "some-refresh-token-value"
    assert hash_refresh_token(token) == hash_refresh_token(token)
    assert len(hash_refresh_token(token)) == 64  # sha256 hex digest


def test_opaque_token_and_hash_used_by_verification_and_reset_tokens():
    token = generate_opaque_token()
    assert len(token) > 30
    digest = hash_opaque_token(token)
    assert digest == hash_opaque_token(token)
    assert digest != token  # never store/compare the plaintext
