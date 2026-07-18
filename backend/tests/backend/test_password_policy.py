import pytest

from app.password_policy import validate_password


def test_accepts_a_reasonable_password():
    validate_password("Sommarsemester2026", email="user@example.com")


def test_rejects_too_short():
    with pytest.raises(ValueError, match="tecken"):
        validate_password("Short1", email="user@example.com")


def test_rejects_no_letter():
    with pytest.raises(ValueError, match="bokstav"):
        validate_password("123456789012", email="user@example.com")


def test_rejects_no_digit():
    with pytest.raises(ValueError, match="siffra"):
        validate_password("abcdefghijklmnop", email="user@example.com")


def test_rejects_common_weak_password():
    with pytest.raises(ValueError, match="vanligt"):
        validate_password("password1234", email="user@example.com")


def test_rejects_password_containing_email_local_part():
    with pytest.raises(ValueError, match="e-postadress"):
        validate_password("johndoe12345678", email="johndoe@example.com")


def test_email_substring_check_is_skipped_without_email_context():
    # Used by the reset-password schema layer, which doesn't have the email in the same
    # payload — the endpoint re-validates with email context after loading the user.
    validate_password("johndoe12345678", email=None)
